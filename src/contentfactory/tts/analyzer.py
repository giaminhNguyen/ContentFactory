"""TTS Analyzer: onboarding một engine TTS mới chỉ từ repo/docs/source (HANDOFF §7–8).

  nguồn tham chiếu (thư mục local | repo GitHub | URL docs)
    -> quét file: docs (README/docs) | example | source | cấu hình cài đặt
    -> EXTRACTOR tất định (ast + regex) sinh *ứng viên* {key, value, source, confidence, ref, quote}
    -> [tùy chọn] ai_infer(digest): chỉ điền chỗ trống, luôn source=ai_inference, tối đa confidence=medium, KHÔNG ghi đè bằng chứng
    -> gộp: mỗi giá trị có source + confidence + evidence; mâu thuẫn được ghi lại (giới hạn số: chọn giá trị THẬN TRỌNG nhất)
    -> sinh: capabilities, profile annotated (candidate), luật ngắt đoạn, ứng viên adapter (spec cho CommandTTS | khung code), needs_user

Chỉ hỏi người dùng (`needs_user`) những gì không thể tự biết: credential, reference voice bắt buộc, tham số bắt buộc không có mặc định.
`source` chỉ là một trong: official_docs | source_code | official_example | runtime_test | ai_inference (| user | default).
Phần "runtime_test" do `autotune.py` điền sau khi chạy thử adapter; Analyzer thuần tĩnh KHÔNG chạy mã của repo.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Callable

from ..fsutil import atomic_write_json, atomic_write_text
from . import schema as S

SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "env", "__pycache__", ".idea", ".vscode", "dist", "build", "site-packages"}
DOC_EXT = {".md", ".rst", ".txt", ".adoc"}
MAX_FILE_BYTES = 400_000
MAX_FILES = 600
EXAMPLE_DIRS = {"example", "examples", "demo", "demos", "sample", "samples", "tutorial", "tutorials", "notebooks"}
INSTALL_FILES = {"requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "dockerfile", "environment.yml", "package.json"}
CONF_CAP = {"ai_inference": "medium"}

ROLES = {   # vai trò -> mẫu tên cờ CLI (so khớp không phân biệt hoa thường, bỏ '-'/'_')
    "text": r"text|input|prompt|sentence|content|t", "text_file": r"textfile|inputfile|file|txt|infile",
    "out": r"out|output|outputfile|outpath|outputpath|outfile|save|savepath|wav|outwav",
    "voice": r"voice|voiceid|speaker|speakerid|spk|voicename", "language": r"lang|language|langcode",
    "speed": r"speed|rate|tempo", "model": r"model|modelname|modelpath|ckpt|checkpoint|checkpointpath",
    "ref_audio": r"refaudio|reference|referenceaudio|promptaudio|speakerwav|refwav|ref|refpath|prompt_wav|promptwav",
    "device": r"device|gpu|cuda", "sample_rate": r"samplerate|sr|samplingrate", "emotion": r"emotion|style|mood",
}
LANG_NAMES = {"vietnamese": "vi", "english": "en", "chinese": "zh", "mandarin": "zh", "japanese": "ja", "korean": "ko",
              "french": "fr", "german": "de", "spanish": "es", "italian": "it", "portuguese": "pt", "russian": "ru",
              "thai": "th", "indonesian": "id", "hindi": "hi", "arabic": "ar", "turkish": "tr", "polish": "pl", "dutch": "nl"}
LANG_CODE = re.compile(r"""['"]([a-z]{2,3}(?:[-_][A-Za-z]{2,4})?)['"]""")


