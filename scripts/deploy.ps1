# 单机版自动部署，保留 standalone 参数兼容旧命令
param(
    [ValidateSet('standalone')][string]$Mode = 'standalone',
    [switch]$SkipInstall,
    [switch]$PrepareOnly,
    [switch]$Auto,
    [string]$PythonPath,
    [ValidateSet('direct', 'venv')][string]$Environment,
    [string]$PipSource
)
$ErrorActionPreference = 'Stop'
$SyncRoot = Split-Path -Parent $PSScriptRoot
$SyncArguments = @((Join-Path $SyncRoot 'scripts/deploy.py'), $Mode)
if ($SkipInstall) { $SyncArguments += '--skip-install' }
if ($PrepareOnly) { $SyncArguments += '--prepare-only' }
if ($Auto) { $SyncArguments += '--auto' }
if ($PipSource) { $SyncArguments += @('--pip-source', $PipSource) }
# 显式解释器优先；配置路径相对于工程，而不是当前工作目录。
if (-not $PythonPath) { $PythonPath = $env:SYNC_PYTHON }
if (-not $PythonPath) {
    $SyncSection = ''
    Get-Content -LiteralPath (Join-Path $SyncRoot 'env/install.ini') -Encoding UTF8 | ForEach-Object {
        $SyncLine = $_.Trim()
        if ($SyncLine -match '^\[(.+)\]$') { $SyncSection = $Matches[1] }
        elseif ($SyncSection -eq 'runtime' -and $SyncLine -match '^python\s*=\s*(.*)$') { $PythonPath = $Matches[1].Trim() }
    }
}
if ($Environment) { $SyncArguments += @('--environment', $Environment) }
if ($PythonPath) {
    if (-not [IO.Path]::IsPathRooted($PythonPath)) { $PythonPath = Join-Path $SyncRoot $PythonPath }
    if (Test-Path -LiteralPath $PythonPath -PathType Container) { $PythonPath = Join-Path $PythonPath 'python.exe' }
    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        Write-Host "Python not found: $PythonPath"
        exit 1
    }
    $SyncArguments += @('--python', $PythonPath)
    & $PythonPath @SyncArguments
} elseif (Test-Path -LiteralPath (Join-Path $SyncRoot 'python/python.exe')) {
    & (Join-Path $SyncRoot 'python/python.exe') @SyncArguments
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 @SyncArguments
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python @SyncArguments
} else {
    Write-Host 'Set [runtime] python in env/install.ini to your Python executable or directory.'
    exit 1
}
exit $LASTEXITCODE
