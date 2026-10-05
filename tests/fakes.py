"""Test double cho Phase 2: yt-dlp giả (đọc fixture), oh-story giả (AgentRunner viết file như oh-story), StageContext dựng tay."""
from __future__ import annotations

import json
import random
import re
import shutil
from pathlib import Path

from contentfactory.contracts import CancelToken, ErrorClass, StageContext, StageError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
URL = "https://www.youtube.com/watch?v=abcdefghijk"


def make_ctx(root: Path, stage: str = "story", params: dict | None = None, inputs: dict | None = None,
             job_id: str = "000001", attempt: int = 1, cancel: CancelToken | None = None) -> StageContext:
    ws = Path(root) / "workspace" / f"job_{job_id}"
    stage_dir = ws / stage
    stage_dir.mkdir(parents=True, exist_ok=True)
    events: list = []
    ctx = StageContext(job_id=job_id, stage=stage, attempt=attempt, stage_key="k", workspace=ws, stage_dir=stage_dir,
                       params=params or {}, inputs=inputs or {}, config={"output_dir": str(Path(root) / "output")},
                       cancel=cancel or CancelToken(), log=lambda event, level="info", **f: events.append((event, f)))
    ctx.events = events
    return ctx


class FakeYtDlp:
    """Thay YtDlp: đếm số lần gọi, trả fixture như yt-dlp sẽ làm."""

    def __init__(self, fixture: str = "manual_split.srt", manual: bool = True, lang: str = "vi", title: str = "Chuyện ma ở nhà cũ"):
        self.fixture, self.manual, self.lang, self.title = fixture, manual, lang, title
        self.info_calls = self.download_calls = 0

    def available(self) -> bool:
        return True

    def info(self, url: str) -> dict:
        self.info_calls += 1
        tracks = {self.lang: [{"ext": Path(self.fixture).suffix[1:]}]}
        return {"id": "abcdefghijk", "title": self.title, "channel": "Kênh Thử", "duration": 13, "language": self.lang,
                "upload_date": "20260101", "webpage_url": url,
                "subtitles": tracks if self.manual else {}, "automatic_captions": {} if self.manual else tracks}

    def download_subtitle(self, url: str, lang: str, auto: bool, out_dir: Path) -> Path:
        self.download_calls += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / f"abcdefghijk.{lang}{Path(self.fixture).suffix}"
        shutil.copyfile(FIXTURES / self.fixture, dst)
        return dst


