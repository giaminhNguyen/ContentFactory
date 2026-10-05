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
    "limits": {"default": 2, "gpu": 1},                     # đồng thời theo tài nguyên (D-15)
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
    f = root / "config" / "config.json"
    if f.exists():
        _merge(data, json.loads(f.read_text(encoding="utf-8")))
    if overrides:
        _merge(data, overrides)
    return Config(root, data)
