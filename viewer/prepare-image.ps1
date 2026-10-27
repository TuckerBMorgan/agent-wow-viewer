# Install once on a disposable builder, then generalize before capturing an AMI.
param([string]$Bucket, [string]$Region, [string]$Recipe, [switch]$Finalize)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
New-Item -ItemType Directory -Force C:\viewer | Out-Null
Start-Service AmazonSSMAgent
Start-Transcript C:\viewer\image-build.log -Append
$aws = 'C:\Program Files\Amazon\AWSCLIV2\aws.exe'
$env:AWS_DEFAULT_REGION = $Region
$token = Invoke-RestMethod -Method Put -Uri 'http://169.254.169.254/latest/api/token' -Headers @{'X-aws-ec2-metadata-token-ttl-seconds'='21600'}
$Instance = Invoke-RestMethod 'http://169.254.169.254/latest/meta-data/instance-id' -Headers @{'X-aws-ec2-metadata-token'=$token}
$prefix = "viewer/image-build/$Instance"

function Save-Status($Status, $Message = '') {
    @{status=$Status; recipe=$Recipe; instance=$Instance; error=$Message} | ConvertTo-Json | Set-Content C:\viewer\build-status.json
    # AWS CLI cannot read an active Windows transcript; release its file handle first.
    Stop-Transcript -ErrorAction SilentlyContinue | Out-Null
    try {
        & $aws s3 cp C:\viewer\build-status.json "s3://$Bucket/$prefix/status.json" --only-show-errors
        if ($LASTEXITCODE) { throw 'Could not upload image build status.' }
        if (Test-Path C:\viewer\image-build.log) {
            & $aws s3 cp C:\viewer\image-build.log "s3://$Bucket/$prefix/setup.log" --only-show-errors
            if ($LASTEXITCODE) { throw 'Could not upload image build log.' }
        }
    } finally {
        Start-Transcript C:\viewer\image-build.log -Append | Out-Null
    }
}

try {
    if ($Finalize) {
        $game = (Get-Content C:\viewer\game-path.txt).Trim()
        if ((Get-Item "$game\Wow.exe").VersionInfo.FilePrivatePart -ne 12340) { throw 'Expected WoW 3.3.5a build 12340.' }
        foreach ($path in @($aws, 'C:\Program Files\Amazon\SessionManagerPlugin\bin\session-manager-plugin.exe',
                            'C:\viewer\obs\bin\64bit\obs64.exe')) {
            if (!(Test-Path $path)) { throw "Missing image dependency: $path" }
        }
        if (!(Get-Service dcvserver -ErrorAction SilentlyContinue)) { throw 'Amazon DCV is not installed.' }
        if (Get-LocalUser Viewer -ErrorAction SilentlyContinue) { throw 'An image must not contain a viewer account.' }
        foreach ($path in @('settings.json', 'account.json', 'job.json', 'configured.flag')) {
            if (Test-Path "C:\viewer\$path") { throw "Image contains runtime state: $path" }
        }
        if (!(Get-CimInstance Win32_VideoController | Where-Object Name -Match 'NVIDIA')) { throw 'NVIDIA GPU driver is not ready.' }
        if (Test-Path "$game\d3d9.dll") { throw 'Software Direct3D shim must not be installed.' }
        @{recipe=$Recipe; client_version='3.3.5a'; client_build=12340; renderer='nvidia'; obs_version='33.0.0'} | ConvertTo-Json | Set-Content C:\viewer\image.json
        Unregister-ScheduledTask 'ViewerImageFinalize' -Confirm:$false
        Unregister-ScheduledTask 'ViewerSafetyStop' -Confirm:$false
        Save-Status 'prepared'
        Stop-Transcript
        Remove-Item C:\viewer\build-status.json, C:\viewer\image-build.log, C:\viewer\prepare-image.ps1 -Force
        # EC2Launch resets its state so a clone executes its own user data.
        # https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/sysprep-using-ec2launchv2.html
        & 'C:\Program Files\Amazon\EC2Launch\EC2Launch.exe' sysprep --shutdown=true
        if ($LASTEXITCODE) { throw 'EC2Launch Sysprep failed.' }
        exit
    }
    $stop = New-ScheduledTaskAction -Execute shutdown.exe -Argument '/s /f /t 0'
    Register-ScheduledTask 'ViewerSafetyStop' -Action $stop -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(6)) -User SYSTEM -RunLevel Highest -Force | Out-Null

