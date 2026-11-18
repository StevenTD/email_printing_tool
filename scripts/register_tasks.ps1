param(
    [string]$TaskPrefix = "MailPrint"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$WebScript = Join-Path $PSScriptRoot "run_web.ps1"
$WorkerScript = Join-Path $PSScriptRoot "run_worker.ps1"
$PowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$UserId = "$env:USERDOMAIN\$env:USERNAME"

foreach ($ScriptPath in @($WebScript, $WorkerScript)) {
    if (-not (Test-Path $ScriptPath)) {
        throw "Startup script not found: $ScriptPath"
    }
}

$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$Settings = New-ScheduledTaskSettingsSet `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable
$Principal = New-ScheduledTaskPrincipal `
    -UserId $UserId `
    -LogonType Interactive `
    -RunLevel Limited

$WebAction = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$WebScript`"" `
    -WorkingDirectory $ProjectRoot
$WorkerAction = New-ScheduledTaskAction `
    -Execute $PowerShell `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$WorkerScript`"" `
    -WorkingDirectory $ProjectRoot

Register-ScheduledTask `
    -TaskName "$TaskPrefix-Web" `
    -Action $WebAction `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "MailPrint Waitress web application" `
    -Force | Out-Null
Register-ScheduledTask `
    -TaskName "$TaskPrefix-Worker" `
    -Action $WorkerAction `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "MailPrint Gmail and print worker" `
    -Force | Out-Null

Write-Host "Registered $TaskPrefix-Web and $TaskPrefix-Worker for $UserId."
