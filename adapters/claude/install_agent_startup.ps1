<#
.SYNOPSIS
  Registers (or removes) a Windows Scheduled Task that starts agent.py automatically at sign-in.

.DESCRIPTION
  Runs the outbound relay agent windowless (pythonw.exe from adapters/claude/.venv) whenever the
  current user signs in. It is a sign-in task, not a pre-login service, on purpose: SolidWorks is a
  desktop COM application and only works inside an interactive user session. For true unattended
  boot, enable Windows auto-logon for this account and the task starts straight after it.

  The task restarts the agent if it exits (e.g. after the "different protocol" SystemExit), has no
  run-time limit, and keeps running on battery. Output goes to adapter.log (adapter_log.py).

  No admin rights needed: the task is registered for the current user only.

.EXAMPLE
  .\install_agent_startup.ps1              # install / update, and start it now
  .\install_agent_startup.ps1 -NoStart     # install only
  .\install_agent_startup.ps1 -Uninstall   # remove
#>
param(
    [switch]$Uninstall,
    [switch]$NoStart,
    [string]$TaskName = "SolidPilot Agent"
)

$ErrorActionPreference = "Stop"
$here = $PSScriptRoot

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    return
}

$pythonw = Join-Path $here ".venv\Scripts\pythonw.exe"
$agent   = Join-Path $here "agent.py"
$envFile = Join-Path $here ".env"
if (-not (Test-Path $pythonw)) { throw "Missing $pythonw. Create the venv first: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt" }
if (-not (Test-Path $agent))   { throw "Missing $agent." }
if (-not (Test-Path $envFile)) { throw "Missing $envFile. Copy .env.agent.example to .env and fill in GATEWAY_URL, AGENT_CLIENT_ID, AGENT_CLIENT_SECRET." }

$user    = "$env:USERDOMAIN\$env:USERNAME"
$action  = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$agent`"" -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = "PT30S"   # let the network and shell settle before connecting out
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "Starts the SolidPilot outbound relay agent (adapters/claude/agent.py) at sign-in." | Out-Null
Write-Host "Registered scheduled task '$TaskName' (runs at sign-in of $user)."

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started it now. Watch adapter.log for '[agent] connected to gateway'."
}
