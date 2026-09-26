# win11\indexer.ps1 -- day-to-day control of the installed Task Scheduler worker
# (install it once with win11\install.ps1 interval|daemon). From the repo:
#
#   powershell -ExecutionPolicy Bypass -File win11\indexer.ps1 update    git pull; reinstall only if pyproject.toml changed; restart
#   powershell -ExecutionPolicy Bypass -File win11\indexer.ps1 restart   stop the running worker and start it again (new code, models reload)
#   powershell -ExecutionPolicy Bypass -File win11\indexer.ps1 stop      stop it; stays stopped until `start` (daemon: or the next logon)
#   powershell -ExecutionPolicy Bypass -File win11\indexer.ps1 start     start it again after `stop`
#   powershell -ExecutionPolicy Bypass -File win11\indexer.ps1 status    running or not, and the last lines of the log
#   powershell -ExecutionPolicy Bypass -File win11\indexer.ps1 log       follow the log (Ctrl-C to leave)
param([Parameter(Mandatory)][ValidateSet('update', 'restart', 'stop', 'start', 'status', 'log')][string]$Command)
$ErrorActionPreference = 'Stop'
$Name = 'A2 photo indexer'
$Repo = Split-Path -Parent $PSScriptRoot
$Log = Join-Path $env:USERPROFILE 'a2-photo-indexer.log'
# The extras pip installs on the NVIDIA box (see README "Set up").
$Extras = if ($env:A2_INDEXER_EXTRAS) { $env:A2_INDEXER_EXTRAS } else { 'cuda,ocr' }

function Get-Task { Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue }

function Get-IndexerProcesses {
    # The task runs cmd.exe -> a2-photo-indexer.exe -> python; match the
    # launcher's path. Never this script's own PowerShell.
    Get-CimInstance Win32_Process |
        Where-Object { $_.CommandLine -like '*a2-photo-indexer*' -and $_.ProcessId -ne $PID }
}

function Stop-Indexer {
    Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    # Ending the task ends its cmd.exe, not always the indexer under it.
    Get-IndexerProcesses | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Assert-Installed {
    if (-not (Get-Task)) { throw "not installed -- run: powershell -ExecutionPolicy Bypass -File win11\install.ps1 daemon   (or: interval)" }
}

switch ($Command) {
    'update' {
        Assert-Installed
        Push-Location $Repo
        try {
            $before = (git rev-parse HEAD).Trim()
            git pull --ff-only
            if ($LASTEXITCODE -ne 0) { throw 'git pull failed' }
            $after = (git rev-parse HEAD).Trim()
            if ($before -eq $after) {
                'already up to date'
            } else {
                git log --oneline "$before..$after"
                git diff --quiet $before $after -- pyproject.toml
                if ($LASTEXITCODE -eq 0) {
                    'dependencies unchanged -- no reinstall needed'
                } else {
                    'pyproject.toml changed -- reinstalling'
                    & (Join-Path $Repo '.venv\Scripts\pip.exe') install -e ".[$Extras]"
                    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
                }
            }
        } finally { Pop-Location }
        Stop-Indexer
        Start-ScheduledTask -TaskName $Name
        'restarted -- the models load again (a minute or two); use "log" to watch'
    }
    'restart' {
        Assert-Installed
        Stop-Indexer
        Start-ScheduledTask -TaskName $Name
        'restarted -- the models load again (a minute or two); use "log" to watch'
    }
    'stop' {
        Assert-Installed
        Stop-Indexer
        'stopped (until "start"; a daemon also comes back at the next logon)'
    }
    'start' {
        Assert-Installed
        if (Get-IndexerProcesses) { 'already running' } else { Start-ScheduledTask -TaskName $Name; 'started' }
    }
    'status' {
        $task = Get-Task
        if (-not $task) {
            'not installed'
        } else {
            $procs = @(Get-IndexerProcesses | Where-Object { $_.Name -like 'python*' -or $_.Name -like 'a2-photo-indexer*' })
            $info = Get-ScheduledTaskInfo -TaskName $Name
            if ($procs.Count -gt 0) {
                $started = ($procs | Sort-Object CreationDate | Select-Object -First 1).CreationDate
                "running, since $started (task: $($task.State))"
            } else {
                "not running (task: $($task.State), last run $($info.LastRunTime), next $($info.NextRunTime))"
            }
        }
        if (Test-Path $Log) {
            '--- last log lines:'
            # Progress bars (model loading) are one huge `r-joined line: drop them.
            (Get-Content $Log -Tail 200) -split "`r" |
                Where-Object { $_.Trim() -and $_ -notmatch 'it/s\]' } |
                Select-Object -Last 8
        }
    }
    'log' {
        Get-Content $Log -Tail 40 -Wait
    }
}
