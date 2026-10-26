param(
    [string]$PythonPath,
    [switch]$PrepareOnly
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
$projectDirectory = $PSScriptRoot
$pluginDirectory = Join-Path $projectDirectory 'plugin'
$launcherPath = Join-Path $pluginDirectory 'scripts\launcher.ps1'

function Save-LocalFile([string]$Path, [string]$Content) {
    if (Test-Path -LiteralPath $Path) {
        $oldContent = [IO.File]::ReadAllText($Path)
        if ($oldContent -eq $Content) { return }
        $backupPath = $Path + '.before-install'
        if (-not (Test-Path -LiteralPath $backupPath)) {
            Copy-Item -LiteralPath $Path -Destination $backupPath
        }
    }
    [IO.File]::WriteAllText($Path, $Content, $utf8)
}

$candidates = @()
if ($PythonPath) {
    $candidates += $PythonPath
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) { $candidates += $pythonCommand.Source }
    $python3Command = Get-Command python3 -ErrorAction SilentlyContinue
    if ($python3Command) { $candidates += $python3Command.Source }
}
$pythonExecutable = $null
foreach ($candidate in ($candidates | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $candidate)) { continue }
    try {
        $runtimeProbe = & $candidate -c 'import sys, tkinter; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $runtimeProbe) {
            $pythonExecutable = [string]($runtimeProbe | Select-Object -Last 1)
            break
        }
    } catch { }
}
if (-not $pythonExecutable) {
    throw 'Python with tkinter was not found. Install Python with Tcl/Tk, then rerun with -PythonPath C:\path\python.exe.'
}
$nodeExecutable = $null
$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
if ($nodeCommand) {
    try {
        $nodeProbe = & $nodeCommand.Source -e "if (Number(process.versions.node.split('.')[0]) < 22 || typeof globalThis.WebSocket !== 'function' || typeof globalThis.fetch !== 'function') { process.exit(1); } console.log(process.execPath);" 2>$null
        if ($LASTEXITCODE -eq 0 -and $nodeProbe) {
            $nodeExecutable = [string]($nodeProbe | Select-Object -Last 1)
        }
    } catch { }
}
if (-not $nodeExecutable) {
    throw 'Node.js 22 or newer with global WebSocket and fetch is required. Install Node.js 22 or newer, make node.exe available on PATH, then rerun Install.ps1.'
}
if (-not (Test-Path -LiteralPath (Join-Path $pluginDirectory 'scripts\progress.py'))) {
    throw 'The plugin package is incomplete: scripts\progress.py is missing.'
}
if (-not (Test-Path -LiteralPath (Join-Path $pluginDirectory 'scripts\overlay.py'))) {
    throw 'The plugin package is incomplete: scripts\overlay.py is missing.'
}
foreach ($nativeComponent in @('overlay_wpf.py', 'overlay_wpf.cs', 'overlay_wpf.manifest')) {
    if (-not (Test-Path -LiteralPath (Join-Path $pluginDirectory ('scripts\' + $nativeComponent)))) {
        throw "The native window component is missing: scripts\$nativeComponent"
    }
}
$nativeRuntimeProbe = & $pythonExecutable -c 'import sys; sys.path.insert(0, sys.argv[1]); from overlay_wpf import framework_directory; print(framework_directory())' (Join-Path $pluginDirectory 'scripts') 2>&1
if ($LASTEXITCODE -ne 0) {
    throw 'The native progress window requires the Windows .NET Framework with WPF and its built-in compiler.'
}

$runtimeConfiguration = @{ python = $pythonExecutable; node = $nodeExecutable } | ConvertTo-Json
Save-LocalFile (Join-Path $pluginDirectory 'runtime.json') $runtimeConfiguration
$mcpConfiguration = @{
    mcpServers = @{
        plan_progress = @{
            command = $pythonExecutable
            cwd = $pluginDirectory
            args = @((Join-Path $pluginDirectory 'scripts\progress.py'), 'serve')
            startup_timeout_sec = 10
            tool_timeout_sec = 30
        }
    }
} | ConvertTo-Json -Depth 8
Save-LocalFile (Join-Path $pluginDirectory '.mcp.json') $mcpConfiguration

if ($PrepareOnly) {
    Write-Output "Prepared the plugin with Python: $pythonExecutable"
    Write-Output "Prepared the visible-chat observer with Node.js: $nodeExecutable"
    Write-Output 'No Codex settings or installed plugins were changed.'
    exit 0
}
$codexCommand = Get-Command codex -ErrorAction SilentlyContinue
if (-not $codexCommand) { throw 'Codex CLI was not found on PATH.' }
$codexDirectory = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$codexConfig = Join-Path $codexDirectory 'config.toml'
$configBackup = $codexConfig + '.before-plan-progress'
if ((Test-Path -LiteralPath $codexConfig) -and -not (Test-Path -LiteralPath $configBackup)) {
    Copy-Item -LiteralPath $codexConfig -Destination $configBackup
}
& $codexCommand.Source plugin marketplace add $projectDirectory --json
if ($LASTEXITCODE -ne 0) { throw 'Codex could not register the local marketplace.' }
& $codexCommand.Source plugin add 'codex-plan-progress@codex-plan-progress-local' --json
if ($LASTEXITCODE -ne 0) { throw 'Codex could not install the task-progress plugin.' }

Write-Output 'Installed codex-plan-progress.'
Write-Output 'Codex may load the MCP tools automatically. If they are unavailable, reopen Codex.'
Write-Output 'For automatic hooks, open PowerShell, run codex, then enter /hooks inside the terminal chat and review/trust this plugin.'
Write-Output 'Without hook trust, invoke the plan-progress skill and update progress through its MCP tools.'
Write-Output 'Keep this project folder in place. If you move it, run Install.ps1 from the new location.'
