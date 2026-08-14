@echo off
setlocal
cd /d "%~dp0"
set "PORT=18900"

set "FOUND="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
    echo [stop] Mclaw process PID %%a
    taskkill /F /PID %%a >nul 2>&1
    set "FOUND=1"
)

if defined FOUND (
    echo Mclaw stopped.
) else (
    echo Mclaw is not running - port %PORT% has no listener.
)