class ScriptedOhStory:
    """AgentRunner giả lập đúng các tệp mà oh-story ghi ra (正典, 分支提案, 分支简报, 分支设定, 大纲, 细纲, 正文, 追踪).
    Chương sinh ra CÓ dòng tiêu đề và mỗi đoạn một dòng như oh-story thật; Story Assembler phải gỡ."""

    def __init__(self, ignore_first_turns: dict | None = None, fail_on_call: int | None = None,
                 fail_with: StageError | None = None, never: set | None = None):
        self.calls: list[str] = []          # dòng lệnh của mỗi lượt
        self.prompts: list[str] = []
        self.ignore = dict(ignore_first_turns or {})   # {"analyze": 1}: lượt đầu chỉ hỏi lại, không ghi gì
        self.fail_on_call, self.fail_with = fail_on_call, fail_with
        self.never = never or set()
        self.n = 0

    @staticmethod
    def _w(p: Path, text: str) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def run(self, prompt: str, cwd: Path, session, ctx):
        self.n += 1
        self.prompts.append(prompt)
        first = next((l for l in prompt.splitlines() if l.startswith("/story")), None)
        if first:
            self.last_cmd = first                    # follow-up không nêu lệnh: tiếp tục lệnh gần nhất
        cmd = first or getattr(self, "last_cmd", "")
        self.calls.append(first or "(follow-up)")
        if self.fail_on_call == self.n:
            raise self.fail_with or StageError(ErrorClass.TRANSIENT, "AGENT_NO_RESULT", "mô phỏng agent chết")
        step = ("analyze" if "story-branch analyze" in cmd else "explore" if "story-branch explore" in cmd
                else "create" if "story-branch create" in cmd else "handoff" if "story-branch handoff" in cmd
                else "outline" if "开书" in cmd else "write")
        if step in self.never:
            return self._turn()
        if self.ignore.get(step, 0) > 0:
            self.ignore[step] -= 1
            return self._turn()
        m = re.search(r"Thư mục sách: `([^`]+)`", prompt) or re.search(r"Thư mục sách đích: `([^`]+)`", prompt)
        book = cwd / (m.group(1) if m else getattr(self, "book", "book"))
        if m:
            self.book = m.group(1)
        if step == "analyze":
            self._w(cwd / "分支库" / "SRC-001" / "正典.md", "# Chính điển\nNhân vật, dòng thời gian, bí mật.\n")
        elif step == "explore":
            self._w(cwd / "分支库" / "SRC-001" / "分支提案.md", "# Đề xuất nhánh\nB01 ...\n")
        elif step == "create":
            self._w(cwd / "分支库" / "SRC-001" / "分支" / "SRC-001-B01.md", "# Nhánh B01\n")
        elif step == "handoff":
            self._w(book / "设定" / "分支设定.md", "# Thiết lập nhánh\n")
            self._w(book / ".story" / "work" / "分支交接.md", "# Bàn giao\n")
        elif step == "outline":
            self._w(book / "大纲" / "大纲.md", "# Đại cương\n")
            for i in range(1, 11):
                self._w(book / "大纲" / f"细纲_第{i:03d}章.md", f"# Chi tiết chương {i}\n")
        elif step == "write":
            m2 = re.search(r"写第(\d+)(?:-(\d+))?章", cmd)
            a = int(m2.group(1))
            b = int(m2.group(2) or a)
            for i in range(a, b + 1):
                self._w(book / "正文" / f"第{i:03d}章_Tiêu đề {i}.md",
                        f"Chương {i}: Tiêu đề {i}\n{self._paragraph(i, 1)}\n{self._paragraph(i, 2)}\n")
            self._w(book / "追踪" / "_tracking-state.json", json.dumps({"last_committed_chapter": b}))
        return self._turn()

    @staticmethod
    def _paragraph(i: int, j: int) -> str:
        words = "đêm gió mưa hẻm bước chân đèn cửa gỗ tiếng động người áo đen im lặng bóng tối lá khô mèo trắng sông sương".split()
        rnd = random.Random(i * 100 + j)
        return f"Đoạn {i}.{j} " + " ".join(rnd.choice(words) for _ in range(40)) + "."

    def _turn(self) -> dict:
        return {"session_id": f"S{self.n}", "text": "ok", "cost_usd": 0.01, "is_error": False}


class StubProvider:
    """SourceProvider giả: đếm số lần gọi, trả fixture, có thể lỗi theo kịch bản (danh sách StageError lần lượt)."""

    def __init__(self, name="stub", kinds=("youtube_url",), fixture="manual_split.srt", fail=None, title="Chuyện ma ở nhà cũ",
                 available=True, kind="manual", language="vi", description=None):
        self.name, self.kinds, self.fixture, self.fail = name, kinds, fixture, list(fail or [])
        self.title, self._available, self.kind, self.language, self.description = title, available, kind, language, description
        self.calls = self.describe_calls = 0

    def supports(self, kind):
        return kind in self.kinds

    def available(self):
        return self._available

    def health(self):
        return {"ok": self._available, "provider": self.name}

    def describe(self, src, ctx):
        self.describe_calls += 1
        return {"title": self.title, "description": self.description} if self.title else {}

    def acquire(self, src, work_dir, ctx, prefs):
        self.calls += 1
        if self.fail:
            e = self.fail.pop(0)
            if e is not None:
                raise e
        work_dir.mkdir(parents=True, exist_ok=True)
        fmt = Path(self.fixture).suffix[1:]
        out = work_dir / f"subtitle_raw.{fmt}"
        shutil.copyfile(FIXTURES / self.fixture, out)
        return {"source_url": src["value"], "source_type": "youtube", "provider": self.name, "video_id": "abcdefghijk",
                "title": self.title, "description": self.description, "language": self.language, "raw_subtitle_path": out,
                "subtitle_format": fmt, "subtitle_kind": self.kind, "has_timestamps": True, "metadata": {"stub": True},
                "status": "ok", "error": None}


def stub_deploy(root: Path, ws: Path) -> None:
    """Thay deploy.py: chỉ cần dấu `.story-deployed` (deploy thật được test riêng)."""
    ws.mkdir(parents=True, exist_ok=True)
    (ws / ".story-deployed").write_text("stub\n", encoding="utf-8")