# ================================================================ nạp nguồn
def fetch_reference(ref: str, dest: Path, run: Callable = subprocess.run, urlopen: Callable | None = None) -> Path:
    """Đưa một tham chiếu về thư mục local: thư mục có sẵn | git repo (clone nông) | URL docs (tải về dạng text)."""
    p = Path(ref)
    if p.exists():
        return p
    dest.mkdir(parents=True, exist_ok=True)
    if re.match(r"^(git@|ssh://|https?://(www\.)?(github|gitlab|huggingface)\.(com|co)/)", ref) or ref.endswith(".git"):
        run(["git", "clone", "--depth", "1", ref, str(dest / "repo")], check=True, capture_output=True, timeout=600)
        return dest / "repo"
    if ref.startswith(("http://", "https://")):
        if urlopen is None:
            from urllib.request import urlopen as _u
            urlopen = _u
        html = urlopen(ref, timeout=60).read().decode("utf-8", "replace")
        text = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", "\n", text)
        d = dest / "docs"
        atomic_write_text(d / "page.md", f"<!-- {ref} -->\n" + re.sub(r"\n\s*\n+", "\n\n", text))
        return d
    raise FileNotFoundError(f"không hiểu tham chiếu TTS: {ref!r} (cần thư mục, git URL hoặc URL docs)")


def _scan(root: Path) -> list[tuple[Path, str]]:
    """[(file, loại)] với loại ∈ docs|example|source|install."""
    out = []
    for f in sorted(root.rglob("*")):
        if len(out) >= MAX_FILES:
            break
        rel = f.relative_to(root)
        if not f.is_file() or any(part in SKIP_DIRS for part in rel.parts) or f.stat().st_size > MAX_FILE_BYTES:
            continue
        low = f.name.lower()
        in_example = any(part.lower() in EXAMPLE_DIRS for part in rel.parts[:-1])
        if low in INSTALL_FILES:
            out.append((f, "install"))
        elif f.suffix.lower() in DOC_EXT:
            out.append((f, "example" if in_example else "docs"))
        elif f.suffix.lower() == ".py":
            out.append((f, "example" if in_example or low.startswith(("demo", "example")) else "source"))
        elif f.suffix.lower() == ".ipynb":
            out.append((f, "example"))
    return out


def _read(f: Path) -> str:
    t = f.read_text(encoding="utf-8", errors="replace")
    if f.suffix.lower() == ".ipynb":
        try:
            t = "\n".join("".join(c.get("source", [])) for c in json.loads(t).get("cells", []))
        except ValueError:
            pass
    return t


SRC_OF = {"docs": "official_docs", "example": "official_example", "source": "source_code", "install": "official_docs"}


class _Cands:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, key: str, value, kind: str, rel: str, line: int, quote: str, source: str | None = None) -> None:
        self.items.append({"key": key, "value": value, "source": source or SRC_OF[kind], "confidence": "medium",
                           "ref": f"{rel}:{line}", "quote": quote.strip()[:160]})


def _norm(s: str) -> str:
    return re.sub(r"[-_\s]", "", s.lstrip("-")).lower()


def role_of(flag: str) -> str | None:
    n = _norm(flag)
    for role in ("text_file", "ref_audio", "sample_rate", "text", "out", "voice", "language", "speed", "model", "device", "emotion"):
        if re.fullmatch(ROLES[role].replace("_", ""), n):
            return role
    return None


# ================================================================ extractors
def _argparse(tree: ast.AST, rel: str, kind: str, c: _Cands, args_out: list[dict]) -> None:
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"):
            continue
        flags = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.startswith("-")]
        if not flags:
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}

        def lit(n):
            try:
                return ast.literal_eval(n) if n is not None else None
            except (ValueError, SyntaxError):
                return None
        long = next((f for f in flags if f.startswith("--")), flags[0])
        info = {"flag": long, "role": role_of(long) or next((role_of(f) for f in flags if role_of(f)), None),
                "default": lit(kw.get("default")), "required": bool(lit(kw.get("required"))), "choices": lit(kw.get("choices")),
                "help": lit(kw.get("help")), "type": getattr(kw.get("type"), "id", None), "ref": f"{rel}:{node.lineno}",
                "file": rel, "action": lit(kw.get("action"))}
        args_out.append(info)
        c.add(f"cli.arg:{long}", {k: v for k, v in info.items() if k not in ("file",)}, kind, rel, node.lineno, f"add_argument({long})")


