# Production checklist (kết quả Phase 8)

Trạng thái kiểm chứng thực tế trên máy phát triển (Windows 11, Python 3.10, ffmpeg 9.0.2 full build, không có NVENC). "Đã kiểm chứng" = chạy thật, không phải mock. Lệnh tái lập: `python scripts/validate_real.py <URL>` và bộ test với `CF_TEST_CONTENTFLOW_PYTHON` + `CF_TEST_YT_UPLOADER_EXE`.

## A. Đã kiểm chứng thật

| Hạng mục | Cách kiểm | Kết quả |
|---|---|---|
| Source thật (Subtitle_supperVip → transcript) từ URL YouTube thật | `validate_real.py` | OK (~2–3 s) |
| Audio Quality Pipeline bằng ffmpeg thật (master, watermark, x2 giữ cao độ, cắt part) | test_audio + validate_real (audio 240 s) | OK (~10 s cho 240 s) |
| Render YouTube 16:9 + TikTok 9:16 bằng ContentFlow thật, Source Sync thật | test_render (Real*) + validate_real | OK: đúng kích thước, có audio, độ dài khớp audio |
| Từng part TikTok ↔ audio part; tổng part × 2 = audio YouTube | validate_real (6 part), RealKillRestartTest | OK (sai lệch < 3 s) |
| Chế độ SUBTITLE_ONLY với nguồn thật; VIDEO_ONLY từ audio có sẵn rồi nới target tới publish (không chạy lại stage đã xong) | validate_real | OK (đã sửa lỗi: output không còn bắt buộc `story.txt`) |
| Kill runner giữa lúc render thật → khởi động lại → tiếp tục, audio không làm lại, render_youtube `interrupted → succeeded` | `RealKillRestartTest` | OK |
| Daemon `yt-uploader` thật (build bằng Go): health, idempotency lookup, ánh xạ lỗi auth | test_publishing (Real daemon) | OK |
| `setup` trên thư mục sạch và trên repo này (venv, pip, build uploader, doctor) | chạy thật | OK |
| Bí mật không lọt vào output/log/DB/workspace/`status`/`doctor --json` | `SecretsTest` | OK |
| Upload chậm không chặn render job khác; Story/TTS tiếp tục khi renderer bận | `UploadDoesNotBlockTest`, test_render | OK |
| Prosody (D-100): speech plan tất định, tách câu tiếng Việt (số thập phân, ngày, domain, viết tắt, thoại), không chia vụn ở dấu phẩy, khoảng nghỉ chính xác ±30 ms khi engine để lại im lặng cuối, không trôi qua 40 segment, retry dùng lại plan, mặc định 0 lần gọi LLM | test_prosody (39 test, gồm `StitchTest` với ffmpeg thật và `GoldenTest` 6 mẫu nội bộ), test_ui (`ProsodyViewTest`), `qa.mjs --only prosody` | OK. **Chưa nghe thử với engine TTS thật** (repo chỉ có engine giả): quy trình QA nghe — mở Giọng đọc → Nghe thử A/B với giọng thật, ghi `rules_version` (`vi-1`) + profile vào ghi chú; kiểm không máy móc, không nghỉ đôi, không cụt cuối câu, không click |
| Resume/retry: mạng, quota, token, đĩa, kill, restart, part lỗi, upload lỗi, Auto Resume bật/tắt | test_pause_resume, test_resume, test_failure_retry, test_render, test_publishing | OK |
| Dữ liệu: `project.title`, tiêu đề `[Full Audio N] \| …`, sequence ổn định qua retry, mô tả từ Channel Config, watermark theo kênh, `project.json`, output không có file tạm | test_publishing, test_automode | OK |

### Giao diện (Phase 9) — kiểm chứng bằng Chrome thật

| Hạng mục | Cách kiểm | Kết quả |
|---|---|---|
| Luồng hằng ngày (dán link → RUN → theo dõi → Mở output), bấm đúp RUN chỉ tạo 1 job | `qa.mjs --only daily` | OK |
| Chế độ một phần (truyện/audio có sẵn, bắt buộc tên truyện, khai báo trẻ em) | `--only partial` | OK |
| Job chờ mạng / cần đăng nhập / lỗi: giải thích, nút đúng ngữ cảnh, bật/tắt Auto Resume, không lộ stack trace | `--only paused` | OK |
| Kênh (sửa/lưu/giữ sau tải lại), TTS (không lộ credential), cài đặt tự lưu, Doctor 12 nhóm | `--only config` | OK |
| 7 trang × 4 cỡ cửa sổ × sáng/tối: không tràn ngang, không lỗi console/mạng, axe: 0 vi phạm nghiêm trọng | `--only pages` | OK (92 kiểm tra) |
| Bàn phím/focus/dialog, reduced-motion, rò rỉ poller/listener/tween | `--only keyboard,motion,perf` | OK |
| Danh sách 250 job, log dài, thời gian nạp, kích thước tài nguyên | `--only scale` | OK (`docs/PERFORMANCE.md`) |
| **Ứng dụng thật**: tạo dữ liệu mẫu từ UI → RUN → ffmpeg + ContentFlow thật → YouTube 1920×1080, TikTok 1080×1920, thumbnail, tiêu đề đúng mẫu kênh | `real_root.py` + `--only real` | OK |
| Dữ liệu mẫu qua đúng bộ kiểm story và QA audio thật | `tests/test_samples.py` | OK |