function Install-Msi($Url, $Name, $Extra = '') {
    Invoke-WebRequest -UseBasicParsing $Url -OutFile "C:\viewer\$Name.msi"
    $p = Start-Process msiexec.exe -ArgumentList "/i C:\viewer\$Name.msi /qn /norestart $Extra" -Wait -PassThru
    if ($p.ExitCode -notin @(0, 1638, 3010)) { throw "$Name installation failed ($($p.ExitCode))." }
}

Write-Output 'Installing AWS connection tools and Amazon DCV.'
Install-Msi 'https://awscli.amazonaws.com/AWSCLIV2.msi' 'awscli'
Invoke-WebRequest -UseBasicParsing 'https://s3.amazonaws.com/session-manager-downloads/plugin/latest/windows/SessionManagerPluginSetup.exe' -OutFile C:\viewer\ssm.exe
$p = Start-Process C:\viewer\ssm.exe -ArgumentList '/S' -Wait -PassThru
if ($p.ExitCode -notin @(0, 3010)) { throw 'Session Manager plugin installation failed.' }
Install-Msi 'https://d1uj6qtbmh3dt5.cloudfront.net/nice-dcv-server-x64-Release.msi' 'dcv' 'DISABLE_FIREWALL=1 DISABLE_AUTOMATIC_SESSION_CREATION=1'
$aws = 'C:\Program Files\Amazon\AWSCLIV2\aws.exe'
Write-Output 'Installing Microsoft runtimes, NVIDIA GRID and OBS Studio.'
Invoke-WebRequest -UseBasicParsing 'https://aka.ms/vs/17/release/vc_redist.x86.exe' -OutFile C:\viewer\vcredist.exe
$p = Start-Process C:\viewer\vcredist.exe -ArgumentList '/install /quiet /norestart' -Wait -PassThru
if ($p.ExitCode -notin @(0, 1638, 3010)) { throw 'Visual C++ runtime installation failed.' }
Invoke-WebRequest -UseBasicParsing 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile C:\viewer\vcredist64.exe
$p = Start-Process C:\viewer\vcredist64.exe -ArgumentList '/install /quiet /norestart' -Wait -PassThru
if ($p.ExitCode -notin @(0, 1638, 3010)) { throw 'Visual C++ x64 runtime installation failed.' }
Invoke-WebRequest -UseBasicParsing 'https://download.microsoft.com/download/8/4/a/84a35bf1-dafe-4ae8-82af-ad2ae20b6b14/directx_Jun2010_redist.exe' -OutFile C:\viewer\directx.exe
New-Item -ItemType Directory -Force C:\viewer\directx | Out-Null
$p = Start-Process C:\viewer\directx.exe -ArgumentList '/Q /C /T:C:\viewer\directx' -Wait -PassThru
if ($p.ExitCode) { throw 'DirectX extraction failed.' }
$p = Start-Process C:\viewer\directx\DXSETUP.exe -ArgumentList '/silent' -Wait -PassThru
if ($p.ExitCode) { throw 'DirectX installation failed.' }
& $aws s3 cp "s3://$Bucket/viewer/bootstrap/596.86__grid_win10_win11_server2022_server2025_dch_64bit_international_aws_swl.exe" C:\viewer\nvidia-grid.exe --only-show-errors
if ($LASTEXITCODE) { throw 'NVIDIA GRID download failed.' }
$sig = Get-AuthenticodeSignature C:\viewer\nvidia-grid.exe
if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'NVIDIA') { throw 'Unexpected NVIDIA installer signature.' }
$p = Start-Process C:\viewer\nvidia-grid.exe -ArgumentList '-s -noreboot' -Wait -PassThru
if ($p.ExitCode -notin @(0, 3010)) { throw 'NVIDIA GRID installation failed.' }
Invoke-WebRequest -UseBasicParsing 'https://github.com/obsproject/obs-studio/releases/download/33.0.0/OBS-Studio-33.0.0-Windows-x64.zip' -OutFile C:\viewer\obs.zip
if ((Get-FileHash C:\viewer\obs.zip -Algorithm SHA256).Hash.ToLower() -ne '3369a3ecd67b5669a8a08d77357f87cc786d1fba42fbbd1054d841a69f6271a7') { throw 'OBS archive checksum mismatch.' }
Expand-Archive C:\viewer\obs.zip C:\viewer\obs -Force
# All OBS profiles and credentials are created per instance, never in the AMI.
if (Test-Path C:\viewer\obs\config) { throw 'The base image contains an OBS profile.' }

