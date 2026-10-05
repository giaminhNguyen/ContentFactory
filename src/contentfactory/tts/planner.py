"""Segment Planner (rule + AI) và Validator tất định.

  RuleSegmentPlanner : tất định, theo đoạn/câu, dùng làm mặc định và làm phương án dự phòng.
  AISegmentPlanner   : nhờ LLM *gom câu* thành segment. LLM chỉ được trả CHỈ SỐ CÂU (không được viết lại văn bản), nên
                       không thể làm thay đổi nội dung; dù vậy mọi kế hoạch (của ai cũng vậy) vẫn phải qua `validate_plan`.
  validate_plan      : luật cứng, không tin planner: không mất/lặp/đổi chữ, không vượt max_chars, không cắt giữa từ,
                       index liên tục, pause hợp lệ. Lỗi => Manager thử lại planner kèm phản hồi rồi rơi về RuleSegmentPlanner.

Planner chỉ biết text + profile FLAT (schema.resolve); không biết engine nào.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Protocol

from ..contracts import Segment

_SENT = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…][\"”’)\]])\s+")
_DIALOGUE = re.compile(r"^[\"“‘'—–-]")
_END = re.compile(r"[.!?…][\"”’)\]]*\s*$")
_CUTS = (re.compile(r"[;；]\s"), re.compile(r"[:：]\s"), re.compile(r"[,，、]\s"), re.compile(r"\s[—–-]\s"), re.compile(r"\s"))
MAX_PAUSE_MS = 10_000


class PlanError(Exception):
    """Planner không tạo được kế hoạch (vd LLM trả JSON hỏng); Manager sẽ thử lại/ rơi về rule."""


class SegmentPlanner(Protocol):
    name: str

    def plan(self, text: str, profile: dict, feedback: list[str] | None = None) -> list[Segment]: ...


# ---------------------------------------------------------------- tách đoạn / câu
def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def split_long(sentence: str, max_chars: int, joiner: str = " ") -> list[str]:
    """Cắt một câu dài hơn max_chars tại ranh giới tốt nhất (; : , — khoảng trắng); không có thì cắt cứng."""
    out, s = [], sentence.strip()
    while len(s) > max_chars:
        window, cut = s[: max_chars + 1], 0
        for rx in _CUTS:
            ms = [m for m in rx.finditer(window) if m.end() >= max_chars * 0.4]
            if ms:
                cut = ms[-1].end() if rx.pattern != r"\s" else ms[-1].start()
                break
        if not cut or cut > max_chars:
            cut = max_chars                                    # không có ranh giới (vd chữ Hán): cắt cứng
        out.append(s[:cut].strip())
        s = s[cut:].strip()
    if s:
        out.append(s)
    return out


def sentences_of(paragraph: str, max_chars: int, joiner: str = " ") -> list[str]:
    out: list[str] = []
    for s in (x.strip() for x in _SENT.split(paragraph) if x.strip()):
        out += split_long(s, max_chars, joiner)
    return out


def _pause_for(para: str, flat: dict) -> int:
    return int(flat["pause_ms"]["dialogue"] if _DIALOGUE.match(para) else flat["pause_ms"]["paragraph"])


# ---------------------------------------------------------------- rule planner
class RuleSegmentPlanner:
    name = "rule"

    def plan(self, text: str, profile: dict, feedback: list[str] | None = None) -> list[Segment]:
        seg, joiner = profile["segment"], profile["joiner"]
        pref, mx, mn = int(seg["preferred_chars"]), int(seg["max_chars"]), int(seg["min_chars"])
        out: list[Segment] = []
        for para in paragraphs(text):
            local: list[dict] = []
            buf = ""
            for s in sentences_of(para, mx, joiner):
                if buf and len(buf) + len(joiner) + len(s) > pref:
                    local.append({"text": buf, "pause_after_ms": int(profile["pause_ms"]["sentence"])})
                    buf = s
                else:
                    buf = f"{buf}{joiner}{s}" if buf else s
            if buf:
                local.append({"text": buf, "pause_after_ms": _pause_for(para, profile)})
            if len(local) > 1 and len(local[-1]["text"]) < mn and \
                    len(local[-2]["text"]) + len(joiner) + len(local[-1]["text"]) <= mx:     # không để lại mẩu cụt
                local[-2] = {"text": f"{local[-2]['text']}{joiner}{local[-1]['text']}",
                             "pause_after_ms": local[-1]["pause_after_ms"]}
                local.pop()
            for seg_ in local:
                out.append({"index": len(out) + 1, **seg_})
        return out


# ---------------------------------------------------------------- AI planner
PROMPT = """Bạn là bộ lập kế hoạch ngắt đoạn cho TTS. Dưới đây là các câu đã đánh số, nhóm theo đoạn văn (dòng "--- đoạn ---" là ranh giới đoạn).
Hãy GOM các câu liên tiếp thành segment để đọc tự nhiên. Quy tắc:
- Mỗi segment tối đa {max_chars} ký tự (cộng các câu và dấu cách), nên khoảng {preferred_chars} ký tự.
- Giữ các câu theo đúng thứ tự, KHÔNG bỏ câu, KHÔNG lặp câu, KHÔNG viết lại chữ nào.
- Không gom câu của hai đoạn văn khác nhau vào một segment.
- Ưu tiên ngắt ở cuối câu; hội thoại nên tách khỏi lời dẫn.
- "pause": "paragraph" nếu segment kết thúc đoạn văn, "dialogue" nếu kết thúc lời thoại, "sentence" nếu còn tiếp trong đoạn.
Chỉ trả JSON: {{"segments": [{{"sentences": [1, 2], "pause": "sentence"}}, ...]}}
{feedback}
{body}"""


class AISegmentPlanner:
    """`llm(prompt) -> str` do bên ngoài tiêm vào (vd gọi Claude CLI); planner không biết LLM nào."""
    name = "ai"

    def __init__(self, llm: Callable[[str], str], window_sentences: int = 120) -> None:
        self.llm, self.window = llm, window_sentences

    def plan(self, text: str, profile: dict, feedback: list[str] | None = None) -> list[Segment]:
        mx, joiner = int(profile["segment"]["max_chars"]), profile["joiner"]
        paras = [(p, sentences_of(p, mx, joiner)) for p in paragraphs(text)]
        out: list[Segment] = []
        for win in self._windows(paras):
            flat = [s for _, ss in win for s in ss]
            body, n = [], 0
            for p, ss in win:
                body.append("--- đoạn ---")
                for s in ss:
                    n += 1
                    body.append(f"{n}. {s}")
            raw = self.llm(PROMPT.format(max_chars=mx, preferred_chars=profile["segment"]["preferred_chars"],
                                         feedback=("Lần trước kế hoạch bị từ chối vì: " + "; ".join(feedback)) if feedback else "",
                                         body="\n".join(body)))
            try:
                groups = json.loads(re.search(r"\{.*\}", raw, re.S).group(0))["segments"]
                for g in groups:
                    ids = [int(i) for i in g["sentences"]]
                    pause = {"paragraph": profile["pause_ms"]["paragraph"], "dialogue": profile["pause_ms"]["dialogue"],
                             "sentence": profile["pause_ms"]["sentence"]}[g.get("pause", "sentence")]
                    out.append({"index": len(out) + 1, "text": joiner.join(flat[i - 1] for i in ids),
                                "pause_after_ms": int(pause)})
            except (AttributeError, KeyError, ValueError, TypeError, IndexError) as e:
                raise PlanError(f"phản hồi LLM không đúng định dạng: {e!r}") from None
        return out

    def _windows(self, paras: list[tuple[str, list[str]]]):
        win, n = [], 0
        for p in paras:
            if win and n + len(p[1]) > self.window:
                yield win
                win, n = [], 0
            win.append(p)
            n += len(p[1])
        if win:
            yield win


# ---------------------------------------------------------------- validator
def _collapse(s: str, joiner: str) -> str:
    return re.sub(r"\s+", "", s) if joiner == "" else " ".join(s.split())


def validate_plan(segments: list[Segment], text: str, profile: dict) -> dict:
    """{"errors": [{code, index, message}], "warnings": [...]}. Rỗng errors = kế hoạch dùng được."""
    errs: list[dict] = []
    warns: list[dict] = []
    mx, mn = int(profile["segment"]["max_chars"]), int(profile["segment"]["min_chars"])
    joiner = profile["joiner"]
    para_pause = int(profile["pause_ms"]["paragraph"])

    def e(code, i, msg):
        errs.append({"code": code, "index": i, "message": msg})

    if not segments:
        e("EMPTY_PLAN", None, "không có segment")
        return {"errors": errs, "warnings": warns}
    for pos, s in enumerate(segments, 1):
        i = s.get("index")
        if i != pos:
            e("BAD_INDEX", i, f"index phải liên tục từ 1 (vị trí {pos} có {i!r})")
        t = s.get("text", "")
        if not t.strip():
            e("EMPTY_SEGMENT", i, "segment rỗng")
        elif not re.search(r"\w", t):
            e("NO_SPEECH", i, "segment không có chữ/số để đọc")
        if len(t) > mx:
            e("TOO_LONG", i, f"{len(t)} > max_chars={mx}")
        p = s.get("pause_after_ms")
        if not isinstance(p, int) or isinstance(p, bool) or not 0 <= p <= MAX_PAUSE_MS:
            e("BAD_PAUSE", i, f"pause_after_ms={p!r} ngoài [0, {MAX_PAUSE_MS}]")
        if t.strip() and len(t) < mn and pos != len(segments) and (p or 0) < para_pause:
            warns.append({"code": "SHORT", "index": i, "message": f"{len(t)} < min_chars={mn}"})
        if t.strip() and not _END.search(t) and (p or 0) < para_pause:
            warns.append({"code": "CUT_MID_SENTENCE", "index": i, "message": "segment không kết thúc bằng dấu câu"})
    want, got = _collapse(text, joiner), _collapse(joiner.join(s.get("text", "") for s in segments), joiner)
    if want != got:
        k = next((n for n, (a, b) in enumerate(zip(want, got)) if a != b), min(len(want), len(got)))
        e("TEXT_MISMATCH", None, f"nội dung segment không khớp văn bản gốc (khác từ ký tự {k}: "
                                 f"{want[max(0, k - 15):k + 15]!r} vs {got[max(0, k - 15):k + 15]!r}); có thể mất/lặp/đổi chữ hoặc cắt giữa từ")
    return {"errors": errs, "warnings": warns}
