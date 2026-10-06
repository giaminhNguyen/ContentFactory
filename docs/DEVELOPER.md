# ContentFactory — ghi chú cho người phát triển (nhật ký phase + lệnh nâng cao)

> Tài liệu người dùng nằm ở `README.md`. Đây là README cũ (Phase 0–6) giữ lại làm tham chiếu lệnh nâng cao (`--advanced`).


Pipeline biến một nguồn truyện/video thành 1 video YouTube và N video TikTok. Thiết kế: `HANDOFF.md`; audit và quyết định: `docs/`.

Trạng thái: **Phase 6 (gói output có phiên bản + README/project.json, Metadata Builder/Channel Config/Sequence, upload YouTube qua yt_uploader, retry upload không render lại; chưa upload thật) trên Phase 5 (tích hợp ContentFlow: render YouTube 16:9 + từng part TikTok 9:16, Source Sync nền dùng chung, retry/trạng thái từng part; cần Python có Pillow cho ContentFlow) trên Phase 4 (Audio Quality Pipeline: dọn biên, Pause Engine, Narration Master, YouTube + watermark, TikTok ×2 giữ cao độ + split thông minh, Audio QA; cần ffmpeg) trên Phase 3 (TTS framework: Manager, Planner + validator, retry/cache theo segment, onboarding Analyzer, Auto Tune; chưa có engine TTS thật) trên Phase 2.9 (lõi căn chỉnh với kiến trúc cuối: start/target stage, hold/auto resume, config snapshot) trên nền Phase 2 + Subtitle_supperVip** — orchestrator lõi (SQLite, state machine, queue theo stage, checkpoint, retry, resume) + **Source thật** (`SourceAdapter` nhiều provider: Subtitle_supperVip chính, yt-dlp dự phòng → Transcript Processor) + **Story** (`StoryBranchAdapter` điều khiển oh-story, Story Assembler). TTS/Render/Upload vẫn là fake. Thiết kế job control (stage-based pipeline, pause/auto-resume, Resource Monitor, config snapshot) đã chốt trong `HANDOFF.md` §15A–§15C nhưng **chưa triển khai**. Story **chưa được kiểm chứng với LLM thật** (xem `docs/DECISIONS.md` D-23).

```powershell
# Python >= 3.10, không cần cài gói ngoài để chạy test
python scripts/run_fake_job.py --temp          # chạy 1 fake job trong thư mục tạm
python -m unittest discover -s tests -t .      # chạy test (~8 phút đủ bộ, gồm ffmpeg/ContentFlow thật khi có biến môi trường)
node --test tests/ui_js/*.mjs                  # logic thuần của giao diện (không cần trình duyệt)
# QA trình duyệt thật (Chrome + axe): xem README mục "Phát triển"; phần scale cần fixture `--many 250`, phần templates/tplprev cần `scripts/ui_qa/real_templates.py`

# dùng CLI trên thư mục gốc repo (tạo workspace/, output/, runtime/ — đã gitignore)
$env:PYTHONPATH = "src"
python -m contentfactory submit --input "https://youtu.be/x" --set made_for_kids=false
python -m contentfactory run                   # chạy tới khi hết việc; Ctrl-C để dừng êm, chạy lại sẽ resume
python -m contentfactory status
python -m contentfactory retry <job_id>        # job FAILED: chạy lại đúng stage lỗi

# điều khiển job (Phase 2.9)
python -m contentfactory plan   --input "https://youtu.be/x" --mode THROUGH_TTS          # xem stage nào chạy/bỏ qua
python -m contentfactory submit --mode TTS_ONLY --artifact story_text=story.txt           # TTS từ story.txt có sẵn
python -m contentfactory submit --mode VIDEO_ONLY --artifact audio_master=a.wav --metadata-title "Tiêu đề"
python -m contentfactory resume <job_id> [--now]       # job đang PAUSED_* (Auto Resume tắt hoặc muốn ép đo lại)
python -m contentfactory config <job_id> --auto-resume off
python -m contentfactory resources                     # trạng thái Resource Monitor

# Publish (Phase 6): kênh, tiêu đề, upload YouTube. Mặc định vẫn là fake.
#   channels/kenh_a/channel.json: {"name": "Kênh Truyện A", "sequence": {"last_used": 26}, "watermark": "watermark.wav",
#                                  "description_template": "{project_title}\n\nNghe full tại {channel_name}", "publishing": {"privacy": "unlisted", "made_for_kids": false}}
#   config/config.json: {"adapters": {"publish": "yt_uploader"}}   # cần daemon:  yt-uploader serve --headless  (+ yt-uploader login một lần)
python -m contentfactory submit --input "<url>" --channel kenh_a --title "Tiêu đề truyện" --set made_for_kids=false
python -m contentfactory status                       # in cả gói output và link YouTube
python -m contentfactory sequences                    # số Full Audio đã reserve; sequence-release <job> để nhả số của job chưa đăng
# Test thật: $env:CF_TEST_YT_UPLOADER_EXE = "<yt-uploader.exe đã build bằng go build ./cmd/yt-uploader>"

# Render (Phase 5): ContentFlow thật (mặc định vẫn là fake). Cần Python có Pillow + ffmpeg trên PATH cho ContentFlow:
#   config/config.json: {"adapters": {"render": "contentflow"}, "tools": {"contentflow": {"python": "D:/venv-cf/Scripts/python.exe"}},
#     "render": {"pools": {"gameplay": {"raw_dir": "D:/videos/ngang"}, "gameplay_vertical": {"raw_dir": "D:/videos/doc"}}}}
python -m contentfactory pools --sync                  # xem/đồng bộ source pool (chạy nền tự động khi `run`)
python -m contentfactory retry-part <job_id> 3         # render lại đúng part TikTok số 3 (job ở render_tiktok)
# Thumbnail cần template 1648x928 + font (repo ContentFlow không có): render.profiles.youtube.thumbnail.config_overrides
# Test thật: $env:CF_TEST_CONTENTFLOW_PYTHON = "<python có Pillow>"; python -m unittest tests.test_render

# Audio (Phase 4): dùng ffmpeg thật cho stage audio (mặc định vẫn là fake); profile mastering/split nằm trong params.audio
#   config/config.json:  {"adapters": {"audio": "ffmpeg"}, "tools": {"ffmpeg": null, "ffprobe": null}}
python scripts/audio_qa.py <file.wav> [--lufs -16 --duration 600]       # QA một file audio (corrupt, rỗng, clipping, im lặng, độ to...)

# TTS (Phase 3): thêm engine mới chỉ từ repo/docs, rồi đo thực tế
python scripts/tts_onboard.py <thư-mục | git URL | URL docs> --out onboarding/<engine>    # profile candidate + config adapter + needs_user
python scripts/tts_tune.py --root . --profile onboarding/<engine>/profile.candidate.json  # Auto Tune: gọi engine THẬT, tốn thời gian/tiền
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
