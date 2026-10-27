# Windows + WSL only: registers a logon task that keeps the bot running in the background (no window), then starts it.
# Run from the repo folder:  powershell -ExecutionPolicy Bypass -File .\install-task.ps1
# Remove with: Unregister-ScheduledTask -TaskName agent-wallet-tg -Confirm:$false
$repo = (& wsl.exe -e wslpath -a "$PSScriptRoot").Trim()
$action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\conhost.exe" `
  -Argument "--headless $env:WINDIR\System32\wsl.exe -e bash $repo/run.sh"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName agent-wallet-tg -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName agent-wallet-tg
Get-ScheduledTask -TaskName agent-wallet-tg | Select-Object TaskName, State