def _python_api(tree: ast.AST, rel: str, kind: str, c: _Cands) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and re.fullmatch(
                r"(synthesi[sz]e|tts|text_to_speech|infer|inference|generate|speak|say)(_\w+)?", node.name):
            names = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]
            if any(n in ("text", "sentence", "prompt", "content", "input_text") for n in names):
                defaults = node.args.defaults
                dv = {}
                for a, d in zip(node.args.args[len(node.args.args) - len(defaults):], defaults):
                    try:
                        dv[a.arg] = ast.literal_eval(d)
                    except (ValueError, SyntaxError):
                        pass
                c.add("api.python", {"module": rel, "function": node.name, "params": names, "defaults": dv}, kind, rel,
                      node.lineno, f"def {node.name}({', '.join(names)})")
        for dec in getattr(node, "decorator_list", []):
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in ("post", "get", "route") \
                    and dec.args and isinstance(dec.args[0], ast.Constant):
                path = str(dec.args[0].value)
                if re.search(r"tts|speech|synth|voice|audio|generate|infer", path, re.I):
                    c.add("api.http", {"route": path, "method": dec.func.attr.upper()}, kind, rel, node.lineno, f"@{dec.func.attr}({path!r})")


_RX_LINE = [
    ("install.command", re.compile(r"^\s*[$>]?\s*((?:pip3?|uv pip|conda|mamba|npm|apt(?:-get)?|brew|docker|git clone|poetry)\s+(?:install|run|pull|add|clone)\b.*)$"), 1),
    ("run.example", re.compile(r"^\s*[$>]?\s*(python3?\s+\S+\.py\s+.*(?:--text|--input|--prompt|--out)\S*.*|\S*tts\S*\s+.*--(?:text|input).*)$"), 1),
]
_LIMIT_CODE = re.compile(r"\b(?:max|limit)[_ ]?(chars|characters|length|text[_ ]?length|input[_ ]?length|len|tokens|words)\w*\s*[=:]\s*(\d{2,6})\b", re.I)
_LIMIT_DOC = re.compile(r"(?:up to|maximum(?: input)?(?: length)?(?: of| is|:)?|max(?:imum)?(?: input)?(?: length)?(?: of| is|:|=)?|limit(?:ed)? to|no more than|at most|tối đa)\s*(\d[\d,.]{1,7})\s*(characters|chars|tokens|words|ký tự|từ)\b", re.I)
_SR = re.compile(r"\b(?:sample[_ -]?rate|sampling[_ -]?rate|sr)\b\s*[=:]\s*(\d{4,6})\b", re.I)
_SR_DOC = re.compile(r"\b(\d{1,2}(?:\.\d)?)\s*kHz\b|\b(\d{4,6})\s*Hz\b", re.I)
_ENV = re.compile(r"""(?:os\.environ(?:\.get)?\s*[\[(]\s*|os\.getenv\s*\(\s*|export\s+|\$env:|^\s*)['"]?([A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|CREDENTIALS?|_KEY))\b""", re.M)
_MODEL = re.compile(r"""(?:from_pretrained|model_name|model_id|repo_id|checkpoint)\s*[=(:]\s*['"]([\w.-]+/[\w.-]+)['"]""")
_FLAGS = {
    "voice_cloning": re.compile(r"\b(voice[ -]clon\w*|zero[ -]shot|clone (?:a |your )?voice|reference (?:audio|voice|speaker))\b", re.I),
    "ssml": re.compile(r"\bssml\b", re.I), "streaming": re.compile(r"\bstream(?:ing)?\b", re.I),
    "batch": re.compile(r"\bbatch(?:ed)?\b", re.I), "emotion": re.compile(r"\b(emotion\w*|style transfer|expressive)\b", re.I),
    "gpu": re.compile(r"\b(cuda|gpu|nvidia)\b", re.I), "cpu": re.compile(r"\bcpu(?:[- ]only)?\b", re.I),
}
_FORMAT = re.compile(r"\.(wav|mp3|ogg|flac|opus)\b", re.I)
_NEG = re.compile(r"\b(not|no|never|without|unsupported|cannot|can't|isn't|aren't|doesn't|không|chưa)\b", re.I)
# cờ chỉ tin khi nằm trong tài liệu/ví dụ (từ "stream", "batch" trong code có quá nhiều nghĩa khác)
_FLAG_DOCS_ONLY = {"voice_cloning", "streaming", "batch", "emotion"}


