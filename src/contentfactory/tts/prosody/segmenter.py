"""Segmenter nhận biết tiếng Việt: đoạn -> câu -> ranh giới nhỏ (micro). Thuần, tất định, không LLM.

Không tách câu bằng `re.split(r'[.!?]')`. Một dấu kết thúc chỉ là RANH GIỚI CÂU khi:
  - sau nó có khoảng trắng rồi mới tới ký tự kế (không phải 1.5, 12.10.2026, example.com, TP.HCM, U.S.A);
  - từ đứng trước không phải viết tắt không bao giờ kết thúc câu (TP., Mr., GS., TS., ThS., BS., ...);
  - `v.v.`/`etc.` chỉ kết thúc câu khi chữ kế tiếp viết hoa; chữ cái đầu tên riêng đứng một mình (Nguyễn V. An) không kết thúc câu;
  - chữ kế tiếp không phải chữ thường (câu tiếng Việt mở đầu bằng chữ hoa) — nên `“Anh đi đâu?” cô hỏi.` là MỘT câu (lời dẫn viết thường),
    còn `“Anh đi đâu?” Cô đứng dậy.` là hai câu;
  - `…` + chữ thường là ngập ngừng giữa câu (`Tôi… tôi không biết.`), `…` + chữ hoa/hết đoạn mới là ranh giới.
Dấu `, ; :` và `—` KHÔNG tách câu và KHÔNG tách lời gọi TTS: chúng chỉ được ghi lại thành ranh giới nhỏ (micro) để QC/engine có ngữ cảnh,
và là ứng viên cắt khi một câu vượt giới hạn của engine (cắt ở ranh giới ngôn ngữ tốt nhất, không bao giờ giữa từ).
"""
from __future__ import annotations

import re

SCENE_MARK = "§§SCENE§§"
_SCENE_LINE = re.compile(r"^[ \t]*(?:(?:[*\-_=#~][ \t]*){3,}|⁂)[ \t]*$", re.M)
_BLANKS = re.compile(r"\n[ \t]*\n(?:[ \t]*\n){2,}")
_TERM = ".!?…"
_CLOSE = set("\"”’'»)]}")
_DIALOGUE_START = re.compile(r"^[\"“‘'«—–-]")
# từ viết tắt KHÔNG BAO GIỜ kết thúc câu (so khớp không phân biệt hoa thường, bỏ dấu chấm)
_NEVER_END = {"tp", "tx", "mr", "mrs", "ms", "dr", "gs", "pgs", "ts", "ths", "bs", "ks", "ls", "st", "prof", "sr", "jr", "tt", "vs", "vd", "tr"}
_MAY_END = {"v.v", "vv", "etc", "tp.hcm"}
# cắt SAU các từ này làm vỡ nghĩa cụm (bởi vì, nếu như, mặc dù, không những, rằng, của, với, để ...)
_BAD_END = {"bởi", "nếu", "mặc", "không", "vì", "dù", "rằng", "của", "với", "để", "và", "hoặc", "nhưng", "mà", "thì", "là", "cho", "tại", "do", "khi", "như"}
_WORD = re.compile(r"[\w'’-]+", re.U)


def mark_scenes(text: str) -> str:
    """Thay dòng phân cảnh tường minh (`***`, `---`, `* * *`, `###`, `⁂`) và khoảng ≥3 dòng trống bằng một đoạn dấu `SCENE_MARK`.
    Chạy TRƯỚC Normalizer (Normalizer gỡ markdown nên sẽ làm mất các dòng này). Xuống dòng thường KHÔNG phải cảnh mới."""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = _BLANKS.sub(f"\n\n{SCENE_MARK}\n\n", t)
    return _SCENE_LINE.sub(f"\n{SCENE_MARK}\n", t)


def strip_marks(text: str) -> str:
    """Văn bản không còn dấu phân cảnh (để kiểm tra phủ văn bản khớp và tính khóa cache)."""
    return re.sub(r"\n\s*\n", "\n\n", "\n\n".join(p for p in re.split(r"\n\s*\n", text) if p.strip() and p.strip() != SCENE_MARK)).strip()


def paragraphs(text: str) -> list[dict]:
    """[{"text", "dialogue", "scene_after"}] — dấu phân cảnh gắn vào đoạn đứng trước (`scene_after`), không thành đoạn riêng."""
    out: list[dict] = []
    for p in (x.strip() for x in re.split(r"\n\s*\n", text)):
        if not p:
            continue
        if p == SCENE_MARK:
            if out:
                out[-1]["scene_after"] = True
            continue
        out.append({"text": p, "dialogue": bool(_DIALOGUE_START.match(p)), "scene_after": False})
    return out


def _is_digit(c: str) -> bool:
    return c.isdigit()


def _next_is_lower(s: str, k: int) -> bool:
    """Ký tự chữ kế tiếp (bỏ ngoặc/dấu gạch mở đầu thoại) có phải chữ thường không."""
    while k < len(s) and (s[k] in "\"“‘'«([—–- " or s[k] in "—–"):
        k += 1
    return k < len(s) and s[k].isalpha() and s[k].islower()


