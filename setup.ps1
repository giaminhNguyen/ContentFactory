# Cài đặt máy mới:  .\setup.ps1            (hỏi vài thứ cần thiết)
#                   .\setup.ps1 -Yes       (không hỏi, dùng mặc định)   .\setup.ps1 -DryRun   (chỉ in kế hoạch)
# Chạy lại bao nhiêu lần cũng được (idempotent); không ghi đè cấu hình bạn đã chỉnh trong config\config.local.json.
param([switch]$Yes, [switch]$DryRun, [switch]$Force)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
function Find-Python {
    foreach ($c in @("python", "py")) { if (Get-Command $c -ErrorAction SilentlyContinue) { return $c } }
    return $null
}
$py = Find-Python
if (-not $py) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "Chưa có Python: cài Python 3.12 bằng winget..."
        winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
        Write-Host "Đã cài Python. MỞ LẠI terminal này rồi chạy lại .\setup.ps1"; exit 1
    }
    Write-Host "Cần Python >= 3.10 (https://www.python.org/downloads/). Cài rồi chạy lại .\setup.ps1"; exit 1
}
$env:PYTHONPATH = "$root\src;$env:PYTHONPATH"
$env:PYTHONIOENCODING = "utf-8"
$argsList = @("-m", "contentfactory", "--root", $root, "setup")
if ($Yes) { $argsList += "--yes" }
if ($DryRun) { $argsList += "--dry-run" }
if ($Force) { $argsList += "--force" }
if ($py -eq "py") { & py -3 @argsList } else { & $py @argsList }
exit $LASTEXITCODE
