$ErrorActionPreference = 'Stop'
$codexCommand = Get-Command codex -ErrorAction SilentlyContinue
if (-not $codexCommand) { throw 'Codex CLI was not found on PATH.' }

& $codexCommand.Source plugin remove 'codex-plan-progress@codex-plan-progress-local'
if ($LASTEXITCODE -ne 0) { throw 'Codex could not remove the task-progress plugin.' }
& $codexCommand.Source plugin marketplace remove 'codex-plan-progress-local'
if ($LASTEXITCODE -ne 0) { throw 'The plugin was removed, but Codex could not remove its local marketplace.' }

Write-Output 'Removed codex-plan-progress. Restart Codex and close the floating window if it is still open.'
Write-Output 'Your saved progress remains in %LOCALAPPDATA%\CodexPlanProgress. Project files were kept.'