### Hiệu năng quan sát (CPU libx264, 1920×1080 / 1080×1920, audio 240 s)

| Stage | Thời gian |
|---|---|
| source (thật) | ~2.5 s |
| audio (ffmpeg) | ~10 s |
| render YouTube | ~39 s (~6× thời gian thực) |
| render TikTok (6 part) | ~24 s |
| output + publish (giả) | < 1 s |

Nút thắt là render (CPU). Có NVENC thì nhanh hơn nhiều (chưa đo: máy này không có). Chưa đo video 10–60 phút.

## B. Chưa kiểm chứng (cần điều kiện bên ngoài)

| Hạng mục | Thiếu gì | Cách kiểm chứng khi có |
|---|---|---|
| Giao diện trên Firefox/Safari, máy yếu, FPS hoạt họa bằng số | chỉ có Chrome; headless không đo FPS tin cậy | thử tay trên các trình duyệt khác; đo bằng DevTools Performance |
| Story thật bằng Claude Code CLI (tiếng Việt, truyện dài) | tốn token tài khoản; chưa có phiên chạy được duyệt chi phí | `python scripts/validate_real.py <URL> --story real` (thêm `--max-budget-usd` ở `run_real_job.py`) |
| TTS thật | chưa onboard engine nào | Phase TTS: `scripts/tts_onboard.py` |
| Upload YouTube thật | cần OAuth client Google + tài khoản | `setup` → `yt-uploader login` → `cf go ... ` với `privacy: private` |
| Quota/playlist/lịch đăng thật | như trên | như trên |
| NVENC | máy không có GPU NVIDIA | `doctor` cảnh báo; render tự rơi về libx264 |
| Render video dài 10–60 phút, nhiều job song song ở quy mô thật | thời gian | chạy `--long-audio-sec 1800` |
| Linux/macOS | chỉ thử Windows | lõi stdlib portable; script `.ps1/.cmd` là Windows |

## C. Việc còn phải làm tay trên máy mới

Đăng nhập `claude`; video nền; template thumbnail + font; OAuth Google + `yt-uploader login`; onboard TTS. `setup.ps1` + `doctor` liệt kê từng việc.

## D. Trước khi chạy thật lần đầu

1. `cf doctor` không còn `[FAIL]`; đọc từng `[WARN]`.
2. `cf demo` chạy được.
3. Kênh có `made_for_kids` đúng; `privacy` ở `private` cho tới khi tin tưởng.
4. Đặt `--title` riêng (đừng đăng công khai tiêu đề của người khác).
5. Chạy một job ngắn bằng TTS giả để xem toàn bộ gói output, rồi mới bật TTS/upload thật.

### Template (Phase 10) — kiểm chứng thật

| Hạng mục | Cách kiểm | Kết quả |
|---|---|---|
| Job thật (ffmpeg + ContentFlow thật) với template mặc định và template `thumb_gold`/`youtube_framed`/`tiktok_framed`: video 1920×1080, 3 part 1080×1920, thumbnail 1648×928, ảnh chụp khung hình đúng khung/vùng video | script smoke (VIDEO_ONLY từ audio mẫu), xem ảnh | OK |
| Phase 8 end-to-end (YouTube thật → Source → render thật → output → nới target) vẫn đạt khi render dùng template builtin | `scripts/validate_real.py <URL>` | OK ("KẾT QUẢ: ĐẠT") |
| Template: vòng đời, immutability, snapshot sống qua publish v3/restart/retry, invalidation theo kind, cache key, tương thích layout cũ, migrate, portable path, path traversal, import asset an toàn | `tests/test_templates*.py`, `modules/ContentFlow/tests/test_templating.py` (+ `real_ffmpeg`) | OK |
| Template Studio bằng Chrome thật: tạo → kéo/nhập số → undo/redo → lưu → kiểm tra → xem trước → render thử → publish → chọn cho kênh → xem trước RUN; axe, tràn ngang ở 1440/390 px sáng/tối | `qa.mjs --only templates` (`real_templates.py`) | OK (75 kiểm tra) |

**Chưa kiểm chứng / giới hạn:** chữ trong template **video** (renderer video chưa vẽ chữ); `align` chỉ `center`; thumbnail trong danh sách template là khung sơ đồ (xem trước thật trong Studio); NVENC/Linux/macOS như trước.
