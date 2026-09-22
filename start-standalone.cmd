@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
title File Sync - Standalone
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\deploy.ps1" -Mode standalone -Auto
set "SyncExitCode=%errorlevel%"
if not "%SyncExitCode%"=="0" (
  echo.
  echo Startup failed. Please read the error above.
  pause
)
exit /b %SyncExitCode%
