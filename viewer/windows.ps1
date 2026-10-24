param([string]$Bucket, [string]$Region)
$ErrorActionPreference = 'Stop'
Start-Service AmazonSSMAgent
if (Test-Path C:\viewer\configured.flag) { return }
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
New-Item -ItemType Directory -Force C:\viewer | Out-Null
Start-Transcript C:\viewer\setup.log -Append
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
Start-Process C:\viewer\ssm.exe -ArgumentList '/S' -Wait
Install-Msi 'https://d1uj6qtbmh3dt5.cloudfront.net/nice-dcv-server-x64-Release.msi' 'dcv' 'DISABLE_FIREWALL=1 DISABLE_AUTOMATIC_SESSION_CREATION=1'
$aws = 'C:\Program Files\Amazon\AWSCLIV2\aws.exe'
$env:AWS_DEFAULT_REGION = $Region
$token = Invoke-RestMethod -Method Put -Uri 'http://169.254.169.254/latest/api/token' -Headers @{'X-aws-ec2-metadata-token-ttl-seconds'='21600'}
$Instance = Invoke-RestMethod 'http://169.254.169.254/latest/meta-data/instance-id' -Headers @{'X-aws-ec2-metadata-token'=$token}
@{bucket=$Bucket; region=$Region; instance=$Instance} | ConvertTo-Json | Set-Content C:\viewer\settings.json

Write-Output 'Installing Microsoft runtimes and the Direct3D software renderer.'
Invoke-WebRequest -UseBasicParsing 'https://aka.ms/vs/17/release/vc_redist.x86.exe' -OutFile C:\viewer\vcredist.exe
Start-Process C:\viewer\vcredist.exe -ArgumentList '/install /quiet /norestart' -Wait
Invoke-WebRequest -UseBasicParsing 'https://download.microsoft.com/download/8/4/a/84a35bf1-dafe-4ae8-82af-ad2ae20b6b14/directx_Jun2010_redist.exe' -OutFile C:\viewer\directx.exe
New-Item -ItemType Directory -Force C:\viewer\directx | Out-Null
$p = Start-Process C:\viewer\directx.exe -ArgumentList '/Q /C /T:C:\viewer\directx' -Wait -PassThru
if ($p.ExitCode) { throw 'DirectX extraction failed.' }
$p = Start-Process C:\viewer\directx\DXSETUP.exe -ArgumentList '/silent' -Wait -PassThru
if ($p.ExitCode) { throw 'DirectX installation failed.' }
Invoke-WebRequest -UseBasicParsing 'https://www.7-zip.org/a/7zr.exe' -OutFile C:\viewer\7zr.exe
Invoke-WebRequest -UseBasicParsing 'https://github.com/pal1000/mesa-dist-win/releases/download/26.2.4/mesa3d-26.2.4-release-msvc.7z' -OutFile C:\viewer\mesa.7z
if ((Get-FileHash C:\viewer\mesa.7z -Algorithm SHA256).Hash.ToLower() -ne '351fc8c8b695878ffb3eaa044b3ead08672a48b1a045e3c3e3975811df0f6695') { throw 'Mesa archive checksum mismatch.' }
& C:\viewer\7zr.exe x C:\viewer\mesa.7z -oC:\viewer\mesa -y
if ($LASTEXITCODE) { throw 'Mesa extraction failed.' }
Invoke-WebRequest -UseBasicParsing 'https://sdk.lunarg.com/sdk/download/1.4.363.0/windows/vulkan-runtime-components.zip' -OutFile C:\viewer\vulkan.zip
if ((Get-FileHash C:\viewer\vulkan.zip -Algorithm SHA256).Hash.ToLower() -ne 'a25a927aa8b9f0371048f1861cf88ac3b9bc9b1fb332c42d897c8ab32695769a') { throw 'Vulkan archive checksum mismatch.' }
Expand-Archive C:\viewer\vulkan.zip C:\viewer\vulkan -Force
Invoke-WebRequest -UseBasicParsing 'https://github.com/doitsujin/dxvk/releases/download/v3.1.1/dxvk-3.1.1.tar.gz' -OutFile C:\viewer\dxvk.tar.gz
if ((Get-FileHash C:\viewer\dxvk.tar.gz -Algorithm SHA256).Hash.ToLower() -ne '40565b4a724aadc4433fa4e010b4b23916d9b1f1baeee64e17186db94f54e608') { throw 'DXVK archive checksum mismatch.' }
New-Item -ItemType Directory -Force C:\viewer\dxvk | Out-Null
tar.exe -xf C:\viewer\dxvk.tar.gz -C C:\viewer\dxvk
if ($LASTEXITCODE) { throw 'DXVK extraction failed.' }