if (!(Test-Path C:\viewer\game-path.txt)) {
Write-Output 'Downloading and verifying the Windows 3.3.5a client (17.7 GB).'
Invoke-WebRequest -UseBasicParsing 'https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip' -OutFile C:\viewer\aria2.zip
Expand-Archive C:\viewer\aria2.zip C:\viewer\aria2 -Force
& $aws s3 cp "s3://$Bucket/viewer/bootstrap/$Recipe/client.torrent" C:\viewer\client.torrent --only-show-errors
if ($LASTEXITCODE) { throw 'Client torrent metadata download failed.' }
if ((Get-FileHash C:\viewer\client.torrent -Algorithm SHA256).Hash.ToLower() -ne '58731d43da3e615fbeb489ebd7a9401a8b56eeed8b7baf72f6b57646c574f62e') { throw 'Client torrent metadata checksum mismatch.' }
$aria = (Get-ChildItem C:\viewer\aria2 -Recurse -Filter aria2c.exe | Select-Object -First 1).FullName
& $aria --torrent-file=C:\viewer\client.torrent --index-out=1=client.zip --dir=C:\viewer --seed-time=0 --max-upload-limit=8K --check-integrity=true --file-allocation=none --auto-file-renaming=false --allow-overwrite=true --max-connection-per-server=16 --split=16 --summary-interval=30 --enable-color=false 'https://btground.dedyn.io/chmi/ChromieCraft_3.3.5a.zip'
if ($LASTEXITCODE) { throw 'Client download failed.' }
if ((Get-Item C:\viewer\client.zip).Length -ne 17674749792) { throw 'Unexpected client archive size.' }
Write-Output 'Extracting the client.'
New-Item -ItemType Directory -Force C:\viewer\client | Out-Null
& tar.exe -xf C:\viewer\client.zip -C C:\viewer\client
if ($LASTEXITCODE) { throw 'Client extraction failed.' }
$exe = Get-ChildItem C:\viewer\client -Recurse -Filter Wow.exe | Select-Object -First 1
if (!$exe -or $exe.VersionInfo.FilePrivatePart -ne 12340) { throw 'Expected WoW 3.3.5a build 12340.' }
$game = $exe.DirectoryName
$game | Set-Content C:\viewer\game-path.txt
} else {
    $game = (Get-Content C:\viewer\game-path.txt).Trim()
    if ((Get-Item "$game\Wow.exe").VersionInfo.FilePrivatePart -ne 12340) { throw 'Expected WoW 3.3.5a build 12340.' }
}
# Native Direct3D on the T4; remove shims when upgrading an older CPU client image.
foreach ($name in @('d3d9.dll', 'vulkan-1.dll', 'Wow.exe.local')) {
    Remove-Item "$game\$name" -Force -ErrorAction SilentlyContinue
}
Get-ChildItem "$game\Data" -Recurse -Filter realmlist.wtf | ForEach-Object { 'set realmlist 127.0.0.1' | Set-Content $_.FullName -Encoding Ascii }
New-Item -ItemType Directory -Force "$game\WTF" | Out-Null
@'
SET locale "enUS"
SET realmList "127.0.0.1"
SET accountName "WATCHER"
SET gxWindow "1"
SET gxResolution "1280x720"
SET gxApi "D3D9"
SET movie "0"
SET readTOS "1"
SET readEULA "1"
SET readScanning "-1"
SET readContest "-1"
SET gameTip "0"
SET accounttype "LK"
SET maxFPS "60"
SET maxFPSbk "60"
SET gxCursor "0"
'@ | Set-Content "$game\WTF\Config.wtf" -Encoding Ascii


# Keep extracted runtime files, but discard downloads before snapshotting.
foreach ($pattern in @('client.zip','client.torrent','aria2*','*.msi','ssm.exe','vcredist*.exe','directx*',
                       '7zr.exe','mesa*','vulkan*','dxvk*','obs.zip','nvidia-grid.exe')) {
    Get-ChildItem "C:\viewer\$pattern" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
}
Save-Status 'installed'
# Complete pending installer reboots before Sysprep. No desktop user is created here.
$arguments = "-NoProfile -ExecutionPolicy Bypass -File C:\viewer\prepare-image.ps1 -Bucket $Bucket -Region $Region -Recipe $Recipe -Finalize"
$action = New-ScheduledTaskAction -Execute powershell.exe -Argument $arguments
Register-ScheduledTask 'ViewerImageFinalize' -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) -User SYSTEM -RunLevel Highest -Force | Out-Null
Stop-Transcript
Restart-Computer -Force
} catch {
    $message = $_.Exception.Message
    Write-Output "Image build failed: $message"
    if (Test-Path $aws) {
        try { Save-Status 'failed' $message } catch { Write-Output $_ }
    }
    Stop-Transcript -ErrorAction SilentlyContinue
    Stop-Computer -Force
    throw $message
}
