@echo off
REM clickthis.bat - open the Grafana dashboard windows on Windows and place them.
REM
REM This opens and positions windows only. The watchdog half of this project
REM (blank-page detection, reload, auto-scroll) is macOS-only - it drives Chrome
REM through AppleScript, which Windows does not have. Use clickthis.command there.

setlocal
cd /d "%~dp0"

where powershell >nul 2>&1
if errorlevel 1 (
  echo PowerShell was not found on this machine, so the windows cannot be placed.
  echo Open the dashboard manually, or install PowerShell.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0open-wall.ps1" %*
set RC=%ERRORLEVEL%

if not "%RC%"=="0" echo.& echo Finished with errors (exit code %RC%).
pause
exit /b %RC%