def _lines(text: str):
    for i, ln in enumerate(text.splitlines(), 1):
        yield i, ln


def _text_extractors(text: str, rel: str, kind: str, c: _Cands) -> None:
    is_code = kind in ("source",) or rel.endswith(".py")
    for i, ln in _lines(text):
        for key, rx, g in _RX_LINE:
            if (m := rx.match(ln)) and (kind in ("docs", "install", "example")):
                c.add(key, m.group(g).strip().strip("`"), kind, rel, i, ln)
        if (m := (_LIMIT_CODE if is_code else _LIMIT_DOC).search(ln)):
            unit = (m.group(1) if is_code else m.group(2)).lower()
            n = int(re.sub(r"[,.]", "", m.group(2 if is_code else 1)))
            k = ("max_chars" if unit in ("chars", "characters", "length", "textlength", "text_length", "text length", "inputlength",
                                         "input_length", "input length", "len", "ký tự") else
                 "max_tokens" if unit == "tokens" else "max_words")
            if 10 <= n <= 1_000_000:
                c.add(k, n, kind, rel, i, ln)
        if (m := _SR.search(ln)):
            c.add("sample_rate", int(m.group(1)), kind, rel, i, ln)
        elif not is_code and (m := _SR_DOC.search(ln)):
            c.add("sample_rate", int(float(m.group(1)) * 1000) if m.group(1) else int(m.group(2)), kind, rel, i, ln)
        for m in _ENV.finditer(ln):
            c.add("credential.env", m.group(1), kind, rel, i, ln)
        if (m := _MODEL.search(ln)):
            c.add("model", m.group(1), kind, rel, i, ln)
        for flag, rx in _FLAGS.items():
            if flag in _FLAG_DOCS_ONLY and is_code:
                continue
            if rx.search(ln) and not ln.lstrip().startswith(("import ", "from ")):
                c.add(f"flag:{flag}", not _NEG.search(ln), kind, rel, i, ln)    # câu phủ định ("not available") là bằng chứng False
        if not is_code:
            low = ln.lower()
            for name, code in LANG_NAMES.items():
                if re.search(rf"\b{name}\b", low) and re.search(r"language|support|voice|multilingual|lang", low):
                    c.add("language", code, kind, rel, i, ln)
            for m in re.finditer(r"output[^.\n]{0,40}\.(wav|mp3|ogg|flac|opus)\b|(wav|mp3|ogg|flac|opus) (?:file|audio|output)", ln, re.I):
                c.add("output_format", (m.group(1) or m.group(2)).lower(), kind, rel, i, ln)
        else:
            if re.search(r"(lang|language)s?\w*\s*=\s*[\[{(]", ln, re.I):
                for code in LANG_CODE.findall(ln)[:60]:
                    c.add("language", code.replace("_", "-"), kind, rel, i, ln)
            if (m := re.search(r"\bvoices?\w*\s*=\s*[\[{(](.*)", ln, re.I)):
                for v in re.findall(r"""['"]([\w .-]{2,40})['"]""", m.group(1))[:50]:
                    c.add("voice.name", v, kind, rel, i, ln)
            for m in _FORMAT.finditer(ln):
                if re.search(r"save|write|output|sf\.write|wavfile|torchaudio", ln, re.I):
                    c.add("output_format", m.group(1).lower(), kind, rel, i, ln)


