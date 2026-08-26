# capture-logs.ps1
#
# Run this from the self-healing-ops-mock-service project root, in a SEPARATE
# terminal from the one running `mvn spring-boot:run`. Requires the FILE_JSON
# appender added to logback-spring.xml (writes to logs/mock-service.log).
#
# For each of the 10 FailureMode values, this:
#   1. resets all failure state
#   2. records the current line count of the log file
#   3. activates the mode
#   4. generates the kind of traffic that mode actually needs to fire
#   5. slices out only the NEW lines and saves them to captured-logs/<mode>.log
#   6. deactivates the mode
#
# Caveat: this assumes POST /admin/failures/reset also clears simulated
# memory usage, not just the active-mode set. If memory_leak / oom_kill
# capture looks off (e.g. threshold crossed instantly), restart the service
# between those two captures instead of relying on reset.

$Base    = "http://localhost:8080"
$LogFile = "logs/mock-service.log"
$OutDir  = "captured-logs"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function Send-Burst {
    # Fires $Count requests essentially simultaneously by starting all the
    # async calls before waiting on any of them - needed for modes that key
    # off concurrent in-flight requests or requests-per-window, where a
    # simple sequential loop never produces real concurrency.
    #
    # Builds the task list explicitly via .Add() rather than relying on
    # `$tasks = for (...) {...}` output capture, which can come back $null
    # in some PowerShell setups and crash WaitAll with ArgumentNullException.
    param([string]$Url, [int]$Count)
    Add-Type -AssemblyName System.Net.Http -ErrorAction SilentlyContinue
    $client = New-Object System.Net.Http.HttpClient
    $tasks = [System.Collections.Generic.List[System.Threading.Tasks.Task]]::new()
    for ($i = 0; $i -lt $Count; $i++) {
        $tasks.Add($client.GetAsync($Url))
    }
    if ($tasks.Count -eq 0) {
        Write-Warning "Send-Burst: no requests were queued (Count=$Count) - skipping"
        $client.Dispose()
        return
    }
    [System.Threading.Tasks.Task]::WaitAll($tasks.ToArray())
    $client.Dispose()
}

function Send-Sequential {
    param([string]$Url, [int]$Count)
    1..$Count | ForEach-Object {
        try { Invoke-RestMethod -Uri $Url -TimeoutSec 10 | Out-Null } catch {}
    }
}

function Capture-Mode {
    param(
        [string]$Mode,
        [scriptblock]$TrafficAction,
        [int]$SettleSeconds = 2
    )
    Write-Host "== $Mode =="
    Invoke-RestMethod -Method Post -Uri "$Base/admin/failures/reset" | Out-Null
    Start-Sleep -Seconds 1

    $before = 0
    if (Test-Path $LogFile) { $before = (Get-Content $LogFile).Count }

    Invoke-RestMethod -Method Post -Uri "$Base/admin/failures/$Mode/activate" | Out-Null
    & $TrafficAction
    Start-Sleep -Seconds $SettleSeconds

    $lines = Get-Content $LogFile
    $captured = if ($lines.Count -gt $before) { $lines[$before..($lines.Count - 1)] } else { @() }
    $captured | Set-Content "$OutDir\$Mode.log"

    Invoke-RestMethod -Method Post -Uri "$Base/admin/failures/$Mode/deactivate" | Out-Null
    Write-Host "  captured $($captured.Count) line(s) -> $OutDir\$Mode.log"
}

$orderUrl = "$Base/api/orders/1"

# --- request-driven modes: a modest sequential burst is enough to see the pattern ---
#Capture-Mode -Mode "connection_pool_exhaustion" -TrafficAction { Send-Sequential -Url $orderUrl -Count 30 }
#Capture-Mode -Mode "db_deadlock"                -TrafficAction { Send-Sequential -Url $orderUrl -Count 30 }
#Capture-Mode -Mode "slow_downstream_dependency" -TrafficAction { Send-Sequential -Url $orderUrl -Count 5 }
#Capture-Mode -Mode "disk_full"                  -TrafficAction { Send-Sequential -Url $orderUrl -Count 30 }
#Capture-Mode -Mode "bad_deploy_error_spike"     -TrafficAction { Send-Sequential -Url $orderUrl -Count 30 }
#Capture-Mode -Mode "config_drift"               -TrafficAction { Send-Sequential -Url $orderUrl -Count 30 }

# --- concurrency-driven modes: need a genuine simultaneous burst ---
Capture-Mode -Mode "thread_pool_exhaustion" -TrafficAction { Send-Burst -Url $orderUrl -Count 100 }
#Capture-Mode -Mode "retry_storm"            -TrafficAction { Send-Burst -Url $orderUrl -Count 60 }

# --- time-driven modes: let the background simulator run to the 1024MB threshold ---
# ~40MB every 5s -> roughly 130s to cross 1024MB from a clean reset
#Capture-Mode -Mode "memory_leak" -TrafficAction { Start-Sleep -Seconds 140 } -SettleSeconds 0
#Capture-Mode -Mode "oom_kill"    -TrafficAction { Start-Sleep -Seconds 140 } -SettleSeconds 0

Invoke-RestMethod -Method Post -Uri "$Base/admin/failures/reset" | Out-Null
Write-Host "`nDone. One file per failure mode in $OutDir\"