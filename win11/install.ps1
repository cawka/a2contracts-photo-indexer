# win11\install.ps1 interval|daemon|remove -- registers the Task Scheduler
# task "A2 photo indexer" (replacing any earlier one) for the current user.
#   interval: `run --once` every 30 minutes; a missed run starts as soon as possible
#   daemon:   `run` always on from logon, restarted if it exits (models stay loaded)
# Output goes to %USERPROFILE%\a2-photo-indexer.log.
param([Parameter(Mandatory)][ValidateSet('interval', 'daemon', 'remove')][string]$Mode)
$ErrorActionPreference = 'Stop'
$Name = 'A2 photo indexer'
$Repo = Split-Path -Parent $PSScriptRoot
$Exe = Join-Path $Repo '.venv\Scripts\a2-photo-indexer.exe'
$Log = Join-Path $env:USERPROFILE 'a2-photo-indexer.log'

Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
if ($Mode -eq 'remove') { "removed '$Name'"; return }
if (-not (Test-Path $Exe)) { throw "$Exe not found -- create the venv and pip install first" }

$runArgs = if ($Mode -eq 'interval') { 'run --once' } else { 'run' }
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -WorkingDirectory $Repo `
    -Argument "/c `"`"$Exe`" $runArgs --api https://contracts.a2cons.com >> `"$Log`" 2>&1`""

if ($Mode -eq 'interval') {
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 12) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
}
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $Name
"installed '$Name' ($Mode); log: Get-Content -Wait `"$Log`""