Write-Output 'Downloading and verifying the Windows 3.3.5a client (17.7 GB).'
Invoke-WebRequest -UseBasicParsing 'https://github.com/aria2/aria2/releases/download/release-1.37.0/aria2-1.37.0-win-64bit-build1.zip' -OutFile C:\viewer\aria2.zip
Expand-Archive C:\viewer\aria2.zip C:\viewer\aria2 -Force
& $aws s3 cp "s3://$Bucket/viewer/bootstrap/client.torrent" C:\viewer\client.torrent --only-show-errors
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
if (!$exe -or $exe.VersionInfo.FileVersion -notmatch '12340') { throw 'Expected WoW 3.3.5a build 12340.' }
$game = $exe.DirectoryName
Copy-Item C:\viewer\dxvk\dxvk-3.1.1\x32\d3d9.dll $game -Force
Copy-Item C:\viewer\vulkan\VulkanRT-X64-1.4.363.0-Components\x86\vulkan-1.dll $game -Force
New-Item -ItemType File -Force "$game\Wow.exe.local" | Out-Null
$game | Set-Content C:\viewer\game-path.txt
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
SET maxFPS "30"
SET maxFPSbk "15"
SET gxCursor "0"
'@ | Set-Content "$game\WTF\Config.wtf" -Encoding Ascii

# A short-lived desktop user; its random password never leaves this VM.
$password = [Guid]::NewGuid().ToString('N') + '!aA1'
New-LocalUser Viewer -Password (ConvertTo-SecureString $password -AsPlainText -Force) -PasswordNeverExpires | Out-Null
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
Set-ItemProperty $winlogon AutoAdminLogon '1'
Set-ItemProperty $winlogon DefaultUserName 'Viewer'
Set-ItemProperty $winlogon DefaultDomainName $env:COMPUTERNAME
Set-ItemProperty $winlogon DefaultPassword $password

# Access is authenticated by IAM/SSM. The EC2 security group has no ingress.
$dcv = 'Registry::HKEY_USERS\S-1-5-18\Software\GSettings\com\nicesoftware\dcv'
foreach ($key in @('security','session-management','session-management\automatic-console-session')) { New-Item "$dcv\$key" -Force | Out-Null }
Set-ItemProperty "$dcv\security" authentication 'none'
New-ItemProperty "$dcv\session-management" -Name create-session -Value 1 -PropertyType DWord -Force | Out-Null
Set-ItemProperty "$dcv\session-management\automatic-console-session" owner 'Viewer'
Set-ItemProperty "$dcv\session-management\automatic-console-session" permissions-file 'C:\viewer\viewer.perm'
"[permissions]`n%any% allow builtin" | Set-Content C:\viewer\viewer.perm

