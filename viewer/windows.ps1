param([string]$Bucket, [string]$Region, [string]$Recipe)
$ErrorActionPreference = 'Stop'
Start-Service AmazonSSMAgent
if (Test-Path C:\viewer\configured.flag) { return }
$ProgressPreference = 'SilentlyContinue'
Start-Transcript C:\viewer\setup.log -Append
$image = Get-Content C:\viewer\image.json | ConvertFrom-Json
if ($image.recipe -ne $Recipe -or $image.client_build -ne 12340) { throw 'Viewer image does not match this checkout; rebuild with --build-client.' }
if ($image.renderer -ne 'nvidia') { throw 'The viewer requires the GPU client image.' }
$game = (Get-Content C:\viewer\game-path.txt).Trim()
if ((Get-Item "$game\Wow.exe").VersionInfo.FilePrivatePart -ne 12340) { throw 'Expected WoW 3.3.5a build 12340.' }
$aws = 'C:\Program Files\Amazon\AWSCLIV2\aws.exe'
$env:AWS_DEFAULT_REGION = $Region
$token = Invoke-RestMethod -Method Put -Uri 'http://169.254.169.254/latest/api/token' -Headers @{'X-aws-ec2-metadata-token-ttl-seconds'='21600'}
$Instance = Invoke-RestMethod 'http://169.254.169.254/latest/meta-data/instance-id' -Headers @{'X-aws-ec2-metadata-token'=$token}
@{bucket=$Bucket; region=$Region; instance=$Instance; recipe=$Recipe} | ConvertTo-Json | Set-Content C:\viewer\settings.json
$stop = New-ScheduledTaskAction -Execute shutdown.exe -Argument '/s /f /t 0'
Register-ScheduledTask 'ViewerSafetyStop' -Action $stop -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(6)) -User SYSTEM -RunLevel Highest -Force | Out-Null

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
# Debugger disconnection must not lock an unattended broadcast desktop.
New-ItemProperty "$dcv\security" -Name os-auto-lock -Value 0 -PropertyType DWord -Force | Out-Null
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
# DCV display management requires the system task, even for the session owner.
for ($i=0; $i -lt 30; $i++) {
    & 'C:\Program Files\NICE\DCV\Server\bin\dcv.exe' set-display-layout --session console '1920x1080' >C:\viewer\display.log 2>C:\viewer\display.err
    if ($LASTEXITCODE -eq 0) { New-Item -ItemType File -Force C:\viewer\display-ready.flag | Out-Null; break }
    Start-Sleep 2
}
# The desktop task confirms that the client has a working Direct3D window.
for ($attempt=0; $attempt -lt 120; $attempt++) {
    if (Test-Path C:\viewer\client-ready.flag) { break }
    Start-Sleep 5
}
if ($attempt -eq 120) {
    @{error='Client desktop did not become ready; inspect play.log.'} | ConvertTo-Json | Set-Content C:\viewer\failed.json
    & $aws s3 cp C:\viewer\failed.json "s3://$($s.bucket)/viewer/$($s.instance)/failed.json" --only-show-errors
    exit 1
}
$ready=@{desktop_ready=$true;instance=$s.instance;recipe=$s.recipe;updated_at=(Get-Date).ToUniversalTime().ToString('o')}
# A recovered startup supersedes the previous failure marker in S3.
[IO.File]::WriteAllText('C:\viewer\failed.json','null')
& $aws s3 cp C:\viewer\failed.json "s3://$($s.bucket)/viewer/$($s.instance)/failed.json" --only-show-errors
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
Add-Content C:\viewer\play.log 'Starting the full Windows client.'
for ($i=0; $i -lt 60 -and !(Test-Path C:\viewer\display-ready.flag); $i++) { Start-Sleep 2 }
if (!(Test-Path C:\viewer\display-ready.flag)) { throw 'Desktop display did not become ready; inspect display.err.' }
$game=(Get-Content C:\viewer\game-path.txt).Trim()
$config=Get-Content "$game\WTF\Config.wtf" | Where-Object {$_ -notmatch '^SET (accountName|gxResolution|showTutorials|Sound_EnableSoundWhenGameIsInBG) '}
@($config; 'SET accountName "WATCHER"'; 'SET gxResolution "1280x720"'; 'SET showTutorials "0"'; 'SET Sound_EnableSoundWhenGameIsInBG "1"') | Set-Content "$game\WTF\Config.wtf" -Encoding Ascii
$p=Get-Process Wow -ErrorAction SilentlyContinue | Where-Object {$_.SessionId -eq (Get-Process -Id $PID).SessionId} | Select-Object -First 1
if (!$p) { $p=Start-Process "$game\Wow.exe" -WorkingDirectory $game -PassThru }
Add-Content C:\viewer\play.log ("Client process: " + $p.Id)
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class ViewerWindow {
  [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left, Top, Right, Bottom; }
  [StructLayout(LayoutKind.Sequential)] public struct Point { public int X, Y; }
  [DllImport("user32.dll", CharSet=CharSet.Auto)]
  public static extern int GetClassName(IntPtr window, StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr window, out Rect rect);
  [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr window, ref Point point);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint x, uint y, uint data, UIntPtr extra);
  public static bool FocusPassword(IntPtr window) {
    if (GetForegroundWindow() != window) return false;
    Rect rect;
    if (!GetClientRect(window, out rect) || rect.Right < 640 || rect.Bottom < 480) return false;
    // The 3.3.5 login form scales with the client area; avoid relying on tab focus.
    Point point = new Point { X = rect.Right / 2, Y = (int)(rect.Bottom * 0.615) };
    if (!ClientToScreen(window, ref point)) return false;
    if (!SetCursorPos(point.X, point.Y)) return false;
    mouse_event(0x0002, 0, 0, 0, UIntPtr.Zero);
    mouse_event(0x0004, 0, 0, 0, UIntPtr.Zero);
    return true;
  }
}
"@
# The first GPU desktop can take several minutes to create its window.
# Inspect readiness instead of treating a single early sample as failure.
$windowDeadline=(Get-Date).AddMinutes(5)
do {
    $p.Refresh()
    if ($p.HasExited) {
        Add-Content C:\viewer\play.log ("Client startup exit: " + $p.ExitCode)
        exit 1
    }
    $windowClass=New-Object System.Text.StringBuilder 128
    [ViewerWindow]::GetClassName($p.MainWindowHandle,$windowClass,128) | Out-Null
    if ($windowClass.ToString() -eq 'GxWindowClassD3d') { break }
    Start-Sleep 5
} while ((Get-Date) -lt $windowDeadline)
Add-Content C:\viewer\play.log ("Client window class: " + $windowClass.ToString())
if ($windowClass.ToString() -ne 'GxWindowClassD3d') {
    Add-Content C:\viewer\play.log 'Client startup needs a visual check; automatic login skipped.'
    exit 1
}
New-Item -ItemType File -Force C:\viewer\client-ready.flag | Out-Null
$shell=New-Object -ComObject WScript.Shell
while (!(Test-Path C:\viewer\account.json)) {
    if ($p.HasExited) { exit 1 }
    Start-Sleep 5
}
$account=Get-Content C:\viewer\account.json | ConvertFrom-Json
for ($attempt=0; $attempt -lt 30; $attempt++) {
    $portsReady=$true
    foreach ($port in @(3724,8085)) {
        $tcp=[Net.Sockets.TcpClient]::new()
        try { $tcp.Connect('127.0.0.1',$port) } catch { $portsReady=$false } finally { $tcp.Dispose() }
    }
    if ($portsReady) { break }
    Start-Sleep 1
}
if (!$portsReady) { throw 'Game tunnels did not become ready.' }
if ($shell.AppActivate($p.Id)) {
    Start-Sleep 1
    if (![ViewerWindow]::FocusPassword($p.MainWindowHandle)) { throw 'Could not focus the game password field.' }
    Start-Sleep -Milliseconds 250
    $shell.SendKeys('^a')
    $shell.SendKeys($account.password)
    $shell.SendKeys('{ENTER}')
    Add-Content C:\viewer\play.log 'Spectator login submitted.'
    # The isolated WATCHER account has exactly one character. Enter its character
    # selection after authentication so a broadcast does not need a DCV operator.
    Start-Sleep 30
    if (!$p.HasExited -and $shell.AppActivate($p.Id)) { Start-Sleep 1; $shell.SendKeys('{ENTER}'); Add-Content C:\viewer\play.log 'Character entry requested.' }
} else {
    throw 'Could not focus the game for automatic login; check whether the desktop is locked.'
}
'@ | Set-Content C:\viewer\play.ps1

$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\viewer\run.ps1'
Register-ScheduledTask 'AgentWowTunnels' -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) -User SYSTEM -RunLevel Highest -Force | Out-Null
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\viewer\play.ps1'
$principal=New-ScheduledTaskPrincipal -UserId Viewer -LogonType Interactive
Register-ScheduledTask 'AgentWowClient' -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn -User Viewer) -Principal $principal -Force | Out-Null
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\viewer\obs.ps1'
Register-ScheduledTask 'AgentWowOBS' -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn -User Viewer) -Principal $principal -Force | Out-Null
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File C:\viewer\watch.ps1'
Register-ScheduledTask 'AgentWowWatch' -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn -User Viewer) -Principal $principal -Force | Out-Null
& icacls.exe C:\viewer /grant 'Viewer:(OI)(CI)M' /T /Q | Out-Null
Write-Output 'Viewer configured from the saved image; rebooting into its private desktop.'
New-Item -ItemType File -Force C:\viewer\configured.flag | Out-Null
Stop-Transcript
& $aws s3 cp C:\viewer\setup.log "s3://$Bucket/viewer/$Instance/setup.log" --only-show-errors
Restart-Computer -Force
