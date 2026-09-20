@echo off
cd /d "%~dp0"

set "PY=C:\ProgramData\miniconda3\python.exe"
if not exist "%PY%" set "PY=C:\Users\%USERNAME%\miniconda3\python.exe"
if not exist "%PY%" set "PY=C:\Users\%USERNAME%\anaconda3\python.exe"

if not exist "%PY%" (
    echo ERROR: Python not found. Install miniconda3.
    pause
    exit /b 1
)

echo ========================================
echo   Stock Agent Server
echo   URL: http://localhost:5000
echo   Press Ctrl+C to stop.
echo ========================================
echo.

REM Create a temp VBScript to delay browser opening (no PowerShell needed)
echo Set ws = WScript.CreateObject("WScript.Shell") > "%TEMP%\open_browser.vbs"
echo WScript.Sleep 4000 >> "%TEMP%\open_browser.vbs"
echo ws.Run "http://localhost:5000" >> "%TEMP%\open_browser.vbs"
start "" /b wscript.exe "%TEMP%\open_browser.vbs"

REM Start server in foreground
"%PY%" api_server.py --port 5000

pause
