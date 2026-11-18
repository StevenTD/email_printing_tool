$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    throw "Python was not found. Create the .venv environment first."
}

Set-Location $ProjectRoot
& $Python manage.py run_worker
exit $LASTEXITCODE
