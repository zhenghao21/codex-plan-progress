param(
    [string]$PythonExecutable = 'python.exe',
    [string]$NodeExecutable = 'node.exe',
    [Parameter(Mandatory = $true)]
    [string]$NodeLicenseFile
)

$ErrorActionPreference = 'Stop'
$stage = Join-Path $PSScriptRoot 'plugin'
$runtime = Join-Path $stage 'runtime'
$build = Join-Path $PSScriptRoot 'build'
if (Test-Path -LiteralPath $runtime) { throw 'runtime already exists; inspect the earlier build before replacing it.' }
foreach ($sourceName in @('overlay_wpf.py', 'overlay_wpf.cs', 'overlay_wpf.manifest')) {
    if (-not (Test-Path -LiteralPath (Join-Path $stage "scripts\$sourceName") -PathType Leaf)) {
        throw "Missing WPF source: scripts/$sourceName. Sync the release sources before building."
    }
}
if (-not (Test-Path -LiteralPath $NodeLicenseFile -PathType Leaf)) { throw 'Supply the license accompanying the Node.js runtime being redistributed.' }
$nodeCommand = Get-Command $NodeExecutable -ErrorAction Stop
& $PythonExecutable -c 'import PyInstaller, tkinter, sys; assert sys.version_info >= (3, 10)'
if ($LASTEXITCODE -ne 0) { throw 'Use an existing Python 3.10+ environment with Tk and PyInstaller. No dependency was installed.' }
& $nodeCommand.Source -e "process.exit(Number(process.versions.node.split('.')[0]) >= 22 ? 0 : 1)"
if ($LASTEXITCODE -ne 0) { throw 'Node.js 22+ is required.' }
New-Item -ItemType Directory -Path $runtime,$build -Force | Out-Null
& $PythonExecutable -m PyInstaller --noconfirm --clean --onefile --console --name plan-progress --distpath $runtime --workpath (Join-Path $build 'cli') --specpath $build (Join-Path $stage 'scripts\progress.py')
if ($LASTEXITCODE -ne 0) { throw 'CLI build failed; runtime is incomplete and must not be submitted.' }
& $PythonExecutable -m PyInstaller --noconfirm --clean --onefile --windowed --name plan-progress-window --paths (Join-Path $stage 'scripts') --hidden-import overlay_wpf --distpath $runtime --workpath (Join-Path $build 'window') --specpath $build (Join-Path $stage 'scripts\overlay.py')
if ($LASTEXITCODE -ne 0) { throw 'Window build failed; runtime is incomplete and must not be submitted.' }
Copy-Item -LiteralPath $nodeCommand.Source -Destination (Join-Path $runtime 'node.exe')
Copy-Item -LiteralPath $NodeLicenseFile -Destination (Join-Path $runtime 'NODE-LICENSE.txt')
Write-Output 'Portable executables created. Keep scripts/overlay_wpf.cs and scripts/overlay_wpf.manifest beside the package; WPF uses the existing Windows .NET Framework 4.8+. This is not a submitted or verified release; inspect bundled Python/Tcl/Tk licenses and test on a clean Windows installation before redistribution.'
