@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\mclaw.exe" (
    echo [ERROR] Virtual environment not found: .venv\Scripts\mclaw.exe
    echo         Please run the project setup first ^(pip install -e .^).
    exit /b 1
)

echo Starting Mclaw server on port 18900 ...
echo   Health check : http://localhost:18900/api/health
echo   API docs     : http://localhost:18900/docs
echo.
echo Press Ctrl+C to stop.
echo.

.venv\Scripts\mclaw.exe serve
