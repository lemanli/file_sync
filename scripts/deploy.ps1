# 单机版自动部署，保留 standalone 参数兼容旧命令
param(
    [ValidateSet('standalone')][string]$Mode = 'standalone',
    [switch]$SkipInstall,
    [switch]$PrepareOnly,
    [switch]$Auto,
    [string]$PipSource
)
$ErrorActionPreference = 'Stop'
$SyncRoot = Split-Path -Parent $PSScriptRoot
$SyncArguments = @((Join-Path $SyncRoot 'scripts/deploy.py'), $Mode)
if ($SkipInstall) { $SyncArguments += '--skip-install' }
if ($PrepareOnly) { $SyncArguments += '--prepare-only' }
if ($Auto) { $SyncArguments += '--auto' }
if ($PipSource) { $SyncArguments += @('--pip-source', $PipSource) }
$SyncPython = Join-Path $SyncRoot ('.venv-' + $Mode + '/Scripts/python.exe')
$SyncLocalReady = $false
if (Test-Path $SyncPython) {
    try {
        & $SyncPython -c "import sys,venv; assert sys.version_info >= (3,10)" 2>$null
        $SyncLocalReady = ($LASTEXITCODE -eq 0)
    } catch {
        $SyncLocalReady = $false
    }
}
if ($SyncLocalReady) {
    & $SyncPython @SyncArguments
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 @SyncArguments
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python @SyncArguments
} else {
    Write-Host 'Python was not found. Install Python 3.12 (64-bit) and run this launcher again.'
    exit 1
}
exit $LASTEXITCODE
