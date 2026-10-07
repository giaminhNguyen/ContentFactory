"""WorkerTTS: worker sống lâu được dùng lại giữa các segment, lỗi được phân loại, worker chết thì khởi động lại."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from contentfactory.adapters.worker_tts import WorkerTTS
from contentfactory.contracts import ErrorClass, StageError
from contentfactory.tts.autotune import make_ctx

FAKE = r"""
import json, os, sys, wave
proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1); os.dup2(2, 1)
print("rác của engine trên stdout")
proto.write(json.dumps({"ready": True, "sample_rate": 8000}) + "\n")
for raw in sys.stdin.buffer:
    r = json.loads(raw)
    if r["text"] == "die":
        sys.exit(3)
    if r["text"] == "bad":
        proto.write(json.dumps({"ok": False, "type": "ValueError", "error": "Voice 'x' not found"}) + "\n"); continue
    w = wave.open(r["out"], "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
    w.writeframes(b"\x10\x10" * 800); w.close()
    proto.write(json.dumps({"ok": True, "pid": os.getpid()}) + "\n")
"""


class WorkerTTSTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-wtts-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ctx = make_ctx(self.tmp)
        self.a = WorkerTTS({"engines": {"fake": {"command": [sys.executable, "-c", FAKE], "capabilities": {"languages": ["vi"]}}}})
        self.addCleanup(self.a._kill, "fake")

    def say(self, text, engine="fake"):
        return self.a.synthesize({"index": 1, "text": text}, {"engine": engine, "language": "vi"}, self.tmp / "o.wav", self.ctx)

    def test_reuse_errors_restart(self):
        self.say("xin chào")
        pid = self.a._procs["fake"][0].pid
        self.say("lần hai")
        self.assertEqual(self.a._procs["fake"][0].pid, pid)                 # model nạp một lần
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["o.wav"])
        with self.assertRaises(StageError) as e:
            self.say("bad")
        self.assertEqual(e.exception.error_class, ErrorClass.POLICY)
        with self.assertRaises(StageError) as e:
            self.say("die")
        self.assertEqual(e.exception.code, "TTS_WORKER_DIED")
        self.say("sống lại")
        self.assertNotEqual(self.a._procs["fake"][0].pid, pid)
        with self.assertRaises(StageError) as e:
            self.say("x", engine="khac")
        self.assertEqual(e.exception.code, "UNKNOWN_TTS_ENGINE")
        self.assertEqual(self.a.capabilities()["languages"], ["vi"])


if __name__ == "__main__":
    unittest.main()
