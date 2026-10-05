"""Sequence Manager (D-47): số "Full Audio {sequence}" theo từng channel, reserve MỘT lần cho mỗi project và cố định.

- `reserve` lười + idempotent: gọi lại cho cùng project trả đúng số cũ (retry upload/rerender không bao giờ đổi số).
- Cấp số trong một transaction IMMEDIATE: `max(last_used trong Channel Config, max đã cấp của channel) + 1`, nên hai job chạy song song luôn nhận số khác nhau.
- Số đã cấp không bị cấp lại (kể cả sau `release`; chấp nhận khoảng trống); `release` là hành động explicit.
- Trạng thái project (không phải cấu hình) nên không nằm trong config snapshot.
"""
from __future__ import annotations

import time

from .db import JobStore


class SequenceManager:
    def __init__(self, store: JobStore) -> None:
        self.store = store

    def reserve(self, channel_id: str, project_id: str, last_used: int = 0, now: float | None = None) -> int:
        now = now or time.time()
        with self.store._tx() as c:
            row = c.execute("SELECT sequence FROM channel_sequences WHERE project_id=? AND status!='released'", (project_id,)).fetchone()
            if row:
                return int(row["sequence"])
            top = c.execute("SELECT COALESCE(MAX(sequence), 0) FROM channel_sequences WHERE channel_id=?", (channel_id,)).fetchone()[0]
            seq = max(int(last_used or 0), int(top)) + 1
            c.execute("INSERT INTO channel_sequences(channel_id, sequence, project_id, status, reserved_at) VALUES(?,?,?,?,?)",
                      (channel_id, seq, project_id, "reserved", now))
            return seq

    def get(self, project_id: str) -> int | None:
        rows = self.store._q("SELECT sequence FROM channel_sequences WHERE project_id=? AND status!='released'", (project_id,))
        return int(rows[0]["sequence"]) if rows else None

    def mark_published(self, project_id: str, now: float | None = None) -> None:
        with self.store._tx() as c:
            c.execute("UPDATE channel_sequences SET status='published', published_at=? WHERE project_id=? AND status='reserved'",
                      (now or time.time(), project_id))

    def release(self, project_id: str, now: float | None = None) -> bool:
        """Nhả số của một project chưa đăng (explicit). Số vẫn bị giữ trong bảng nên không được cấp lại. Đã published thì từ chối."""
        with self.store._tx() as c:
            row = c.execute("SELECT status FROM channel_sequences WHERE project_id=? AND status!='released'", (project_id,)).fetchone()
            if not row:
                return False
            if row["status"] == "published":
                raise ValueError(f"project {project_id} đã đăng với số này; không release")
            c.execute("UPDATE channel_sequences SET status='released', released_at=? WHERE project_id=? AND status='reserved'",
                      (now or time.time(), project_id))
            return True

    def list(self, channel_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM channel_sequences" + (" WHERE channel_id=?" if channel_id else "") + " ORDER BY channel_id, sequence", \
            ((channel_id,) if channel_id else ())
        return [dict(r) for r in self.store._q(sql, args)]
