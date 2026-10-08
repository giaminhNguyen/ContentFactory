"""Cú pháp stream-json của driver `claude_cli` (chuyển từ ClaudeCliRunner ở adapters sang workers, C6)."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from contentfactory.contracts import CancelToken, ErrorClass, StageError
from contentfactory.workers.drivers.base import ExecRequest
from contentfactory.workers.drivers.claude_cli import ClaudeCliDriver
from contentfactory.workers.errors import WorkerErrorClass

HERE = Path(__file__).resolve().parent


class ClaudeCliDriverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stub = [sys.executable, str(HERE / "fake_claude.py")]

    def driver(self, mode="ok", **cfg):
        env = {"FAKE_CLAUDE_MODE": mode, "FAKE_CLAUDE_ARGS_FILE": str(self.tmp / "args.json")}
        return ClaudeCliDriver({"claude_cmd": self.stub, "env": env, **cfg})

    def execute(self, driver, prompt="x", session=None, cancel=None):
        return driver.execute(ExecRequest(work_type="story.write", prompt=prompt, cwd=self.tmp,
                                          session=session, cancel=cancel or CancelToken()))

    def test_command_line_isolates_the_session_and_limits_tools(self):
        cmd = ClaudeCliDriver({"claude_cmd": ["claude"], "model": "m", "max_budget_usd_per_turn": 2}).command("SID")
        for flag in ("-p", "--strict-mcp-config", "--verbose"):
            self.assertIn(flag, cmd)
        self.assertEqual(cmd[cmd.index("--setting-sources") + 1], "project,local")     # không nạp CLAUDE.md/plugin/hook của người dùng
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "SID")
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "2")
        self.assertIn("--allowedTools", cmd)
        self.assertLess(cmd.index("--model"), cmd.index("--allowedTools"))             # variadic nằm cuối
        bypass = ClaudeCliDriver({"claude_cmd": ["claude"], "permission_mode": "bypassPermissions"}).command(None)
        self.assertNotIn("--allowedTools", bypass)
        self.assertNotIn("--resume", bypass)

    def test_parses_result_and_strips_nested_session_env(self):
        os.environ["CLAUDECODE"] = "1"
        self.addCleanup(os.environ.pop, "CLAUDECODE", None)
        res = self.execute(self.driver(), prompt="/story-branch analyze")
        self.assertTrue(res.ok)
        self.assertEqual((res.session_id, res.text, res.cost_usd), ("SID", "xong lượt", 0.0123))
        seen = json.loads((self.tmp / "args.json").read_text(encoding="utf-8"))
        self.assertEqual(seen["claude_env"], [])
        self.assertEqual(Path(seen["cwd"]).resolve(), self.tmp.resolve())

    def test_waits_for_background_tasks_before_finishing(self):
        t0 = time.time()
        res = self.execute(self.driver("background"))
        self.assertEqual(res.text, "xong sau tác vụ nền")                              # lấy result SAU khi tác vụ nền xong
        self.assertGreaterEqual(time.time() - t0, 0.5)

    def test_does_not_hang_when_cli_stays_silent_after_background_tasks(self):
        t0 = time.time()
        res = self.execute(self.driver("background_silent", background_grace_s=1))
        self.assertEqual(res.text, "lượt đầu")
        self.assertLess(time.time() - t0, 15)

    def test_not_logged_in_is_an_auth_error(self):
        res = self.execute(self.driver("auth"))
        self.assertFalse(res.ok)
        self.assertEqual((res.error.kind, res.error.code), (WorkerErrorClass.AUTH, "CLAUDE_NOT_LOGGED_IN"))

    def test_crash_without_result_is_a_retryable_error(self):
        res = self.execute(self.driver("noresult"))
        self.assertFalse(res.ok)
        self.assertEqual((res.error.kind, res.error.code), (WorkerErrorClass.TEMPORARY, "AGENT_NO_RESULT"))

    def test_cancel_kills_a_hung_turn(self):
        cancel = CancelToken()
        threading.Timer(1.0, cancel.set).start()
        t0 = time.time()
        with self.assertRaises(StageError) as cm:
            self.execute(self.driver("hang"), cancel=cancel)
        self.assertEqual(cm.exception.error_class, ErrorClass.CANCELLED)              # hủy không bị coi là lỗi worker
        self.assertLess(time.time() - t0, 10)

    def test_missing_cli_is_a_worker_error(self):
        res = self.execute(ClaudeCliDriver({"claude_cmd": ["khong-co-claude"]}))
        self.assertFalse(res.ok)
        self.assertEqual(res.error.code, "CLAUDE_CLI_MISSING")


if __name__ == "__main__":
    unittest.main()
