# ContentFactory

Pipeline biến một nguồn truyện/video thành 1 video YouTube và N video TikTok. Thiết kế: `HANDOFF.md`; audit và quyết định: `docs/`.

Trạng thái: **Phase 2 (+ tích hợp Subtitle_supperVip)** — orchestrator lõi (SQLite, state machine, queue theo stage, checkpoint, retry, resume) + **Source thật** (`SourceAdapter` nhiều provider: Subtitle_supperVip chính, yt-dlp dự phòng → Transcript Processor) + **Story** (`StoryBranchAdapter` điều khiển oh-story, Story Assembler). TTS/Render/Upload vẫn là fake. Story **chưa được kiểm chứng với LLM thật** (xem `docs/DECISIONS.md` D-23).

```powershell
# Python >= 3.10, không cần cài gói ngoài để chạy test
python scripts/run_fake_job.py --temp          # chạy 1 fake job trong thư mục tạm
python -m unittest discover -s tests -t .      # chạy test (~17 s)

# dùng CLI trên thư mục gốc repo (tạo workspace/, output/, runtime/ — đã gitignore)
$env:PYTHONPATH = "src"
python -m contentfactory submit --input "https://youtu.be/x" --set made_for_kids=false
python -m contentfactory run                   # chạy tới khi hết việc; Ctrl-C để dừng êm, chạy lại sẽ resume
python -m contentfactory status
python -m contentfactory retry <job_id>        # job FAILED: chạy lại đúng stage lỗi
```

## Source + Story thật (Phase 2)

Cần: Python env có `youtube-transcript-api` cho Subtitle_supperVip (`--supervip-python`, hoặc `modules/Subtitle_supperVip/backend/.venv`), `yt-dlp` cho fallback (`pip install yt-dlp`), Claude Code CLI đã đăng nhập, Node 18+.
`YOUTUBE_API_KEY` là tùy chọn (metadata đầy đủ từ Subtitle_supperVip; thiếu thì tiêu đề lấy qua yt-dlp).

```powershell
python scripts/run_real_job.py "https://www.youtube.com/watch?v=..." --dry-run      # kiểm tra điều kiện, KHÔNG gọi LLM
python scripts/run_real_job.py "https://www.youtube.com/watch?v=..." --chapters 3 --max-budget-usd 2
```

Chạy thật gọi Claude qua nhiều lượt (tốn chi phí tài khoản): bắt đầu với `--chapters 3`. Chọn adapter thật trong `config/config.json`:
`"adapters": {"source": "provider_chain", "story": "story_branch"}` (thứ tự provider: `source.providers`). Với mỗi job, ba tầng dữ liệu nguồn nằm ở `workspace/job_<id>/source/`
(`subtitle_raw.*` nguyên bản, `transcript_structured.json` có timestamp, `transcript_clean.txt` sạch, `source.json`); truyện ở `workspace/job_<id>/story/story.txt`
(blueprint/continuity/chương nội bộ ở `story/oh-story/`, báo cáo gỡ heading/trùng lặp ở `story/assembly_report.json`).

Log có cấu trúc: `runtime/logs/orchestrator.jsonl` và `workspace/job_<id>/job.log.jsonl`. Manifest từng job: `workspace/job_<id>/manifest.json`.
