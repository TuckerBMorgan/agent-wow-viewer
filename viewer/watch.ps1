# Interactive spectator camera control through mod-watch's public chat command.
$ErrorActionPreference='Stop'
$aws='C:\Program Files\Amazon\AWSCLIV2\aws.exe'
$s=Get-Content C:\viewer\settings.json | ConvertFrom-Json
$env:AWS_DEFAULT_REGION=$s.region
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class WatchWindow {
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr window, int command);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr window);
}
'@
$shell=New-Object -ComObject WScript.Shell
while ((Get-Content C:\viewer\play.log -Raw -ErrorAction SilentlyContinue) -notmatch 'Character entry requested\.') { Start-Sleep 5 }
Start-Sleep 30
$current=''
while (Get-Process Wow -ErrorAction SilentlyContinue) {
    try {
        $ErrorActionPreference='Continue'
        & $aws s3 cp "s3://$($s.bucket)/viewer/$($s.instance)/watch-request.json" C:\viewer\watch-request.tmp --only-show-errors 2>$null
        $downloaded=$LASTEXITCODE -eq 0
    } finally { $ErrorActionPreference='Stop' }
    if ($downloaded) {
        $request=Get-Content C:\viewer\watch-request.tmp | ConvertFrom-Json
        if ($request.id -ne $current) {
            $current=$request.id
            $status=@{request_id=$current;character=$request.character;updated_at=(Get-Date).ToUniversalTime().ToString('o')}
            try {
                if ($request.character -cnotmatch '^[A-Za-z]{2,12}$') { throw 'Invalid character name.' }
                if (!$request.expires_at -or [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -gt $request.expires_at) { throw 'Watch request expired.' }
                $game=Get-Process Wow | Select-Object -First 1
                for ($focusAttempt=0; $focusAttempt -lt 5; $focusAttempt++) {
                    $game.Refresh()
                    [WatchWindow]::ShowWindowAsync($game.MainWindowHandle,9) | Out-Null
                    $shell.AppActivate($game.Id) | Out-Null
                    [WatchWindow]::SetForegroundWindow($game.MainWindowHandle) | Out-Null
                    Start-Sleep 1
                    $game.Refresh()
                    if ([WatchWindow]::GetForegroundWindow() -eq $game.MainWindowHandle -and $game.MainWindowHandle -ne [IntPtr]::Zero) { break }
                }
                if ($game.MainWindowHandle -eq [IntPtr]::Zero -or [WatchWindow]::GetForegroundWindow() -ne $game.MainWindowHandle) { throw 'The game is not the foreground window.' }
                $shell.SendKeys('{ENTER}')
                $shell.SendKeys('.watch ' + $request.character)
                $shell.SendKeys('{ENTER}')
                $status.state='submitted'
            } catch { $status.state='failed'; $status.error=$_.Exception.Message }
            $status | ConvertTo-Json | Set-Content C:\viewer\watch-status.json
            & $aws s3 cp C:\viewer\watch-status.json "s3://$($s.bucket)/viewer/$($s.instance)/watch-status.json" --only-show-errors
        }
    }
    Start-Sleep 5
}
