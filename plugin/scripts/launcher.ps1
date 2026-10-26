param(
    [Parameter(Position = 0)]
    [ValidateSet('hook', 'serve', 'set-plan', 'show', 'get')]
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
$runtimeFile = Join-Path $pluginDirectory 'runtime.json'
$pythonExecutable = $null

if (Test-Path -LiteralPath $runtimeFile) {
    $runtime = Get-Content -LiteralPath $runtimeFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($runtime.python -and (Test-Path -LiteralPath $runtime.python)) {
        $pythonExecutable = $runtime.python
    }
}
if (-not $pythonExecutable) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) { $pythonExecutable = $pythonCommand.Source }
}
if (-not $pythonExecutable) {
    throw 'Python is unavailable. Run Install.ps1 again to configure the progress plugin.'
}

$entrypoint = Join-Path $PSScriptRoot 'progress.py'
if ($Mode -eq 'hook') {
    # Forward the JSON explicitly: nested PowerShell wrappers can swallow native stdin.
    $hookInput = [Console]::In.ReadToEnd()
    $hookInput | & $pythonExecutable $entrypoint hook
} else {
    & $pythonExecutable $entrypoint $Mode @RemainingArguments
}
exit $LASTEXITCODE