def _micro(sentence: str) -> list[dict]:
    """Ranh giới nhỏ trong một câu: {"kind", "offset"} (offset = ngay SAU dấu). Bỏ qua dấu thập phân/nghìn và giờ (1,5 / 10:30)."""
    out = []
    for i, c in enumerate(sentence):
        prev_d = i > 0 and _is_digit(sentence[i - 1])
        next_d = i + 1 < len(sentence) and _is_digit(sentence[i + 1])
        if c == "," and not (prev_d and next_d):
            out.append({"kind": "comma", "offset": i + 1})
        elif c == ";":
            out.append({"kind": "semicolon", "offset": i + 1})
        elif c == ":" and not (prev_d and next_d):
            out.append({"kind": "colon", "offset": i + 1})
        elif c == "…" and 0 < i < len(sentence) - 1 and sentence[i + 1:].strip("".join(_CLOSE) + " ") != "":
            out.append({"kind": "ellipsis_hesitation", "offset": i + 1})
        elif c in "—–" and 0 < i < len(sentence) - 1 and sentence[i - 1] == " ":
            out.append({"kind": "comma", "offset": i + 1})
    return out


def split_sentences(par: str) -> list[dict]:
    """Câu của một đoạn: [{"text", "end": sentence|question|exclamation|ellipsis|none, "micro": [...]}]."""
    out: list[dict] = []
    n, start, i = len(par), 0, 0
    tail_kind = "none"
    while i < n:
        if par[i] not in _TERM:
            i += 1
            continue
        j = i
        while j < n and par[j] in _TERM:
            j += 1
        run = par[i:j]
        kind = "ellipsis" if ("…" in run or run.count(".") >= 3) else "question" if "?" in run else "exclamation" if "!" in run else "sentence"
        e = j
        while e < n and par[e] in _CLOSE:
            e += 1
        k = e
        while k < n and par[k].isspace():
            k += 1
        if k >= n:                                                     # hết đoạn: dấu cuối quyết định loại câu cuối
            tail_kind = kind
            i = n
            break
        if k == e:                                                     # không có khoảng trắng sau dấu: 1.5, example.com, 12.10.2026, TP.HCM
            i = e
            continue
        c = par[k]
        boundary = True
        if kind == "sentence":
            m = re.search(r"([\w.]+)$", par[start:i])
            token = m.group(1) if m else ""
            low = token.lower().strip(".")
            words = par[start:i].split()
            if low in _NEVER_END:
                boundary = False
            elif low in _MAY_END:
                boundary = not c.islower()
            elif re.fullmatch(r"[A-ZĐ]", token) and len(words) >= 2 and words[-2][:1].isupper() and (c.isupper() or c.isdigit()):
                boundary = False                                       # Nguyễn V. An
            elif _next_is_lower(par, k):
                boundary = False
        elif _next_is_lower(par, k):                                   # “Anh đi đâu?” cô hỏi. / Tôi… tôi không biết.
            boundary = False
        if boundary:
            text = par[start:e].strip()
            if text:
                out.append({"text": text, "end": kind})
            start = k
        i = e
    tail = par[start:].strip()
    if tail:
        out.append({"text": tail, "end": tail_kind})
    for s in out:
        s["micro"] = _micro(s["text"])
    return out


# ---------------------------------------------------------------- cắt câu quá dài theo ranh giới ngôn ngữ
def _last_word(s: str) -> str:
    ws = _WORD.findall(s.lower())
    return ws[-1].strip("-'’") if ws else ""


def bad_cut(left: str, right: str) -> bool:
    """Cắt giữa `left|right` có làm vỡ cụm không (bởi|vì, nếu|như, mặc|dù, không|những, kết thúc bằng rằng/của/với/để...)."""
    return _last_word(left) in _BAD_END or not left.strip() or not right.strip()


def split_long(sentence: str, max_chars: int) -> list[dict]:
    """Cắt một câu dài hơn `max_chars` thành các mảnh ≤ max_chars. Ưu tiên ; > : > , / — > khoảng trắng, gần giới hạn nhất, tránh cắt vỡ cụm.
    Trả [{"text", "cut": semicolon|colon|comma|space|hard|None}] (cut = cách cắt SAU mảnh; None cho mảnh cuối)."""
    out: list[dict] = []
    rest = sentence.strip()
    while len(rest) > max_chars:
        best = None
        for lo_ratio in (0.4, 0.2):
            lo = max(8, int(max_chars * lo_ratio))
            cands = []
            for p in range(lo, max_chars + 1):
                left, right = rest[:p].rstrip(), rest[p:].lstrip()
                c = rest[p - 1]
                if c == ";" and rest[p:p + 1] == " ":
                    pr, kind = 5, "semicolon"
                elif c == ":" and rest[p:p + 1] == " ":
                    pr, kind = 4, "colon"
                elif c in ",，、" and rest[p:p + 1] == " ":
                    pr, kind = 3, "comma"
                elif rest[p:p + 1] == " " and c in "—–" or rest[p:p + 3] in (" — ", " – "):
                    pr, kind = 3, "comma"
                elif rest[p:p + 1] == " ":
                    pr, kind = 1, "space"
                else:
                    continue
                if not bad_cut(left, right):
                    cands.append((pr, p, kind))
            if cands:
                best = max(cands)
                break
        if best is None:                                               # không có ranh giới sạch: khoảng trắng gần giới hạn nhất, cuối cùng mới cắt cứng (CJK)
            sp = rest.rfind(" ", 0, max_chars + 1)
            best = (0, sp, "space") if sp > 0 else (0, max_chars, "hard")
        _, p, kind = best
        out.append({"text": rest[:p].strip(), "cut": kind})
        rest = rest[p:].strip()
    if rest:
        out.append({"text": rest, "cut": None})
    return out
