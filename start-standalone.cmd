@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
title File Sync - Standalone
set "APP_ROOT=%~dp0"

rem 优先使用环境变量，其次读取安装配置中的 python 路径。
if defined SYNC_PYTHON goto selected
for /f "usebackq tokens=1,* delims==" %%A in ("%APP_ROOT%env\install.ini") do (
    for /f "tokens=1" %%K in ("%%A") do if /i "%%K"=="python" (
        for /f "tokens=*" %%P in ("%%B") do set "SYNC_PYTHON=%%P"
    )
)
if defined SYNC_PYTHON goto selected
if exist "%APP_ROOT%python\python.exe" (
    set "SYNC_PYTHON=%APP_ROOT%python\python.exe"
    goto selected
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%APP_ROOT%scripts\deploy.py" %*
    goto finished
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%APP_ROOT%scripts\deploy.py" %*
    goto finished
)
echo Python not found. Set python in env/install.ini.
set "SyncExitCode=1"
goto failed

:selected
rem 相对路径从工程目录解析；同时接受目录或 python.exe。
pushd "%APP_ROOT%"
if errorlevel 1 (
    set "SyncExitCode=1"
    goto failed
)
if exist "%SYNC_PYTHON%\python.exe" set "SYNC_PYTHON=%SYNC_PYTHON%\python.exe"
if not exist "%SYNC_PYTHON%" (
    echo Python not found: "%SYNC_PYTHON%"
    popd
    set "SyncExitCode=1"
    goto failed
)
"%SYNC_PYTHON%" "%APP_ROOT%scripts\deploy.py" %*
set "SyncExitCode=%errorlevel%"
popd
goto checked

:finished
set "SyncExitCode=%errorlevel%"
:checked
if "%SyncExitCode%"=="0" exit /b 0
:failed
echo.
echo Startup failed. Please read the error above.
pause
exit /b %SyncExitCode%