# ================================================================ gộp
def _group(items: list[dict]) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = {}
    for it in items:
        g.setdefault(it["key"], []).append(it)
    return g


def _evidence(its: list[dict], n: int = 5) -> list[dict]:
    seen, out = set(), []
    for it in sorted(its, key=lambda x: -S.SOURCE_RANK[x["source"]]):
        if it["ref"] not in seen:
            seen.add(it["ref"])
            out.append({"ref": it["ref"], "quote": it["quote"]})
    return out[:n]


def _merge_value(key: str, its: list[dict], bool_like: bool = False, numeric_min: bool = False, union: bool = False) -> dict | None:
    """Gộp các ứng viên của một khóa thành một Fact. source/evidence lấy từ các ứng viên ỦNG HỘ giá trị được chọn."""
    its = [i for i in its if i["source"] != "ai_inference"] or its
    if not its:
        return None
    kinds = {i["source"] for i in its}
    note = ""
    if union:
        value = sorted({i["value"] for i in its})
        support, conf = its, ("high" if len(kinds) > 1 else "medium")
    elif numeric_min:
        vals = sorted({i["value"] for i in its})
        value = vals[0]
        support = [i for i in its if i["value"] == value]
        if len(vals) > 1:
            note = f"mâu thuẫn giữa các nguồn {vals}: chọn giá trị thận trọng nhất"
        conf = "low" if len(vals) > 1 else ("high" if len({i["source"] for i in support}) > 1 else "medium")
    else:
        top = max(its, key=lambda i: S.SOURCE_RANK[i["source"]])
        value = top["value"]
        support = [i for i in its if i["value"] == value]
        distinct = {json.dumps(i["value"], sort_keys=True) for i in its}
        if len(distinct) > 1:
            note = f"mâu thuẫn giữa các nguồn: chọn theo nguồn xếp hạng cao nhất ({top['source']})"
        conf = "low" if len(distinct) > 1 else ("high" if len(kinds) > 1 else "medium")
    best_src = max((i["source"] for i in support), key=lambda s_: S.SOURCE_RANK[s_])
    return S.fact(value, best_src, conf, _evidence(support + [i for i in its if i not in support]), note)


def _ai_fill(cands: _Cands, ai_infer: Callable[[dict], list[dict]] | None, digest: dict) -> list[dict]:
    if not ai_infer:
        return []
    have = {i["key"] for i in cands.items}
    out = []
    for it in ai_infer(digest) or []:
        if it.get("key") in have or "value" not in it:                    # AI chỉ điền chỗ trống, không bao giờ ghi đè bằng chứng
            continue
        conf = it.get("confidence", "low")
        if S.CONF_RANK[conf] > S.CONF_RANK[CONF_CAP["ai_inference"]]:
            conf = CONF_CAP["ai_inference"]
        row = {"key": it["key"], "value": it["value"], "source": "ai_inference", "confidence": conf,
               "ref": it.get("ref", "ai"), "quote": str(it.get("quote", ""))[:160]}
        cands.items.append(row)
        out.append(row)
    return out


