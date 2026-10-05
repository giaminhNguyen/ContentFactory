# Cập nhật:  .\update.ps1   (git pull, đưa module về đúng modules.lock, cài lại dependency khi đổi, migration DB, doctor)
param([switch]$Yes, [switch]$DryRun, [switch]$Force)
$root = $PSScriptRoot
$env:PYTHONPATH = "$root\src;$env:PYTHONPATH"
$env:PYTHONIOENCODING = "utf-8"
$venv = Join-Path $root ".venv\Scripts\python.exe"
$py = if (Test-Path $venv) { $venv } else { "python" }
$a = @("-m", "contentfactory", "--root", $root, "update")
if ($Yes) { $a += "--yes" }
if ($DryRun) { $a += "--dry-run" }
if ($Force) { $a += "--force" }
& $py @a
exit $LASTEXITCODE
