"""Giới hạn + kiểm tất định metadata YouTube dùng chung (module dùng chung như contracts/fsutil: chỉ phụ thuộc contracts).

`output.metadata` (dựng title/mô tả) và `publish.stage` (kiểm lần cuối trước uploader) cùng dùng đây nên không có hai bộ luật lệch nhau và hai package không import chéo nhau.
"""
from __future__ import annotations

from .contracts import ErrorClass, StageError

# Nguồn các giới hạn: modules/yt_uploader/internal/upload/validate.go (daemon kiểm TRƯỚC khi gọi YouTube; mỗi số có tài liệu YouTube Data API phía sau).
# Title: đếm ký tự Unicode (rune) = len(str) của Python. Description: BYTE UTF-8. Tags: tổng 500, daemon tính len(tag) theo BYTE + 2 nếu tag có dấu cách/phẩy.
TITLE_MAX_CHARS = 100
TITLE_TARGET_CHARS = 90              # mục tiêu khi AI đặt tên (đệm dưới trần cứng 100 vì LLM đếm ký tự không chuẩn)
DESCRIPTION_MAX_BYTES = 5000
DESCRIPTION_TARGET_BYTES = 4500
TAGS_MAX_CHARS = 500
PRIVACY_VALUES = ("private", "unlisted", "public")


def _bad(code: str, msg: str, detail: dict | None = None) -> StageError:
    return StageError(ErrorClass.POLICY, code, msg, detail)


def utf8_len(s: str) -> int:
    """Độ dài BYTE UTF-8 (không phải số ký tự: tiếng Việt có dấu 2-3 byte/ký tự). Surrogate lẻ không mã hóa được ⇒ INVALID_METADATA."""
    try:
        return len(s.encode("utf-8"))
    except UnicodeEncodeError:
        raise _bad("INVALID_METADATA", "văn bản chứa ký tự không hợp lệ (surrogate lẻ), không mã hóa được UTF-8") from None


def check_title(title) -> str:
    """Tiêu đề YouTube cuối cùng: chuỗi, không rỗng, 1 dòng, không '<' '>' (YouTube Data API cấm), ≤ 100 ký tự. KHÔNG bao giờ cắt."""
    if not isinstance(title, str):
        raise _bad("INVALID_METADATA", f"title phải là chuỗi, nhận {type(title).__name__}")
    t = title.strip()
    if not t or "\n" in t or "\r" in t:
        raise _bad("INVALID_TITLE", "tiêu đề YouTube rỗng hoặc có xuống dòng")
    utf8_len(t)
    if "<" in t or ">" in t:
        raise _bad("INVALID_METADATA", "tiêu đề YouTube không được chứa '<' hoặc '>' (YouTube từ chối); đổi project.title/title_template", {"title": t})
    if len(t) > TITLE_MAX_CHARS:
        raise _bad("TITLE_TOO_LONG", f"tiêu đề YouTube dài {len(t)} ký tự (> {TITLE_MAX_CHARS}); rút gọn project.title hoặc title_template, KHÔNG tự cắt",
                   {"title": t, "length": len(t), "limit": TITLE_MAX_CHARS})
    return t


def check_description(desc) -> str:
    """Mô tả: chuỗi, không '<' '>', ≤ 5000 BYTE UTF-8. Template do chủ kênh viết nên không biết đoạn nào "ít quan trọng" (có thể là credit/disclosure)
    ⇒ vượt thì BÁO LỖI rõ, không cắt."""
    if not isinstance(desc, str):
        raise _bad("INVALID_METADATA", f"description phải là chuỗi, nhận {type(desc).__name__}")
    d = desc.strip()
    n = utf8_len(d)
    if "<" in d or ">" in d:
        raise _bad("INVALID_METADATA", "mô tả YouTube không được chứa '<' hoặc '>' (YouTube từ chối); sửa description_template")
    if n > DESCRIPTION_MAX_BYTES:
        raise _bad("DESCRIPTION_TOO_LONG", f"mô tả dài {n} byte (> {DESCRIPTION_MAX_BYTES}; {len(d)} ký tự)", {"bytes": n, "chars": len(d), "limit": DESCRIPTION_MAX_BYTES})
    return d


def tags_cost(tags: list[str]) -> int:
    """Giống daemon: len(tag) theo BYTE UTF-8 (chặt hơn đếm ký tự), +2 nếu tag chứa dấu cách/phẩy (YouTube coi như bọc ngoặc kép)."""
    return sum(utf8_len(t) + (2 if (" " in t or "," in t) else 0) for t in tags)