# ================================================================ phân tích
def analyze(root: Path, engine: str | None = None, ai_infer: Callable[[dict], list[dict]] | None = None) -> dict:
    """Quét `root` và trả kết quả đầy đủ: {engine, capabilities, profile, adapter, needs_user, candidates, files}."""
    root = Path(root)
    engine = engine or re.sub(r"[^\w.-]+", "_", root.name.lower()) or "tts"
    c, cli_args = _Cands(), []
    files = _scan(root)
    for f, kind in files:
        rel = f.relative_to(root).as_posix()
        text = _read(f)
        _text_extractors(text, rel, kind, c)
        if f.suffix == ".py":
            try:
                tree = ast.parse(text)
            except (SyntaxError, ValueError):
                continue
            _argparse(tree, rel, kind, c, cli_args)
            _python_api(tree, rel, kind, c)
    digest = {"engine": engine, "files": [f.relative_to(root).as_posix() for f, _ in files][:80],
              "cli_args": [a["flag"] for a in cli_args][:40], "keys_found": sorted({i["key"] for i in c.items})[:60]}
    ai_rows = _ai_fill(c, ai_infer, digest)
    g = _group(c.items)

    def one(key, **kw):
        return _merge_value(key, g[key], **kw) if key in g else None

    caps: dict = {}
    facts: dict = {}                                                   # đường dẫn trong capabilities/profile -> Fact
    for key, name, kw in (("max_chars", "max_chars", {"numeric_min": True}), ("sample_rate", "sample_rate", {"numeric_min": False}),
                          ("language", "languages", {"union": True}), ("voice.name", "voices", {"union": True}),
                          ("output_format", "output_formats", {"union": True})):
        f = one(key, **kw)
        if f:
            facts[name] = f
            caps[name] = f["value"]
    for flag in ("voice_cloning", "ssml", "streaming", "batch"):
        f = one(f"flag:{flag}", bool_like=True)
        if f:
            facts[flag] = f
            caps[flag] = bool(f["value"])
    caps["device"] = [d for d in ("gpu", "cpu") if f"flag:{d}" in g]
    roles: dict[str, dict] = {}
    for a in cli_args:
        if a["role"] and (a["role"] not in roles or not a["file"].endswith("__init__.py")):
            roles.setdefault(a["role"], a)
    caps["speed"] = bool(roles.get("speed"))
    if roles.get("speed"):
        facts["speed"] = S.fact(True, SRC_OF["source"], "medium", [{"ref": roles["speed"]["ref"], "quote": f"add_argument({roles['speed']['flag']})"}])
    needs: list[dict] = []
    ref_arg = roles.get("ref_audio")
    caps["requires_reference_audio"] = bool(ref_arg and ref_arg["required"])
    if ref_arg:
        caps["voice_cloning"] = True
        facts.setdefault("voice_cloning", S.fact(True, "source_code", "medium", [{"ref": ref_arg["ref"], "quote": f"add_argument({ref_arg['flag']})"}]))
    if caps["requires_reference_audio"]:
        needs.append({"key": "settings.reference_audio", "reason": "required_reference_voice",
                      "detail": f"engine bắt buộc audio tham chiếu ({ref_arg['flag']}); không thể suy ra", "ref": ref_arg["ref"]})
    optional_env: set[str] = set()
    for it in g.get("credential.env", []):
        if any(n["key"] == f"env:{it['value']}" for n in needs):
            continue
        opt = bool(re.search(r"\boptional\b", it["quote"], re.I))
        if opt:
            optional_env.add(it["value"])
        needs.append({"key": f"env:{it['value']}", "reason": "credential_optional" if opt else "credential",
                      "detail": "đặt biến môi trường này (không ghi vào profile)" + (" — tài liệu ghi là tùy chọn" if opt else ""),
                      "ref": it["ref"]})
    caps["engine_version"] = ""
    caps = S.normalize_capabilities(caps)

    # ---- adapter ứng viên
    adapter = _adapter_candidate(engine, root, roles, cli_args, g, caps, needs, optional_env)
    # ---- profile annotated
    prof = S.new_annotated(engine)
    prof["status"] = "candidate"
    seg = {}
    if "max_chars" in facts:
        seg["max_chars"] = facts["max_chars"]
        seg["preferred_chars"] = S.fact(max(20, round(facts["max_chars"]["value"] * 0.6 / 10) * 10), "default", "low",
                                        note="quy tắc kinh nghiệm: 60% giới hạn tài liệu; xác nhận bằng Auto Tune")
        prof["meta"]["needs_tune"] = False
    else:
        prof["meta"]["needs_tune"] = True                              # không tìm thấy giới hạn: dùng mặc định + nên chạy Auto Tune
        seg["max_chars"] = S.fact(S.DEFAULT_FLAT["segment"]["max_chars"], "default", "low", note="không tìm thấy giới hạn trong nguồn; chạy Auto Tune")
        seg["preferred_chars"] = S.fact(S.DEFAULT_FLAT["segment"]["preferred_chars"], "default", "low")
    seg["min_chars"] = S.fact(S.DEFAULT_FLAT["segment"]["min_chars"], "default", "low")
    prof["segment"] = seg
    prof["break_priority"] = S.fact(S.DEFAULT_FLAT["break_priority"], "default", "low")
    prof["pause_ms"] = {k: S.fact(v, "default", "low") for k, v in S.DEFAULT_FLAT["pause_ms"].items()}
    langs = caps["languages"]
    prof["joiner"] = S.fact("" if langs and all(x.split("-")[0] in ("zh", "ja") for x in langs) else " ", "default", "low",
                            note="không dùng dấu cách giữa các segment nếu mọi ngôn ngữ hỗ trợ là zh/ja")
    settings = {}
    for role, key in (("speed", "speed"), ("device", "device"), ("emotion", "emotion"), ("sample_rate", "sample_rate")):
        a = roles.get(role)
        if a and a["default"] is not None:
            settings[key] = S.fact(a["default"], "source_code", "medium", [{"ref": a["ref"], "quote": f"add_argument({a['flag']}, default={a['default']!r})"}])
    prof["settings"] = settings
    for role, name in (("voice", "voice"), ("model", "model"), ("language", "language")):
        a = roles.get(role)
        if a and a["default"] is not None:
            prof[name] = S.fact(a["default"], "source_code", "medium", [{"ref": a["ref"], "quote": f"add_argument({a['flag']}, default={a['default']!r})"}])
    if "model" not in prof and (f := one("model")):
        prof["model"] = f
    prof["needs_user"] = needs
    prof["capabilities"] = caps
    prof["meta"].update({"analyzed_files": len(files), "capability_facts": facts,
                         "ai_rows": len(ai_rows), "adapter_kind": adapter["kind"], "adapter_ready": adapter["ready"]})
    errs = S.validate_annotated(prof)
    if errs:
        raise ValueError("profile sinh ra không hợp lệ: " + "; ".join(errs))
    return {"engine": engine, "capabilities": caps, "profile": prof, "adapter": adapter, "needs_user": needs,
            "candidates": c.items, "files": [{"path": f.relative_to(root).as_posix(), "kind": k} for f, k in files]}


