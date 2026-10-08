"""Claude Code CLI (`claude`) — driver đầu tiên của W1.

Phần khác biệt của CLI này (cú pháp stream-json, resume session, kiểm auth, chờ tác vụ nền) nằm
trong file này. Lỗi được trả về dạng `ExecResult(ok=False, error=WorkerError)` để WorkerManager
retry/fallback; riêng CANCELLED lan ra ngoài (huỷ job không phải lỗi worker).
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time

from contentfactory.contracts import ErrorClass, StageError
from contentfactory.workers.errors import WorkerError, WorkerErrorClass

from .base import BaseDriver, ExecRequest, ExecResult

DEFAULT_ALLOWED = ["Read", "Write", "Edit", "Glob", "Grep", "Skill", "TodoWrite",
                    "Bash(python:*)", "Bash(python3:*)", "Bash(py:*)", "Bash(node:*)", "Bash(bash:*)", "Bash(sh:*)",
                    "Bash(ls:*)", "Bash(mkdir:*)", "Bash(cat:*)", "Bash(wc:*)", "Bash(git:*)"]


class ClaudeCliDriver(BaseDriver):
    id = "claude_cli"
    label = "Claude Code CLI"
    exe_names = ("claude",)
    extra_patterns = (
        (WorkerErrorClass.QUOTA, re.compile(r"claude (?:usage|rate) limit|credit balance is too low", re.I)),
        (WorkerErrorClass.AUTH, re.compile(r"please run /login|claude (?:is )?not (?:yet )?authenticated", re.I)),
    )

    def __init__(self, cfg: dict | None = None) -> None:
        super().__init__(cfg)
        self.cmd = list(self.cfg.get("claude_cmd") or [shutil.which("claude") or "claude"])
        self.permission_mode = self.cfg.get("permission_mode", "acceptEdits")
        self.allowed = self.cfg.get("allowed_tools", DEFAULT_ALLOWED)
        self.model, self.max_budget = self.cfg.get("model"), self.cfg.get("max_budget_usd_per_turn")
        self.idle_s = float(self.cfg.get("idle_timeout_s", 1800))
        self.hard_s = float(self.cfg.get("turn_timeout_s", 4 * 3600))
        self.grace_s = float(self.cfg.get("background_grace_s", 60))
        self.env_extra = self.cfg.get("env", {})

    def command(self, session: str | None) -> list[str]:
        cmd = [*self.cmd, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--permission-mode", self.permission_mode, "--setting-sources", "project,local", "--strict-mcp-config"]
        if self.model:
            cmd += ["--model", self.model]
        if self.max_budget:
            cmd += ["--max-budget-usd", str(self.max_budget)]
        if session:
            cmd += ["--resume", session]
        if self.allowed and self.permission_mode != "bypassPermissions":
            cmd += ["--allowedTools", *self.allowed]
        return cmd

    def build_command(self, req: ExecRequest) -> list[str]:
        return self.command(req.session)

    # -- execute --------------------------------------------------------------------------------
    def execute(self, req: ExecRequest) -> ExecResult:
        start = time.time()
        try:
            turn = self._run(req)
        except StageError as e:
            if e.error_class is ErrorClass.CANCELLED:
                raise                                  # huỷ: manager ghi attempt rồi lan cho caller
            return ExecResult(ok=False, text=e.message, error=WorkerError.from_stage_error(e))
        return ExecResult(ok=True, text=turn["text"], session_id=turn["session_id"], cost_usd=turn["cost_usd"],
                          duration_s=time.time() - start, meta={"is_error": turn["is_error"]})

    def _run(self, req: ExecRequest) -> dict:
        """Một lượt Claude Code stream-json (cùng cách bench của oh-story): ghi một message người dùng,
        đọc sự kiện tới `result`, giữ tiến trình sống tới khi các tác vụ nền (sub-agent) xong."""
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "OMC_"))}
        env.update(self.env_extra)
        try:
            p = subprocess.Popen(self.build_command(req), cwd=req.cwd, env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 encoding="utf-8", errors="replace")
        except FileNotFoundError:
            raise StageError(ErrorClass.RESOURCE, "CLAUDE_CLI_MISSING", f"không chạy được {self.cmd!r}",
                             resource="runtime") from None
        try:
            return self._drive(p, req)
        finally:
            if p.poll() is None:
                p.kill()
            for f in (p.stdin, p.stdout, p.stderr):
                try:
                    f.close()
                except OSError:
                    pass

    def _drive(self, p: subprocess.Popen, req: ExecRequest) -> dict:
        err_buf: list[str] = []
        threading.Thread(target=lambda: err_buf.append(p.stderr.read()), daemon=True).start()
        p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": req.prompt}},
                                 ensure_ascii=False) + "\n")
        p.stdin.flush()
        q: queue.Queue = queue.Queue()

        def reader() -> None:
            for line in p.stdout:
                q.put(line)
            q.put(None)

        threading.Thread(target=reader, daemon=True).start()
        result: dict = {}
        outstanding: set = set()
        closed, start, drained_at = False, time.time(), None
        last = start
        hard_s = min(self.hard_s, req.timeout_s) if req.timeout_s else self.hard_s
        while True:
            if req.cancel.is_set():
                p.kill()
                raise StageError(ErrorClass.CANCELLED, "CANCELLED", "huỷ giữa lượt agent")
            try:
                line = q.get(timeout=1)
            except queue.Empty:
                line = ""
            now = time.time()
            if line is None:
                break
            if line:
                last = now
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "system" and d.get("subtype") == "task_started" and d.get("is_backgrounded"):
                    outstanding.add(d.get("task_id"))
                if d.get("type") == "system" and d.get("subtype") == "task_notification":
                    outstanding.discard(d.get("task_id"))
                    drained_at = now if not outstanding else None
                if d.get("type") == "result":
                    result, drained_at = d, None
                    if not outstanding and not closed:
                        p.stdin.close()
                        closed = True
            # tác vụ nền đã xong mà CLI không phát thêm result nào: đừng chờ tới idle timeout
            if not closed and result and drained_at and now - drained_at > self.grace_s:
                p.stdin.close()
                closed = True
            if not closed and (now - last > self.idle_s or now - start > hard_s):
                p.kill()
                raise StageError(ErrorClass.TRANSIENT, "AGENT_TIMEOUT", f"quá {self.idle_s:.0f}s không có hoạt động")
            if closed and now - last > 120:             # đã đóng stdin mà tiến trình không thoát
                p.kill()
                break
        p.wait(timeout=60)
        text = str(result.get("result", ""))
        err = "".join(err_buf)
        # hết usage/token của tài khoản: tài nguyên TẠM THỜI (PAUSED_TOKEN); thời điểm reset không parse được thì dùng mặc định
        if re.search(r"usage limit|rate limit|too many requests|credit balance|overloaded|quota", text + err, re.I):
            raise StageError(ErrorClass.RESOURCE, "CLAUDE_USAGE_LIMIT", (text or err)[:300], resource="token")
        if re.search(r"not logged in|invalid api key|please run /login|authentication", text + err, re.I):
            raise StageError(ErrorClass.AUTH, "CLAUDE_NOT_LOGGED_IN", (text or err)[:300])
        if not result:
            raise StageError(ErrorClass.TRANSIENT, "AGENT_NO_RESULT", f"exit={p.returncode} {err[-300:]}")
        return {"session_id": result.get("session_id"), "text": text,
                "cost_usd": float(result.get("total_cost_usd") or 0.0), "is_error": bool(result.get("is_error"))}
