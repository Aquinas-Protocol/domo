# install_broker.ps1 — Register domo-elevation-broker as a Windows service via NSSM
# Run from an ELEVATED PowerShell (right-click PowerShell -> Run as Administrator).
#
# What this does:
#   1. Registers a Windows service named "domo-elevation-broker"
#   2. Points it at the venv's python.exe with broker/elevation_broker.py
#   3. Runs it as LocalSystem so it has Administrator privileges without UAC
#   4. Sets it to start automatically at boot
#   5. Logs to data\broker\service.log (with rotation)
#   6. Starts the service
#
# Pair with: install_service.ps1 (the domo main bot service).
# The two services share data\sessions.sqlite — the bot writes elevation_requests
# rows in `pending`/`approved`; the broker reads them and executes after approval.
#
# To uninstall:
#   Stop-Service domo-elevation-broker
#   & $nssm remove domo-elevation-broker confirm

$ErrorActionPreference = "Stop"

$nssm        = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python      = Join-Path $projectRoot ".venv\Scripts\python.exe"
$brokerPy    = Join-Path $projectRoot "broker\elevation_broker.py"
$logDir      = Join-Path $projectRoot "data\broker"
$logFile     = Join-Path $logDir "service.log"
$serviceName = "domo-elevation-broker"

# Sanity checks
if (-not (Test-Path $nssm))     { throw "nssm.exe not found at $nssm" }
if (-not (Test-Path $python))   { throw "venv python not found at $python" }
if (-not (Test-Path $brokerPy)) { throw "broker entry not found at $brokerPy" }
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an ELEVATED PowerShell (Run as Administrator)."
}

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

Write-Host "Registering service '$serviceName'..." -ForegroundColor Cyan

if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
    Write-Host "  existing service found - stopping and removing" -ForegroundColor Yellow
    Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue
    cmd /c "`"$nssm`" remove $serviceName confirm" | Out-Null
}

# Create the service
& $nssm install $serviceName $python $brokerPy

# Service-level config
& $nssm set $serviceName AppDirectory     $projectRoot
& $nssm set $serviceName DisplayName      "Domo Elevation Broker"
& $nssm set $serviceName Description      "Elevated executor for Discord-approved admin commands. Receives request UUIDs over a named pipe from domo; runs the canonical command from elevation_requests."
& $nssm set $serviceName Start            SERVICE_AUTO_START
& $nssm set $serviceName AppStdout        $logFile
& $nssm set $serviceName AppStderr        $logFile
& $nssm set $serviceName AppRotateFiles   1
& $nssm set $serviceName AppRotateOnline  1
& $nssm set $serviceName AppRotateBytes   5242880     # 5 MB
& $nssm set $serviceName AppStdoutCreationDisposition 4
& $nssm set $serviceName AppStderrCreationDisposition 4
& $nssm set $serviceName AppExit          Default Restart
& $nssm set $serviceName AppRestartDelay  5000
& $nssm set $serviceName AppThrottle      10000
& $nssm set $serviceName AppStopMethodSkip      0
& $nssm set $serviceName AppStopMethodConsole   5000
& $nssm set $serviceName AppStopMethodWindow    5000
& $nssm set $serviceName AppStopMethodThreads   5000
& $nssm set $serviceName AppKillProcessTree     1

# LocalSystem — always elevated, no UAC. This is the whole point of the broker.
# Do NOT change ObjectName to a user account; the bot service handles user-context
# work. The broker's only job is to be elevated for the brief windows it executes.
& $nssm set $serviceName ObjectName       LocalSystem

# Start it
& $nssm start $serviceName

Start-Sleep -Seconds 3
$state = (& $nssm status $serviceName).Trim()
Write-Host ""
Write-Host "Service state: $state" -ForegroundColor Green
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor Cyan
Write-Host "  Get-Service $serviceName"
Write-Host "  Restart-Service $serviceName"
Write-Host "  Stop-Service $serviceName; & '$nssm' remove $serviceName confirm   # to uninstall"
Write-Host ""
Write-Host "Tail the broker log: Get-Content '$logFile' -Wait -Tail 50"
Write-Host "Internal broker log:  Get-Content '$logDir\broker.log' -Wait -Tail 50"
