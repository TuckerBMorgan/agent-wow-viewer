# Runs in the Viewer desktop. Only request IDs, secret references and metrics leave this process.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$aws = 'C:\Program Files\Amazon\AWSCLIV2\aws.exe'
$s = Get-Content C:\viewer\settings.json | ConvertFrom-Json
$env:AWS_DEFAULT_REGION = $s.region
$root = 'C:\viewer\obs\config\obs-studio'
$profile = "$root\basic\profiles\Viewer"
$ingest = 'rtmps://ingest.global-contribute.live-video.net/app'
$socket = $null
$process = $null
$request = $null
$recordFile = $null
$stage = 'configuration'

function Write-Json($Path, $Value) {
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
}
function Send-Message($Message) {
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Message | ConvertTo-Json -Depth 30 -Compress))
    $cancel = [Threading.CancellationTokenSource]::new(10000)
    try { $socket.SendAsync([ArraySegment[byte]]::new($bytes), [Net.WebSockets.WebSocketMessageType]::Text, $true, $cancel.Token).GetAwaiter().GetResult() | Out-Null }
    finally { $cancel.Dispose() }
}
function Read-Message([int]$TimeoutMs = 10000) {
    $buffer = New-Object byte[] 65536
    $message = [IO.MemoryStream]::new()
    $cancel = [Threading.CancellationTokenSource]::new($TimeoutMs)
    try {
        do {
            $part = $socket.ReceiveAsync([ArraySegment[byte]]::new($buffer), $cancel.Token).GetAwaiter().GetResult()
            if ($part.MessageType -eq [Net.WebSockets.WebSocketMessageType]::Close) { throw 'OBS WebSocket closed.' }
            $message.Write($buffer, 0, $part.Count)
        } until ($part.EndOfMessage)
        return [Text.Encoding]::UTF8.GetString($message.ToArray()) | ConvertFrom-Json
    } finally { $cancel.Dispose(); $message.Dispose() }
}
function Hash-Base64([string]$Value) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return [Convert]::ToBase64String($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value))) }
    finally { $hash.Dispose() }
}
function Invoke-OBS($Type, $Data = @{}, [int]$TimeoutMs = 10000) {
    $script:stage = $Type
    $id = [Guid]::NewGuid().ToString('N')
    Send-Message @{op=6; d=@{requestType=$Type; requestId=$id; requestData=$Data}}
    do { $reply = Read-Message $TimeoutMs } until ($reply.op -eq 7 -and $reply.d.requestId -eq $id)
    # Do not echo request data or upstream errors: service settings can contain the Twitch key.
    if (!$reply.d.requestStatus.result) { throw "OBS $Type failed (code $($reply.d.requestStatus.code))." }
    return $reply.d.responseData
}
function Save-Status($State, $Details = @{}) {
    $status = @{state=$State; instance=$s.instance; updated_at=(Get-Date).ToUniversalTime().ToString('o')}
    if (Test-Path C:\viewer\job.json) { $status.job_id=(Get-Content C:\viewer\job.json | ConvertFrom-Json).job_id }
    if ($request) { $status.request_id=$request.id; $status.mode=$request.mode }
    if ($request.channel) { $status.channel=$request.channel }
    foreach ($key in $Details.Keys) { $status[$key]=$Details[$key] }
    Write-Json C:\viewer\stream-status.json $status
    & $aws s3 cp C:\viewer\stream-status.json "s3://$($s.bucket)/viewer/$($s.instance)/stream-status.json" --only-show-errors
    if ($LASTEXITCODE) { throw 'Could not upload OBS status.' }
}
function Stop-Outputs {
    if ((Invoke-OBS 'GetStreamStatus').outputActive) { Invoke-OBS 'StopStream' | Out-Null }
    if ((Invoke-OBS 'GetRecordStatus').outputActive) { $script:recordFile = (Invoke-OBS 'StopRecord').outputPath }
    for ($i=0; $i -lt 30; $i++) {
        if (!(Invoke-OBS 'GetStreamStatus').outputActive -and !(Invoke-OBS 'GetRecordStatus').outputActive) { break }
        Start-Sleep 1
    }
    if ($i -eq 30) { throw 'OBS outputs did not stop.' }
    Invoke-OBS 'SetStreamServiceSettings' @{streamServiceType='rtmp_common';streamServiceSettings=@{service='Twitch';protocol='RTMPS';server=$ingest;key=''}} | Out-Null
}

