param(
    [Parameter(Position = 0)]
    [ValidateSet('set-plan', 'set-state', 'show', 'get')]
    [string]$Mode = 'show',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArguments
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
$pluginDirectory = Split-Path -Parent $PSScriptRoot
$portable = Join-Path $pluginDirectory 'runtime\plan-progress.exe'
if (Test-Path -LiteralPath $portable) {
    & $portable $Mode @RemainingArguments
    exit $LASTEXITCODE
}

$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $pythonCommand) { throw 'Python 3.10+ with Tk is required for this source edition.' }
$pythonExecutable = $pythonCommand.Source
& $pythonExecutable -c 'import sys, tkinter; assert sys.version_info >= (3, 10)' 2>$null
if ($LASTEXITCODE -ne 0) { throw 'The selected Python must be version 3.10+ with Tk.' }
if ($Mode -in @('set-plan', 'show')) {
    $nodePath = Join-Path $pluginDirectory 'runtime\node.exe'
    if (-not (Test-Path -LiteralPath $nodePath)) {
        $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
        if (-not $nodeCommand) { throw 'Node.js 22+ is required to follow the visible chat.' }
        $nodePath = $nodeCommand.Source
    }
    & $nodePath -e "process.exit(Number(process.versions.node.split('.')[0]) >= 22 && typeof WebSocket === 'function' && typeof fetch === 'function' ? 0 : 1)" 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Node.js 22+ with WebSocket and fetch is required.' }
}
& $pythonExecutable (Join-Path $PSScriptRoot 'progress.py') $Mode @RemainingArguments
exit $LASTEXITCODE