def clean_publishing(pub: dict, *, require_kids: bool = True) -> tuple[dict, list[str]]:
    """Kiểm + sửa TẤT ĐỊNH các trường đăng. Trả (giá trị sạch, việc đã sửa). Tự sửa (không đổi nghĩa): trim/gộp khoảng trắng, bỏ tag rỗng/trùng/quá dài,
    bỏ NGUYÊN tag cuối khi tổng > 500, privacy hoa/thường, playlist rỗng/trùng. KHÔNG sửa (raise POLICY): sai kiểu, privacy/categoryId sai,
    made_for_kids không phải bool thật (chuỗi "true"/số 0,1/None). Lỗi chính sách nội dung của YouTube không nằm ở đây: không bao giờ cố vượt."""
    fixes: list[str] = []
    tags = [] if pub.get("tags") is None else pub["tags"]
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise _bad("INVALID_METADATA", "tags phải là danh sách chuỗi")
    kept, seen = [], set()
    for raw in tags:
        t = " ".join(raw.split())
        if t and t != raw:
            fixes.append(f"tag {raw!r} đã trim/gộp khoảng trắng")
        if not t or t.casefold() in seen:
            fixes.append(f"bỏ tag rỗng/trùng {raw!r}")
        elif tags_cost([t]) > TAGS_MAX_CHARS:
            fixes.append(f"bỏ tag quá dài {t[:20]!r}… ({tags_cost([t])} > {TAGS_MAX_CHARS})")
        else:
            kept.append(t)
            seen.add(t.casefold())
    while kept and tags_cost(kept) > TAGS_MAX_CHARS:
        fixes.append(f"tổng tags > {TAGS_MAX_CHARS}: bỏ tag cuối {kept.pop()!r}")
    priv = "private" if pub.get("privacy") is None else pub["privacy"]
    if not isinstance(priv, str) or priv.strip().lower() not in PRIVACY_VALUES:
        raise _bad("INVALID_METADATA", f"privacy phải là {' | '.join(PRIVACY_VALUES)}, nhận {priv!r}")
    if priv != priv.strip().lower():
        fixes.append(f"privacy {priv!r} -> {priv.strip().lower()!r}")
    cat = pub.get("category")
    if cat is not None and (not isinstance(cat, str) or (cat.strip() and not (cat.strip().isascii() and cat.strip().isdigit()))):
        raise _bad("INVALID_METADATA", f"category phải là categoryId số của YouTube (vd '22') hoặc bỏ trống, nhận {cat!r}")
    pls = [] if pub.get("playlists") is None else pub["playlists"]
    if not isinstance(pls, list) or not all(isinstance(x, str) for x in pls):
        raise _bad("INVALID_METADATA", "playlists phải là danh sách id playlist (chuỗi)")
    acc = pub.get("account_id")
    if acc is not None and (not isinstance(acc, str) or not acc.strip()):
        raise _bad("INVALID_METADATA", "account_id phải là chuỗi không rỗng hoặc bỏ trống")
    kids = pub.get("made_for_kids")
    if kids is not None and not isinstance(kids, bool):
        raise _bad("MISSING_MADE_FOR_KIDS" if require_kids else "INVALID_METADATA", f"made_for_kids phải là boolean thật true/false, nhận {kids!r} (không đoán, không nhận chuỗi/số)")
    if require_kids and kids is None:
        raise _bad("MISSING_MADE_FOR_KIDS", "cần made_for_kids = true/false do chủ kênh khai báo (params.made_for_kids hoặc channel publishing.made_for_kids)")
    pl_clean = list(dict.fromkeys(x.strip() for x in pls if x.strip()))
    if pl_clean != pls:
        fixes.append("playlist rỗng/trùng đã được bỏ")
    return {"tags": kept, "privacy": priv.strip().lower(), "category": (cat or "").strip() or None, "playlists": pl_clean,
            "account_id": acc.strip() if acc else None, "made_for_kids": kids}, fixes


PUBLISH_KEYS = ("tags", "privacy", "category", "playlists", "account_id", "made_for_kids")


def pick_publishing(params: dict, channel_pub: dict, defaults: dict) -> dict:
    """Giá trị đăng theo ưu tiên params > Channel Config (`publishing`) > config `publishing.defaults`. made_for_kids KHÔNG có default (phải do chủ kênh/job khai)."""
    out = {}
    for k in PUBLISH_KEYS:
        out[k] = next((s[k] for s in (params, channel_pub, {} if k == "made_for_kids" else defaults) if s.get(k) is not None), None)
    return out