try {
    if (Get-Process obs64 -ErrorAction SilentlyContinue) { throw 'OBS is already running; stop the existing controller first.' }
    Save-Status 'initializing'
    New-Item -ItemType Directory -Force $profile, "$root\basic\scenes", "$root\plugin_config\obs-websocket", C:\viewer\recordings | Out-Null
    # Recover unattended after an interrupted VM shutdown; retain the actual crash logs.
    Remove-Item "$root\.sentinel\run_*" -Force -ErrorAction SilentlyContinue
    # Portable config is created only at runtime. Disable automatic desktop and microphone capture.
    @'
[General]
FirstRun=true
LastVersion=553648128
EnableAutoUpdates=false
[Basic]
Profile=Viewer
ProfileDir=Viewer
SceneCollection=Viewer
SceneCollectionFile=Viewer
[BasicWindow]
PreviewEnabled=false
SysTrayEnabled=true
SysTrayWhenStarted=true
'@ | Set-Content "$root\global.ini" -Encoding Ascii
    Copy-Item "$root\global.ini" "$root\user.ini" -Force
    @'
[General]
Name=Viewer
[Output]
Mode=Advanced
Reconnect=true
RetryDelay=2
MaxRetries=20
[Video]
BaseCX=1280
BaseCY=720
OutputCX=1280
OutputCY=720
FPSType=0
FPSCommon=60
ColorFormat=NV12
ColorSpace=709
ColorRange=Partial
[Audio]
SampleRate=48000
ChannelSetup=Stereo
DesktopDevice1=disabled
DesktopDevice2=disabled
AuxDevice1=disabled
AuxDevice2=disabled
AuxDevice3=disabled
[AdvOut]
Encoder=obs_nvenc_h264_tex
AudioEncoder=ffmpeg_aac
TrackIndex=1
ApplyServiceSettings=true
RecType=Standard
RecFilePath=C:/viewer/recordings
RecFormat2=mkv
RecEncoder=none
RecAudioEncoder=none
RecTracks=1
Track1Bitrate=160
[Stream1]
EnableMultitrackVideo=false
'@ | Set-Content "$profile\basic.ini" -Encoding Ascii
    Write-Json "$profile\streamEncoder.json" @{rate_control='cbr';bitrate=6000;keyint_sec=2;preset='p5';tune='hq';multipass='qres';profile='high';bf=2;lookahead=$false;adaptive_quantization=$true}
    Write-Json "$profile\service.json" @{type='rtmp_common';settings=@{service='Twitch';protocol='RTMPS';server=$ingest;key=''}}
    Write-Json "$root\basic\scenes\Viewer.json" @{name='Viewer';sources=@(@{name='Game';id='scene';versioned_id='scene';settings=@{items=@()}});scene_order=@(@{name='Game'});current_scene='Game';current_program_scene='Game'}
    $password = [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N')
    Write-Json "$root\plugin_config\obs-websocket\config.json" @{server_enabled=$true;server_port=4455;auth_required=$true;server_password=$password;first_load=$false;alerts_enabled=$false}
    $stage = 'starting OBS'
    $process = Start-Process 'C:\viewer\obs\bin\64bit\obs64.exe' -WorkingDirectory 'C:\viewer\obs\bin\64bit' -ArgumentList '--portable --profile Viewer --collection Viewer --minimize-to-tray --disable-updater --disable-missing-files-check' -PassThru
    $connectDeadline=(Get-Date).AddMinutes(5)
    $identified=$false
    while ((Get-Date) -lt $connectDeadline) {
        if ($process.HasExited) { throw 'OBS exited during startup.' }
        try {
            $socket = [Net.WebSockets.ClientWebSocket]::new()
            $cancel = [Threading.CancellationTokenSource]::new(3000)
            $socket.ConnectAsync([Uri]'ws://127.0.0.1:4455', $cancel.Token).GetAwaiter().GetResult() | Out-Null
            # First-boot initialization can accept TCP before the protocol is ready.
            $stage = 'receiving OBS hello'
            $hello = Read-Message 30000
            if ($hello.op -ne 0) { throw 'Unexpected OBS handshake.' }
            $secret = Hash-Base64 ($password + $hello.d.authentication.salt)
            $stage = 'authenticating with OBS'
            Send-Message @{op=1;d=@{rpcVersion=1;authentication=(Hash-Base64 ($secret + $hello.d.authentication.challenge));eventSubscriptions=0}}
            if ((Read-Message 30000).op -ne 2) { throw 'OBS authentication failed.' }
            $identified=$true
            break
        } catch { $socket.Dispose(); Start-Sleep 2 }
        finally { $cancel.Dispose() }
    }
    if (!$identified) { throw 'OBS WebSocket did not become ready within five minutes.' }
    $password = $null; $secret = $null
    # The socket starts before the frontend finishes loading the scene collection.
    for ($i=0; $i -lt 60; $i++) {
        if ((Invoke-OBS 'GetSceneList').currentProgramSceneName -eq 'Game') { break }
        Start-Sleep 1
    }
    if ($i -eq 60) { throw 'OBS scene collection did not finish loading.' }
    # The first game hook can take longer while Windows initializes capture/audio.
    $item = Invoke-OBS 'CreateInput' @{sceneName='Game';inputName='WoW';inputKind='game_capture';sceneItemEnabled=$true;inputSettings=@{capture_mode='window';window='World of Warcraft:GxWindowClassD3d:Wow.exe';priority=2;capture_cursor=$false;capture_audio=$true;limit_framerate=$true}} 60000
    Invoke-OBS 'SetSceneItemTransform' @{sceneName='Game';sceneItemId=$item.sceneItemId;sceneItemTransform=@{positionX=0;positionY=0;boundsType='OBS_BOUNDS_SCALE_INNER';boundsWidth=1280;boundsHeight=720;boundsAlignment=0;alignment=5}} | Out-Null
    Invoke-OBS 'SetCurrentProgramScene' @{sceneName='Game'} | Out-Null
    Save-Status 'ready'
    $current = ''; $finish = $null; $baseline = $null
    while (!$process.HasExited) {
        # A new viewer has no request yet. PowerShell 5 turns redirected native
        # stderr into a terminating error under Stop, even for an expected miss.
        try {
            $ErrorActionPreference = 'Continue'
            & $aws s3 cp "s3://$($s.bucket)/viewer/$($s.instance)/stream-request.json" C:\viewer\stream-request.tmp --only-show-errors 2>$null
            $downloaded = $LASTEXITCODE -eq 0
        } finally { $ErrorActionPreference = 'Stop' }
        if ($downloaded) {
            $next = Get-Content C:\viewer\stream-request.tmp | ConvertFrom-Json
            if ($next.id -ne $current) {
                $request = $next; $current=$request.id; $finish=$null; $baseline=$null
                try {
                    Stop-Outputs
                    if (!$request.expires_at -or [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -gt $request.expires_at) { throw 'Output request expired; send a fresh request.' }
                    if ($request.mode -notin @('stop','record','test','live')) { throw 'Unknown output mode.' }
                    if ($request.mode -eq 'stop') { Save-Status 'stopped'; continue }
                    # Refuse to send an empty scene when the game hook has not attached.
                    for ($i=0; $i -lt 30; $i++) {
                        $capture=(Invoke-OBS 'GetSceneItemTransform' @{sceneName='Game';sceneItemId=$item.sceneItemId}).sceneItemTransform
                        if ($capture.sourceWidth -gt 0 -and $capture.sourceHeight -gt 0) { break }
                        Start-Sleep 2
                    }
                    if ($i -eq 30) { throw 'Game capture is not ready.' }
                    $baseline = Invoke-OBS 'GetStats'
                    if ($request.mode -eq 'record') {
                        Invoke-OBS 'StartRecord' | Out-Null
                    } else {
                        if ($request.secret -notmatch '^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:agent-wow/viewer/twitch/[a-z0-9_]+-[A-Za-z0-9]{6}$') { throw 'Unexpected Twitch secret reference.' }
                        $key = & $aws secretsmanager get-secret-value --secret-id $request.secret --query SecretString --output text 2>$null
                        if ($LASTEXITCODE -or !$key -or $key -notmatch '^live_[A-Za-z0-9_]+$') { throw 'Could not load a valid Twitch stream key.' }
                        if ($request.mode -eq 'test') { $key += '?bandwidthtest=true' }
                        Invoke-OBS 'SetStreamServiceSettings' @{streamServiceType='rtmp_common';streamServiceSettings=@{service='Twitch';protocol='RTMPS';server=$ingest;key=$key}} | Out-Null
                        $key=$null
                        Invoke-OBS 'StartStream' | Out-Null
                    }
                    if ($request.mode -in @('record','test')) { $finish=(Get-Date).AddSeconds(60) }
                    $startDeadline=(Get-Date).AddSeconds(45); $wasActive=$false
                    Save-Status 'starting'
                } catch {
                    $key=$null
                    Stop-Outputs
                    $baseline=$null
                    Save-Status 'failed' @{error=$_.Exception.Message}
                    continue
                }
            }
        }
        if ($request -and $request.mode -ne 'stop' -and $baseline) {
            $stats = Invoke-OBS 'GetStats'
            $output = if ($request.mode -eq 'record') { Invoke-OBS 'GetRecordStatus' } else { Invoke-OBS 'GetStreamStatus' }
            # OBS resets encoding counters for each output; rendering counters span the process.
            $metrics = @{fps=$stats.activeFps;render_ms=$stats.averageFrameRenderTime;render_frames=($stats.renderTotalFrames-$baseline.renderTotalFrames);render_skipped=($stats.renderSkippedFrames-$baseline.renderSkippedFrames);encode_frames=$stats.outputTotalFrames;encode_skipped=$stats.outputSkippedFrames;output=$output}
            if ($output.outputActive -and !$output.outputReconnecting -and $output.outputBytes -gt 0) { $wasActive=$true }
            if (!$wasActive -and (Get-Date) -gt $startDeadline) { throw 'OBS output never became active; check the encoder and Twitch connection.' }
            if ($finish -and (Get-Date) -ge $finish) {
                if (!$wasActive -or $metrics.encode_frames -le 0) { throw 'OBS produced no encoded frames.' }
                Stop-Outputs
                Invoke-OBS 'SaveSourceScreenshot' @{sourceName='WoW';imageFormat='png';imageFilePath='C:\viewer\capture-preview.png'} | Out-Null
                & $aws s3 cp C:\viewer\capture-preview.png "s3://$($s.bucket)/viewer/$($s.instance)/capture-preview.png" --only-show-errors
                if ($request.mode -eq 'record') {
                    if (!$recordFile -or !(Test-Path $recordFile)) { throw 'OBS did not return a recording file.' }
                    & $aws s3 cp $recordFile "s3://$($s.bucket)/viewer/$($s.instance)/capture-test.mkv" --only-show-errors
                    if ($LASTEXITCODE) { throw 'Could not upload the test recording.' }
                }
                Save-Status 'complete' $metrics
                $baseline=$null; $finish=$null
            } elseif ($output.outputActive) { Save-Status 'active' $metrics }
        }
        Start-Sleep 5
    }
    throw 'OBS exited unexpectedly.'
} catch {
    $errorMessage = "${stage}: $($_.Exception.Message)"
    Add-Content C:\viewer\obs-controller.log $errorMessage
    Add-Content C:\viewer\obs-controller.log $_.ScriptStackTrace
    try { Save-Status 'failed' @{error=$errorMessage} } catch {}
} finally {
    if ($socket -and $socket.State -eq [Net.WebSockets.WebSocketState]::Open) {
        try { Stop-Outputs } catch {}
        $socket.Dispose()
    }
    if ($process -and !$process.HasExited) {
        $process.CloseMainWindow() | Out-Null
        if (!$process.WaitForExit(5000)) { $process.Kill() }
    }
}
