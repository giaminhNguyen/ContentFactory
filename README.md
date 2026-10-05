# ContentFactory

Pipeline biến một nguồn truyện/video thành 1 video YouTube và N video TikTok. Thiết kế: `HANDOFF.md`; audit và quyết định: `docs/`.

Trạng thái: **Phase 1** — orchestrator lõi (SQLite, state machine, queue theo stage, checkpoint, retry, resume) chạy với **fake adapter**. Chưa có TTS/Story/Render/Upload thật.

```powershell
# Python >= 3.10, không cần cài gói ngoài
python scripts/run_fake_job.py --temp          # chạy 1 fake job trong thư mục tạm
python -m unittest discover -s tests -t .      # chạy test (~13 s)

# dùng CLI trên thư mục gốc repo (tạo workspace/, output/, runtime/ — đã gitignore)
$env:PYTHONPATH = "src"
python -m contentfactory submit --input "https://youtu.be/x" --set made_for_kids=false
python -m contentfactory run                   # chạy tới khi hết việc; Ctrl-C để dừng êm, chạy lại sẽ resume
python -m contentfactory status
python -m contentfactory retry <job_id>        # job FAILED: chạy lại đúng stage lỗi
```

Log có cấu trúc: `runtime/logs/orchestrator.jsonl` và `workspace/job_<id>/job.log.jsonl`. Manifest từng job: `workspace/job_<id>/manifest.json`.
