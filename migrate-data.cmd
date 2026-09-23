@echo off
setlocal
rem 按 application.yml 准备数据，复用启动器中的 Python 和安装配置。
call "%~dp0start-standalone.cmd" --migrate-only %*
set "SyncExitCode=%errorlevel%"
if "%SyncExitCode%"=="0" pause
exit /b %SyncExitCode%