# The system task owns the private game tunnels and lifetime of the viewer.
@'
$ErrorActionPreference='Continue'
$aws='C:\Program Files\Amazon\AWSCLIV2\aws.exe'
$env:Path += ';C:\Program Files\Amazon\SessionManagerPlugin\bin'
$s=Get-Content C:\viewer\settings.json | ConvertFrom-Json
$env:AWS_DEFAULT_REGION=$s.region
$ready=@{desktop_ready=$true;instance=$s.instance;updated_at=(Get-Date).ToUniversalTime().ToString('o')}
$ready | ConvertTo-Json | Set-Content C:\viewer\ready.json
& $aws s3 cp C:\viewer\ready.json "s3://$($s.bucket)/viewer/$($s.instance)/ready.json" --only-show-errors
$deadline=(Get-Date).AddHours(6)
$current=''; $tunnels=@()
while ((Get-Date) -lt $deadline) {
    & $aws s3 cp "s3://$($s.bucket)/viewer/$($s.instance)/job.json" C:\viewer\job.tmp --only-show-errors 2>$null
    if ($LASTEXITCODE -eq 0) {
        Move-Item C:\viewer\job.tmp C:\viewer\job.json -Force
        $j=Get-Content C:\viewer\job.json | ConvertFrom-Json
        if ($current -ne $j.job_id) {
            $tunnels | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
            $tunnels=@()
            foreach ($port in @(3724,8085)) {
                $sessionArguments="ssm start-session --region $($s.region) --target $($j.game_instance) --document-name agent-wow-game-port --parameters portNumber=$port,localPortNumber=$port"
                $tunnels += Start-Process $aws -ArgumentList $sessionArguments -PassThru -WindowStyle Hidden -RedirectStandardOutput "C:\viewer\tunnel-$port.log" -RedirectStandardError "C:\viewer\tunnel-$port.err"
            }
            $current=$j.job_id
        }
        & $aws s3 cp "s3://$($s.bucket)/viewer/$current/account.json" C:\viewer\account.tmp --only-show-errors 2>$null
        if ($LASTEXITCODE -eq 0) { Move-Item C:\viewer\account.tmp C:\viewer\account.json -Force }
        $state=(& $aws batch describe-jobs --jobs $current --query 'jobs[0].status' --output text 2>$null)
        if ($LASTEXITCODE -eq 0 -and $state -in @('SUCCEEDED','FAILED')) {
            Start-Sleep 120
            Stop-Computer -Force
            exit
        }
    }
    Start-Sleep 15
}
Stop-Computer -Force
'@ | Set-Content C:\viewer\run.ps1

# Launch the full client inside the interactive desktop, never in session zero.
@'
$ErrorActionPreference='Stop'
$env:VK_DRIVER_FILES='C:\viewer\mesa\x86\lvp_icd.x86.json'
$env:DXVK_FILTER_DEVICE_NAME='llvmpipe'
Add-Content C:\viewer\play.log 'Starting the full Windows client.'
$game=(Get-Content C:\viewer\game-path.txt).Trim()
$p=Start-Process "$game\Wow.exe" -WorkingDirectory $game -PassThru
Add-Content C:\viewer\play.log ("Client process: " + $p.Id)
Start-Sleep 45
if ($p.HasExited) {
    Add-Content C:\viewer\play.log ("Client startup exit: " + $p.ExitCode)
    exit 1
}
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class ViewerWindow {
  [DllImport("user32.dll", CharSet=CharSet.Auto)]
  public static extern int GetClassName(IntPtr window, StringBuilder text, int count);
}
"@
$p.Refresh()
$windowClass=New-Object System.Text.StringBuilder 128
[ViewerWindow]::GetClassName($p.MainWindowHandle,$windowClass,128) | Out-Null
Add-Content C:\viewer\play.log ("Client window class: " + $windowClass.ToString())
if ($windowClass.ToString() -ne 'GxWindowClassD3d') {
    Add-Content C:\viewer\play.log 'Client startup needs a visual check; automatic login skipped.'
    exit 1
}
$shell=New-Object -ComObject WScript.Shell
while (!(Test-Path C:\viewer\account.json)) {
    if ($p.HasExited) { exit 1 }
    Start-Sleep 5
}
$account=Get-Content C:\viewer\account.json | ConvertFrom-Json
if ($shell.AppActivate($p.Id)) {
    # With a remembered account, the client's initial focus is the password box.
    $shell.SendKeys($account.password)
    $shell.SendKeys('{ENTER}')
}
'@ | Set-Content C:\viewer\play.ps1

$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\viewer\run.ps1'
Register-ScheduledTask 'AgentWowTunnels' -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) -User SYSTEM -RunLevel Highest -Force | Out-Null
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\viewer\play.ps1'
$principal=New-ScheduledTaskPrincipal -UserId Viewer -LogonType Interactive
Register-ScheduledTask 'AgentWowClient' -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn -User Viewer) -Principal $principal -Force | Out-Null
& icacls.exe C:\viewer /grant 'Viewer:(OI)(CI)M' /T /Q | Out-Null
Write-Output 'Windows viewer setup complete; rebooting to activate graphics and the desktop.'
New-Item -ItemType File -Force C:\viewer\configured.flag | Out-Null
Stop-Transcript
& $aws s3 cp C:\viewer\setup.log "s3://$Bucket/viewer/$Instance/setup.log" --only-show-errors
Restart-Computer -Force
