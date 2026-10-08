"""SQLite của Kho nhân vật (system of record): WAL, giao dịch IMMEDIATE (an toàn khi nhiều tiến trình/job cùng ghi), migration có version.

Một kết nối dùng chung + RLock cho luồng trong tiến trình; BEGIN IMMEDIATE cho tiến trình khác. Schema chỉ THÊM (không bao giờ xoá dữ liệu khi nâng cấp).
Excel/JSON chỉ là bản xuất/nhập — không phải nơi lưu sự thật.
"""
from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 1

# Vai mặc định: KHÔNG phải nhân vật canon, chỉ là danh mục vai để gán theo từng truyện (có thể gán nhiều vai tương thích cho một nhân vật).
BUILTIN_ROLES = [
    ("protagonist", "Nhân vật chính", "Người dẫn dắt câu chuyện, chịu thay đổi lớn nhất."),
    ("deuteragonist", "Nhân vật chính thứ hai", "Đồng hành hoặc đối trọng gần nhất với nhân vật chính."),
    ("antagonist", "Phản diện", "Đối lập trực tiếp với mục tiêu của nhân vật chính."),
    ("rival", "Đối thủ", "Cạnh tranh cùng mục tiêu nhưng không nhất thiết là phản diện."),
    ("foil", "Nhân vật tương phản", "Làm nổi bật tính cách nhân vật chính bằng sự khác biệt."),
    ("mentor", "Người dẫn đường", "Truyền kiến thức/giá trị, thường có bí mật."),
    ("ally", "Đồng minh", "Hỗ trợ nhân vật chính có động cơ riêng."),
    ("love_interest", "Người được yêu", "Trục tình cảm của truyện."),
    ("confidant", "Người tâm giao", "Nơi nhân vật chính nói ra điều thật lòng."),
    ("comic_relief", "Nhân vật gây cười", "Giảm căng thẳng đúng lúc."),
    ("catalyst", "Chất xúc tác", "Kích hoạt biến cố mà không nhất thiết đứng về phía nào."),
    ("gatekeeper", "Người gác cổng", "Chặn hoặc thử thách trước một bước ngoặt."),
    ("wildcard", "Ẩn số", "Hành động khó đoán, có thể đổi phe."),
]

DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE characters (
  character_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, name_key TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '[]',
  core_personality TEXT NOT NULL DEFAULT '', temperament TEXT NOT NULL DEFAULT '', motivations TEXT NOT NULL DEFAULT '[]',
  strengths TEXT NOT NULL DEFAULT '[]', flaws TEXT NOT NULL DEFAULT '[]', communication_style TEXT NOT NULL DEFAULT '',
  boundaries TEXT NOT NULL DEFAULT '[]', genre_affinities TEXT NOT NULL DEFAULT '[]', visual_cues TEXT NOT NULL DEFAULT '', voice_cues TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL, status TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
  created_in_story TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX characters_status ON characters(status);
CREATE INDEX characters_name_key ON characters(name_key);
CREATE TABLE role_types (role_code TEXT PRIMARY KEY, label TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', builtin INTEGER NOT NULL DEFAULT 1);
CREATE TABLE worlds (world_id TEXT PRIMARY KEY, story_id TEXT NOT NULL UNIQUE, canon_mode TEXT NOT NULL DEFAULT 'parallel', genre TEXT NOT NULL DEFAULT '',
  constraints TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL);
CREATE TABLE character_variants (variant_id TEXT PRIMARY KEY, character_id TEXT NOT NULL REFERENCES characters(character_id), world_id TEXT NOT NULL, story_id TEXT NOT NULL,
  facts TEXT NOT NULL DEFAULT '{}', deviations TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL);
CREATE TABLE story_cast (story_id TEXT NOT NULL, world_id TEXT NOT NULL, character_id TEXT NOT NULL, variant_id TEXT, role_code TEXT NOT NULL,
  fit_notes TEXT NOT NULL DEFAULT '', local_alias TEXT, goal TEXT NOT NULL DEFAULT '', arc TEXT NOT NULL DEFAULT '', lock_status TEXT NOT NULL DEFAULT 'frozen',
  state TEXT NOT NULL DEFAULT 'active', is_new INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL, PRIMARY KEY (story_id, character_id, role_code));
CREATE TABLE story_relationships (rel_id TEXT PRIMARY KEY, story_id TEXT NOT NULL, world_id TEXT NOT NULL, a_id TEXT NOT NULL, b_id TEXT NOT NULL,
  type TEXT NOT NULL, direction TEXT NOT NULL DEFAULT 'mutual', status TEXT NOT NULL DEFAULT 'active', timeline TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '');
CREATE TABLE appearances (appearance_id TEXT PRIMARY KEY, story_id TEXT NOT NULL, world_id TEXT NOT NULL, character_id TEXT NOT NULL, role_code TEXT NOT NULL,
  outcome TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', snapshot TEXT NOT NULL DEFAULT '{}', publish_id TEXT, created_at REAL NOT NULL,
  UNIQUE (story_id, character_id, role_code));
CREATE INDEX appearances_character ON appearances(character_id);
CREATE TABLE character_candidates (candidate_id TEXT PRIMARY KEY, story_id TEXT NOT NULL, job_id TEXT, profile TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'staged',
  created_at REAL NOT NULL);
CREATE INDEX candidates_story ON character_candidates(story_id);
CREATE TABLE change_sets (publish_id TEXT PRIMARY KEY, story_id TEXT NOT NULL, job_id TEXT, status TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
  reverted_at REAL);
CREATE TABLE universe_revisions (rev INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, ref TEXT, publish_id TEXT);
CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, entity TEXT NOT NULL, entity_id TEXT,
  before TEXT, after TEXT, job_id TEXT, publish_id TEXT, rev INTEGER);
CREATE INDEX audit_entity ON audit_events(entity, entity_id);
"""


class UniverseDB:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            cur = self.conn.execute("PRAGMA user_version").fetchone()[0]
            if cur >= SCHEMA_VERSION:
                return
            if cur and self.path.exists():
                shutil.copyfile(self.path, self.path.with_suffix(f".db.bak-v{cur}"))      # nâng cấp: luôn có bản sao trước khi đổi schema
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                if cur < 1:
                    for stmt in filter(str.strip, DDL.split(";\n")):
                        self.conn.execute(stmt)
                    self.conn.executemany("INSERT INTO role_types(role_code,label,description,builtin) VALUES (?,?,?,1)", BUILTIN_ROLES)
                    self.conn.execute("INSERT INTO meta(key,value) VALUES ('created_at', ?)", (str(time.time()),))
                self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    @contextlib.contextmanager
    def tx(self):
        """Giao dịch ghi: IMMEDIATE (giữ khóa ghi ngay) + RLock; lỗi ⇒ ROLLBACK toàn bộ."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException as e:
                self.conn.execute("COMMIT" if getattr(e, "commit_anyway", False) else "ROLLBACK")      # lỗi nghiệp vụ muốn giữ audit (vd xung đột revision)
                raise

    @contextlib.contextmanager
    def snapshot(self):
        """Giao dịch đọc nhất quán (một revision duy nhất cho cả lần xuất Excel)."""
        with self._lock:
            self.conn.execute("BEGIN")
            try:
                yield self.conn
            finally:
                self.conn.execute("COMMIT")

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        r = self.q(sql, args)
        return r[0] if r else None

    def close(self) -> None:
        with self._lock:
            self.conn.close()


def dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True)