# ================================================================ ứng viên adapter
def _adapter_candidate(engine: str, root: Path, roles: dict, cli_args: list, g: dict, caps: dict, needs: list,
                       optional_env: set) -> dict:
    out_r, text_r, tf_r = roles.get("out"), roles.get("text"), roles.get("text_file")
    if out_r and (text_r or tf_r):
        entry = out_r["file"]
        argv = ["python", entry]
        via = "arg"
        if tf_r and not text_r:
            argv += [tf_r["flag"], "{text_file}"]
            via = "file"
        else:
            argv += [text_r["flag"], "{text}"]
        argv += [out_r["flag"], "{out}"]
        optional = {}
        for role, key in (("voice", "voice"), ("language", "language"), ("model", "model"), ("speed", "settings.speed"),
                          ("device", "settings.device"), ("emotion", "settings.emotion"), ("sample_rate", "settings.sample_rate"),
                          ("ref_audio", "settings.reference_audio")):
            a = roles.get(role)
            if a:
                optional[key] = [a["flag"], "{" + key + "}"]
        mapped = {a["flag"] for a in roles.values()}
        for a in cli_args:
            if a["file"] == entry and a["required"] and a["flag"] not in mapped:
                needs.append({"key": f"cli_arg:{a['flag']}", "reason": "required_parameter",
                              "detail": f"tham số bắt buộc không có mặc định/không nhận diện được: {a['flag']}", "ref": a["ref"]})
        env_req = sorted({i["value"] for i in g.get("credential.env", [])} - optional_env)
        spec = {"engine_id": engine, "command": argv, "text_via": via, "output": "file", "cwd": str(root.resolve()), "timeout_s": 120,
                "optional_args": optional, "env_required": env_req,
                "capabilities": {k: v for k, v in caps.items() if v not in (None, [], "", False) or k in ("speed", "requires_reference_audio")}}
        return {"kind": "command", "ready": True, "confidence": "medium", "spec": spec,
                "config": {"adapters": {"tts": "contentfactory.adapters.command_tts:CommandTTS"}, "adapter_config": {"tts": spec}},
                "note": "dùng CommandTTS có sẵn; chưa kiểm chứng bằng runtime_test (chạy Auto Tune/probe để xác nhận)"}
    if (api := g.get("api.http")):                                  # server HTTP: hàm xử lý route không phải API Python để gọi
        a = api[0]["value"]
        return {"kind": "http", "ready": False, "confidence": "low", "spec": a, "code": _skeleton(engine, caps, "http", a),
                "note": "phát hiện endpoint HTTP; chưa có adapter HTTP tổng quát (cần viết bằng tay hoặc Phase sau)"}
    if (api := g.get("api.python")):
        a = max(api, key=lambda x: S.SOURCE_RANK[x["source"]])["value"]
        return {"kind": "python", "ready": False, "confidence": "low", "spec": a, "code": _skeleton(engine, caps, "python", a),
                "note": "khung adapter gọi hàm Python tìm thấy; cần kiểm tra cách khởi tạo model/tham số trước khi dùng"}
    return {"kind": "unknown", "ready": False, "confidence": "low", "spec": None,
            "note": "không tìm thấy CLI/hàm Python/endpoint để gọi: cần thêm docs hoặc ví dụ chạy"}


