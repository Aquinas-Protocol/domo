# install_service.ps1 - Register domo as a Windows service via NSSM
# Run from an ELEVATED PowerShell (right-click PowerShell -> Run as Administrator).
#
# What this does:
#   1. Registers a Windows service named "domo"
#   2. Points it at the venv's python.exe with `-m src.bot`
#   3. Redirects stdout/stderr to data\runtime.log (with rotation)
#   4. Sets the service to start automatically at boot
#   5. Prompts for your Windows password and sets the service to run under
#      YOUR user account - required so it can read ~/.claude/.credentials.json
#      (subscription OAuth) and ~/.claude/projects/<...>/ (memory)
#   6. Starts the service
#
# To uninstall: Stop-Service domo; & $nssm remove domo confirm

$ErrorActionPreference = "Stop"

$nssm        = "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"
$projectRoot = $PSScriptRoot
$python      = Join-Path $projectRoot ".venv\Scripts\python.exe"
$logFile     = Join-Path $projectRoot "data\runtime.log"
$serviceName = "domo"

# Sanity checks
if (-not (Test-Path $nssm))   { throw "nssm.exe not found at $nssm" }
if (-not (Test-Path $python)) { throw "venv python not found at $python" }
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "This script must be run from an ELEVATED PowerShell (Run as Administrator)."
}

Write-Host "Registering service '$serviceName'..." -ForegroundColor Cyan

# Remove any prior registration (clean re-runs).
# Check existence first; nssm prints to stderr when the service is absent
# and PowerShell turns that into a script-halting error under ErrorActionPreference=Stop.
if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
    Write-Host "  existing service found - stopping and removing" -ForegroundColor Yellow
    Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue
    cmd /c "`"$nssm`" remove $serviceName confirm" | Out-Null
}

# Create the service
& $nssm install $serviceName $python "-m" "src.bot"

# Service-level config
& $nssm set $serviceName AppDirectory     $projectRoot
& $nssm set $serviceName DisplayName      "Domo (Claude Code bridge)"
& $nssm set $serviceName Description      "Domo - Discord-fronted Claude Code assistant: operator + per-thread sub-agents over the second-brain vault"
& $nssm set $serviceName Start            SERVICE_AUTO_START
& $nssm set $serviceName AppStdout        $logFile
& $nssm set $serviceName AppStderr        $logFile
& $nssm set $serviceName AppRotateFiles   1
& $nssm set $serviceName AppRotateOnline  1
& $nssm set $serviceName AppRotateBytes   10485760    # 10 MB
& $nssm set $serviceName AppStdoutCreationDisposition 4
& $nssm set $serviceName AppStderrCreationDisposition 4
& $nssm set $serviceName AppExit          Default Restart
& $nssm set $serviceName AppRestartDelay  5000
& $nssm set $serviceName AppThrottle      10000
# Bound how long NSSM waits for graceful shutdown before TerminateProcess.
# Without this, a stuck SDK subprocess can pin the service in STOP_PENDING.
& $nssm set $serviceName AppStopMethodSkip      0
& $nssm set $serviceName AppStopMethodConsole   5000
& $nssm set $serviceName AppStopMethodWindow    5000
& $nssm set $serviceName AppStopMethodThreads   5000
& $nssm set $serviceName AppKillProcessTree     1

# User-account credentials - load-bearing for subscription OAuth.
# Service must run as the user, not LocalSystem, so it can read ~/.claude/.credentials.json
$account  = ".\$env:USERNAME"
$securePw = Read-Host -AsSecureString "Enter the Windows password for $account"
$plain    = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePw)
)
& $nssm set $serviceName ObjectName $account $plain
$plain = $null  # let GC drop it

# Start it
& $nssm start $serviceName

Start-Sleep -Seconds 3
$state = (& $nssm status $serviceName).Trim()
Write-Host ""
Write-Host "Service state: $state" -ForegroundColor Green
Write-Host ""
Write-Host "Useful commands going forward:" -ForegroundColor Cyan
Write-Host "  Get-Service $serviceName"
Write-Host "  Restart-Service $serviceName"
Write-Host "  & '$nssm' edit $serviceName     # GUI editor"
Write-Host "  Stop-Service $serviceName; & '$nssm' remove $serviceName confirm   # to uninstall"
Write-Host ""
Write-Host "Tail the log: Get-Content '$logFile' -Wait -Tail 50"
