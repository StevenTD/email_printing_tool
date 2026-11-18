$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Waitress = Join-Path $ProjectRoot ".venv\Scripts\waitress-serve.exe"

if (-not (Test-Path $Waitress)) {
    throw "Waitress was not found. Install requirements.txt in .venv first."
}

Set-Location $ProjectRoot
& $Waitress --listen=0.0.0.0:8000 mailprint.wsgi:application
exit $LASTEXITCODE