_SKEL = '''"""Adapter ứng viên cho {engine} — SINH TỰ ĐỘNG, CHƯA KIỂM CHỨNG. Kiểm tra rồi nạp bằng "package.module:Class" trong config."""
from pathlib import Path

from contentfactory.contracts import ErrorClass, StageContext, StageError


class {cls}:
    engine_id = {engine!r}

    def __init__(self, config=None):
        self.config = config or {{}}

    def capabilities(self) -> dict:
        return {caps!r}

    def health(self) -> dict:
        return {{"ok": False, "note": "chưa triển khai"}}

    def synthesize(self, segment: dict, profile: dict, out_path: Path, ctx: StageContext) -> dict:
        # Phát hiện từ nguồn: {detail!r}
        # TODO: gọi engine, ghi WAV vào out_path.with_name(out_path.name + ".part") rồi os.replace(...) (ghi atomic).
        raise StageError(ErrorClass.POLICY, "ADAPTER_NOT_IMPLEMENTED", "adapter ứng viên chưa hoàn thiện")
'''


def _skeleton(engine: str, caps: dict, kind: str, detail: dict) -> str:
    cls = "".join(p.capitalize() for p in re.split(r"[^0-9A-Za-z]+", engine) if p) + "TTS"
    return _SKEL.format(engine=engine, cls=cls or "CandidateTTS", caps={k: v for k, v in caps.items() if v not in (None, [], "")},
                        detail=detail)


# ================================================================ ghi kết quả
def write_onboarding(result: dict, out_dir: Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    paths = {"profile": atomic_write_json(out_dir / "profile.candidate.json", result["profile"]),
             "analysis": atomic_write_json(out_dir / "analysis.json", {k: result[k] for k in ("engine", "capabilities", "candidates", "files", "adapter")}),
             "needs_user": atomic_write_json(out_dir / "needs_user.json", result["needs_user"])}
    ad = result["adapter"]
    if ad.get("config"):
        paths["config_snippet"] = atomic_write_json(out_dir / "config.snippet.json", ad["config"])
    if ad.get("code"):
        paths["adapter_code"] = atomic_write_text(out_dir / "adapter_candidate.py", ad["code"])
    return paths
