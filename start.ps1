# Chạy dịch vụ nền (tự bật daemon upload, đồng bộ pool video, auto resume khi mạng/quota trở lại, dọn dẹp). Ctrl-C để dừng.
# Không bắt buộc cho `cf go` (go tự chạy và tự chờ ngắn); dùng khi muốn xếp hàng nhiều job hoặc để hệ thống tự tiếp tục khi chờ lâu.
$root = $PSScriptRoot
$env:PYTHONPATH = "$root\src;$env:PYTHONPATH"
$env:PYTHONIOENCODING = "utf-8"
$venv = Join-Path $root ".venv\Scripts\python.exe"
$py = if (Test-Path $venv) { $venv } else { "python" }
& $py -m contentfactory --root $root start
exit $LASTEXITCODE
