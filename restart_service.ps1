#Requires -Version 5.1

# Self-elevate if not already admin
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`""
    )
    exit
}

$nssmPid = (Get-WmiObject Win32_Service -Filter "Name='domo'").ProcessId
if ($nssmPid -gt 0) { taskkill /F /T /PID $nssmPid 2>$null }
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'src\.bot' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 12
Start-Service domo
Start-Sleep -Seconds 8
Get-Service domo

Read-Host "`nPress Enter to close"
