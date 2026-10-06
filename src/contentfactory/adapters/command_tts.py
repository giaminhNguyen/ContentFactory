"""CommandTTS: adapter TTS tổng quát cho engine chạy bằng dòng lệnh, điều khiển hoàn toàn bằng một SPEC JSON.

TTS Analyzer (tts/analyzer.py) sinh spec này từ repo/docs của engine => thêm một engine CLI mới chỉ là thêm config, không viết code,
không sửa orchestrator:
    "adapters": {"tts": "contentfactory.adapters.command_tts:CommandTTS"}, "adapter_config": {"tts": <spec>}

Spec:
  engine_id, command: [argv...] với placeholder {text} {text_file} {out} {voice} {model} {language} {settings.<tên>}
  text_via : "arg" | "file" | "stdin"   (text_file/stdin: văn bản UTF-8 ghi ra file tạm bên cạnh chunk)
  output   : "file" (engine ghi vào {out}) | "stdout" (engine in WAV ra stdout)
  optional_args: {"voice": ["--voice", "{voice}"], "settings.speed": ["--speed", "{settings.speed}"]}  chỉ thêm khi giá trị có
  cwd, env, env_required [tên biến môi trường bắt buộc], timeout_s, capabilities {...}
Adapter KHÔNG tự ghép tên file kết quả: ghi vào `<out>.part` rồi đổi tên (atomic, đúng contract). Chỉ xuất WAV; engine ra định dạng khác
cần bước chuyển đổi (AudioProcessor thật, Phase 4) — spec khai `capabilities.output_formats`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..contracts import ErrorClass, StageContext, StageError

_TRANSIENT_RX = re.compile(r"rate.?limit|429|too many requests|timed? ?out|temporar|503|502|504|connection (reset|refused|aborted)", re.I)
_AUTH_RX = re.compile(r"unauthori[sz]ed|forbidden|invalid api.?key|api.?key (is )?(missing|required)|401|403|authentication", re.I)
_RUNTIME_RX = re.compile(r"out of memory|cuda|no module named|modulenotfounderror|command not found|not recognized|no such file", re.I)


def _get(profile: dict, name: str):
    cur = profile
    for part in name.split("."):
        cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def _remove_quietly(path) -> None:
    """Dọn file tạm; KHÔNG BAO GIỜ làm hỏng kết quả/phân loại lỗi của lần gọi. Trên Windows một tiến trình con vừa bị kill (hoặc tiến trình khác thừa kế handle
    trong lúc tạo process song song) có thể còn giữ file vài chục ms => PermissionError; thử lại ngắn rồi bỏ qua (file tạm sẽ bị ghi đè ở lần gọi sau)."""
    if not path:
        return
    p = Path(path)
    for attempt in range(20):
        try:
            p.unlink(missing_ok=True)
            return
        except OSError:
            time.sleep(0.05)


class CommandTTS:
    def __init__(self, spec: dict | None = None) -> None:
        if not spec or not spec.get("command"):
            raise ValueError("CommandTTS cần adapter_config.tts = spec có 'command'")
        self.spec = spec
        self.engine_id = spec.get("engine_id", "command")

    def capabilities(self) -> dict:
        caps = dict(self.spec.get("capabilities") or {})
        if not caps.get("engine_version"):          # đổi lệnh/cwd/tham số => âm thanh có thể khác => cache chunk phải mất hiệu lực
            sig = json.dumps([self.spec["command"], self.spec.get("cwd"), self.spec.get("optional_args"), self.spec.get("env")],
                             sort_keys=True, default=str)
            caps["engine_version"] = "cmd-" + hashlib.sha256(sig.encode()).hexdigest()[:10]
        return caps

    def health(self) -> dict:
        exe = self.spec["command"][0]
        found = shutil.which(exe) or Path(exe).exists() or exe in ("python", "python3", sys.executable)
        missing_env = [e for e in self.spec.get("env_required", []) if not os.environ.get(e) and e not in self.spec.get("env", {})]
        return {"ok": bool(found) and not missing_env, "command_found": bool(found), "missing_env": missing_env}

    def _argv(self, segment: dict, profile: dict, out: Path, text_file: Path | None) -> list[str]:
        ctx = {"text": segment["text"], "text_file": str(text_file or ""), "out": str(out),
               "voice": profile.get("voice"), "model": profile.get("model"), "language": profile.get("language")}
        for k, v in (profile.get("settings") or {}).items():
            ctx[f"settings.{k}"] = v

        def fmt(a: str) -> str:
            def rep(m: re.Match) -> str:
                v = ctx.get(m.group(1))
                if v is None:
                    raise StageError(ErrorClass.POLICY, "MISSING_PLACEHOLDER", f"thiếu giá trị cho {{{m.group(1)}}} (profile/settings)")
                return str(v)
            return re.sub(r"\{([\w.]+)\}", rep, a)

        argv = [fmt(a) for a in self.spec["command"]]
        for name, args in (self.spec.get("optional_args") or {}).items():
            if _get(profile, name) is not None:
                argv += [fmt(a) for a in args]
        return argv

    def synthesize(self, segment: dict, profile: dict, out_path: Path, ctx: StageContext) -> dict:
        s = self.spec
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(out_path.name + ".part")
        text_file = None
        via = s.get("text_via", "arg")
        if via in ("file", "stdin"):
            text_file = out_path.with_name(out_path.name + ".txt")
            text_file.write_text(segment["text"], encoding="utf-8", newline="\n")
        argv = self._argv(segment, profile, tmp, text_file)
        env = {**os.environ, **{k: str(v) for k, v in (s.get("env") or {}).items()}, "PYTHONIOENCODING": "utf-8"}
        missing = [e for e in s.get("env_required", []) if not env.get(e)]
        if missing:
            raise StageError(ErrorClass.AUTH, "MISSING_CREDENTIAL", f"thiếu biến môi trường {missing}", resource="credential")
        so, se = out_path.with_name(out_path.name + ".stdout"), out_path.with_name(out_path.name + ".stderr")
        t0, timeout = time.time(), float(s.get("timeout_s", 120))
        try:
            with open(so, "wb") as fo, open(se, "wb") as fe, \
                    (open(text_file, "rb") if via == "stdin" else open(os.devnull, "rb")) as fi:
                p = subprocess.Popen(argv, cwd=s.get("cwd") or None, env=env, stdin=fi, stdout=fo, stderr=fe)
                while True:
                    try:
                        code = p.wait(0.2)
                        break
                    except subprocess.TimeoutExpired:
                        if ctx.cancel.is_set():
                            p.kill()
                            p.wait()
                            ctx.cancel.check()
                        if time.time() - t0 > timeout:
                            p.kill()
                            p.wait()
                            raise StageError(ErrorClass.TRANSIENT, "TTS_TIMEOUT", f">{timeout:.0f}s", {"argv0": argv[0]},
                                             resource="runtime") from None
            err = se.read_bytes().decode("utf-8", "replace")[-600:]
            if code != 0:
                cls, res = ((ErrorClass.AUTH, "credential") if _AUTH_RX.search(err) else
                            (ErrorClass.RESOURCE, "runtime") if _RUNTIME_RX.search(err) else
                            (ErrorClass.TRANSIENT, "provider" if _TRANSIENT_RX.search(err) else None))
                raise StageError(cls, "TTS_COMMAND_FAILED", f"exit={code}: {err}".strip(), {"exit": code}, resource=res)
            if s.get("output", "file") == "stdout":
                shutil.copyfile(so, tmp)
            if not tmp.is_file() or tmp.stat().st_size == 0:
                raise StageError(ErrorClass.TRANSIENT, "TTS_NO_OUTPUT", f"engine không ghi file kết quả ({argv[0]})")
            os.replace(tmp, out_path)
        finally:
            for f in (tmp, so, se, text_file):
                _remove_quietly(f)
        return {"index": segment["index"], "duration_sec": 0.0}
