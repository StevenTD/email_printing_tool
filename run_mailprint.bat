@echo off
setlocal

for %%I in ("%~dp0.") do set "PROJECT_ROOT=%%~fI"
set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo Python was not found at "%PYTHON%".
    echo Create the project virtual environment before starting MailPrint.
    exit /b 1
)

if /i "%~1"=="--web" goto run_web
if /i "%~1"=="--worker" goto run_worker
if /i "%~1"=="--install-startup" goto install_startup
if /i "%~1"=="--remove-startup" goto remove_startup
if /i "%~1"=="--help" goto usage
if not "%~1"=="" goto usage_error

start "MailPrint Web" "%ComSpec%" /k ""%~f0" --web"
start "MailPrint Worker" "%ComSpec%" /k ""%~f0" --worker"
echo Started MailPrint Web and MailPrint Worker in separate command windows.
exit /b 0

:run_web
pushd "%PROJECT_ROOT%"
"%PYTHON%" manage.py runserver 0.0.0.0:8000 --noreload
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%

:run_worker
pushd "%PROJECT_ROOT%"
"%PYTHON%" manage.py run_worker
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%

:install_startup
set "MAILPRINT_LAUNCHER=%~f0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$startup = [Environment]::GetFolderPath('Startup'); $shortcutPath = Join-Path $startup 'MailPrint.lnk'; $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath); $shortcut.TargetPath = $env:MAILPRINT_LAUNCHER; $shortcut.WorkingDirectory = $env:PROJECT_ROOT; $shortcut.Save()"
if errorlevel 1 (
    echo Could not add MailPrint to the Windows Startup folder.
    exit /b 1
)
echo MailPrint will start after this Windows user signs in.
exit /b 0

:remove_startup
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$shortcutPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'MailPrint.lnk'; if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath; Write-Output 'Removed MailPrint from the Windows Startup folder.' } else { Write-Output 'MailPrint is not in the Windows Startup folder.' }"
if errorlevel 1 exit /b 1
exit /b 0

:usage
echo Usage:
echo   run_mailprint.bat                 Start web server and worker
echo   run_mailprint.bat --install-startup  Start both after Windows sign-in
echo   run_mailprint.bat --remove-startup   Remove the Startup shortcut
exit /b 0

:usage_error
call "%~f0" --help
exit /b 2
