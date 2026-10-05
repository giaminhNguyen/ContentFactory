"""Cấu hình: mặc định trong code, ghi đè bằng config/config.json (stdlib JSON, DECISIONS D-17)."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

DEFAULTS: dict = {
    "paths": {"workspace": "workspace", "output": "output", "runtime": "runtime", "db": "runtime/contentfactory.db"},
    "adapters": {"source": "fake", "story": "fake", "tts": "fake", "planner": "rule", "audio": "fake",
                 "render": "fake", "publish": "fake", "output": "builtin"},
    # Cấu hình truyền vào adapter nạp bằng "package.module:Class": {"tts": {...}} => Class(config). Adapter mới không cần sửa core.
    "adapter_config": {},
    # công cụ ngoài (cấu hình của MÁY, không vào snapshot): None = tìm trên PATH
    "tools": {"ffmpeg": None, "ffprobe": None,
              # yt_uploader (Phase 6): daemon `yt-uploader serve --headless`; token đọc từ <data_dir>/api_token (mặc định %APPDATA%\\yt-uploader)
              "yt_uploader": {"url": "http://127.0.0.1:8973", "data_dir": None, "token": None, "poll_s": 2.0, "max_wait_s": 21600},
              # ContentFlow (Phase 5): root = repo ContentFlow, python = Python có Pillow (+ ffmpeg trên PATH), base_dir = nơi chứa config.json/assets của
              # ContentFlow do orchestrator quản lý (frame, template, font thumbnail) — không sửa module
              "contentflow": {"root": "modules/ContentFlow", "python": None, "base_dir": "config/contentflow", "sync_wait_s": 3600,
                              "verify_output": True}},
    # Render (Phase 5): profile YouTube/TikTok và source pool (cấu hình NGỮ NGHĨA: vào snapshot). Ví dụ pool:
    #   "pools": {"gameplay": {"raw_dir": "D:/videos/gameplay", "sync": {"quality": "balanced"}}}  (size/fps mặc định theo profile)
    "render": {"profiles": {}, "pools": {}, "pools_dir": "runtime/pools", "pool_sync_background": True, "pool_sync_interval_s": 300},
    "limits": {"default": 2, "gpu": 1},
    # Publishing (Phase 6; D-76…): channels/<id>/channel.json = Channel Config; title_policy: warn (mặc định: cảnh báo khi dùng tiêu đề nguồn) | require
    "channels_dir": "channels",
    "tts_profiles_dir": "tts_profiles",                       # profile TTS đã onboard (annotated JSON); Auto Mode chọn tự động theo ngôn ngữ/engine
    # Auto Mode (Phase 7): cấu hình của MÁY, áp dụng lúc tạo job (kết quả chốt vào params/snapshot của job)
    "auto": {"tts_profile_selection": True, "pool_selection": True},
    # Auto Cleanup: dọn trung gian của job đã đăng, cache quá cỡ, workspace cũ. KHÔNG BAO GIỜ đụng output/ (của người dùng)
    "cleanup": {"enabled": True, "interval_s": 600, "intermediates_after_publish": True, "artifact_keep_days": 14, "failed_keep_days": 30,
                "cache_gb": {"tts": 20, "source": 5}},
    "publishing": {"title_policy": "warn", "defaults": {"privacy": "private", "category": None, "tags": [], "playlists": []}},                     # đồng thời theo tài nguyên (D-15)
    # retry (D-40): backoff có jitter, sàn/trần, ưu tiên Retry-After; quá ngưỡng thì GIỮ job thay vì ngủ trong hàng đợi
    "retry": {"max_attempts": 3, "backoff_s": [2, 10, 60], "max_interruptions": 5, "jitter": 0.2, "cap_s": 300,
              "floor_s": 1.0, "retry_after_hold_threshold_s": 600, "max_auto_resumes_without_progress": 5,
              "token_hold_default_s": 900, "quota_hold_default_s": 3600},
    # Auto Resume (D-39): mặc định BẬT; mỗi job override bằng job.auto_resume (giá trị hiệu lực chốt vào snapshot lúc tạo job)
    "auto_resume_default": True,
    # Resource Monitor (D-38): cấu hình của MÁY (không vào snapshot). Ngưỡng đĩa là ƯỚC LƯỢNG, chỉnh theo máy.
    "monitor": {"tick_s": 1.0, "base_s": 30, "max_s": 300, "network_hosts": [["1.1.1.1", 443], ["8.8.8.8", 53]],
                "network_timeout_s": 3.0,
                "disk_min_free_gb": {"default": 0.5, "tts": 1, "audio": 1, "render_youtube": 5, "render_tiktok": 5}},
    "lease_s": 30.0, "heartbeat_s": 10.0, "poll_s": 0.5,
    "output": {"name_template": "{date}_{slug}"},           # D-07
    # Source: ProviderChain thử lần lượt các provider (supervip = Subtitle_supperVip là provider chính, ytdlp = fallback)
    "source": {"providers": ["supervip", "ytdlp", "local", "text"], "languages": ["vi", "en"],
               "allow_translation": False,
               "reconstruct": {}},                          # ghi đè ReconstructConfig (sentence_gap, paragraph_gap, ...)
    "supervip": {"backend_dir": "modules/Subtitle_supperVip/backend", "python": None,
                 "youtube_api_key_env": "YOUTUBE_API_KEY", "timeout_s": 120, "env": {}},
    "youtube": {"yt_dlp_cmd": ["yt-dlp"], "yt_dlp_args": []},   # chỉ cho YtDlpProvider (fallback)
    "story_branch": {"permission_mode": "acceptEdits", "max_turns": 80, "max_follow_ups": 4,
                     "chapters_per_batch": 3, "max_budget_usd_per_turn": None},
    "job_defaults": {"language": "vi", "channel": "default",
                     "tiktok": {"speed": 2.0, "target_part_sec": 600}},
}


def _merge(a: dict, b: dict) -> dict:
    for k, v in b.items():
        a[k] = _merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
    return a


@dataclass
class Config:
    root: Path
    data: dict

    def path(self, key: str) -> Path:
        return (self.root / self.data["paths"][key]).resolve()

    def __getitem__(self, k: str):
        return self.data[k]

    def limit(self, resource: str) -> int:
        lim = self.data["limits"]
        return int(lim.get(resource, lim["default"]))


def load_config(root: Path, overrides: dict | None = None) -> Config:
    root = Path(root).resolve()
    data = copy.deepcopy(DEFAULTS)
    for name in ("config.json", "config.local.json"):      # config.local.json: cấu hình của MÁY do setup ghi (không commit), đè lên config.json
        f = root / "config" / name
        if f.exists():
            _merge(data, json.loads(f.read_text(encoding="utf-8-sig")))
    if overrides:
        _merge(data, overrides)
    return Config(root, data)
