# ContentFactory launcher (PowerShell):  .\cf.ps1 go "<URL YouTube>" --channel <kenh> --open
$root = $PSScriptRoot
$env:PYTHONPATH = "$root\src;$env:PYTHONPATH"
$env:PYTHONIOENCODING = "utf-8"
$venv = Join-Path $root ".venv\Scripts\python.exe"
$py = if (Test-Path $venv) { $venv } else { "python" }
& $py -m contentfactory --root $root @args
exit $LASTEXITCODE
