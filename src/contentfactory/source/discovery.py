"""Khám phá nguồn YouTube: nhận dạng URL (video / kênh / playlist), liệt kê video của kênh/playlist, chọn video theo cách người dùng muốn (D-101).

Chỉ lấy METADATA (yt-dlp `--flat-playlist`): không tải media, không tạo job. Chọn video xong mới tạo Channel Run (xem orchestrator/batches.py).

Nhận dạng (không cần mạng): video (`watch?v=`, `youtu.be`, `/shorts/`, `/embed/`, `/live/`), playlist (`/playlist?list=`), kênh (`/@handle`, `/channel/UC…`,
`/c/…`, `/user/…`, kể cả có đuôi `/videos|/shorts|/streams|/playlists` và chuỗi trần `@handle`). Link không phải YouTube bị từ chối. URL chuẩn (canonical) do
backend dựng lại từ id đã kiểm, KHÔNG phản chiếu chuỗi người dùng nhập — frontend chỉ mở link do backend trả.

Chọn video (sau khi lọc): newest N (mặc định 10) | oldest N | range A..B (vị trí trong danh sách đã lọc) | khoảng ngày | thủ công. Bộ lọc: bỏ video đã xử lý (mặc định BẬT),
bỏ livestream (mặc định BẬT: không quét tab Live), bỏ video sắp công chiếu/premiere (mặc định BẬT), gồm Shorts (cấu hình). Quét có TRẦN (`max_scan`): kênh quá lớn thì cắt và báo
`truncated` (oldest/khoảng ngày khi đó chỉ chính xác trong phần đã quét).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import parse_qs, urlparse

from ..contracts import ErrorClass, StageError

_VID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHAN_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_LIST_ID = re.compile(r"^[A-Za-z0-9_-]{10,64}$")
_HANDLE = re.compile(r"^@[\w.\-]{2,60}$", re.U)
_NAME = re.compile(r"^[\w.\-%]{1,80}$", re.U)
_HOSTS = {"youtube.com", "music.youtube.com", "youtube-nocookie.com", "youtu.be"}
_TABS = {"videos", "shorts", "streams", "live", "playlists", "featured", "about", "community", "releases", "podcasts", "search"}
SELECTION_MODES = ("newest", "oldest", "range", "dates", "manual")
DEFAULT_N = 10
YT = "https://www.youtube.com"


def video_url(video_id: str) -> str:
    return f"{YT}/watch?v={video_id}"


def channel_url(*, channel_id: str | None = None, handle: str | None = None, path: str | None = None) -> str | None:
    """URL kênh chuẩn từ định danh đã kiểm (id UC… > @handle > đường dẫn /c|/user)."""
    if channel_id and _CHAN_ID.match(channel_id):
        return f"{YT}/channel/{channel_id}"
    if handle and _HANDLE.match(handle):
        return f"{YT}/{handle}"
    if path and re.fullmatch(r"/(c|user)/[\w.\-%]{1,80}", path, re.U):
        return f"{YT}{path}"
    return None


def playlist_url(list_id: str) -> str:
    return f"{YT}/playlist?list={list_id}"


def classify(value: str) -> dict:
    """{"provider": "youtube", "kind": video|channel|playlist, "id", "canonical_url", "tab"?} hoặc raise POLICY (NOT_YOUTUBE_URL / UNSUPPORTED_YOUTUBE_URL)."""
    v = (value or "").strip().strip('"')
    if _HANDLE.match(v):
        v = f"{YT}/{v}"
    elif not re.match(r"^https?://", v, re.I):
        raise StageError(ErrorClass.POLICY, "NOT_YOUTUBE_URL", "Chỉ hỗ trợ link YouTube (video, kênh, playlist).", {"hint": "Dán link youtube.com hoặc @tên-kênh."})
    u = urlparse(v)
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host not in _HOSTS:
        raise StageError(ErrorClass.POLICY, "NOT_YOUTUBE_URL", "Chỉ hỗ trợ link YouTube (youtube.com hoặc youtu.be).", {"hint": "Dán link youtube.com hoặc youtu.be."})
    segs = [s for s in u.path.split("/") if s]
    q = parse_qs(u.query)
    if host == "youtu.be":
        vid = segs[0] if segs else ""
        if _VID.match(vid):
            return _video(vid)
    elif u.path == "/watch":
        vid = q.get("v", [""])[0]
        if _VID.match(vid):
            return _video(vid)
    elif len(segs) >= 2 and segs[0] in ("shorts", "embed", "live", "v") and _VID.match(segs[1]):
        return _video(segs[1], short=segs[0] == "shorts")
    elif u.path == "/playlist":
        lid = q.get("list", [""])[0]
        if _LIST_ID.match(lid):
            return {"provider": "youtube", "kind": "playlist", "id": lid, "canonical_url": playlist_url(lid)}
    elif segs:
        tab = segs[-1] if segs[-1] in _TABS and len(segs) > 1 else None
        base = segs[:-1] if tab else segs
        if len(base) == 1 and _HANDLE.match(base[0]):
            return _channel(f"{YT}/{base[0]}", base[0], tab)
        if len(base) == 2 and base[0] == "channel" and _CHAN_ID.match(base[1]):
            return _channel(f"{YT}/channel/{base[1]}", base[1], tab)
        if len(base) == 2 and base[0] in ("c", "user") and _NAME.match(base[1]):
            return _channel(f"{YT}/{base[0]}/{base[1]}", base[1], tab)
    raise StageError(ErrorClass.POLICY, "UNSUPPORTED_YOUTUBE_URL", "Không nhận ra link YouTube này là video, kênh hay playlist.",
                     {"hint": "Dùng link video (watch?v=…), kênh (@tên hoặc /channel/UC…) hoặc playlist (/playlist?list=…)."})


def _video(vid: str, short: bool = False) -> dict:
    return {"provider": "youtube", "kind": "video", "id": vid, "canonical_url": video_url(vid), "short": short}


def _channel(canonical: str, ident: str, tab: str | None) -> dict:
    return {"provider": "youtube", "kind": "channel", "id": ident, "canonical_url": canonical, "tab": tab}


# ---------------------------------------------------------------------------------------------------- liệt kê + chọn
def _date(e: dict) -> str | None:
    """ISO ngày (YYYY-MM-DD) từ `upload_date` (YYYYMMDD) hoặc `timestamp`; None nếu thiếu."""
    d = str(e.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", d):
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    ts = e.get("timestamp") or e.get("release_timestamp")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    return None


def _norm(e: dict, position: int, tab: str) -> dict | None:
    vid = str(e.get("id") or "")
    if not _VID.match(vid):
        return None                                                           # mục không phải video (tab, playlist lồng, rác)
    status = e.get("live_status")
    avail = e.get("availability")
    return {"video_id": vid, "url": video_url(vid), "title": (e.get("title") or "").strip() or None, "duration": e.get("duration"),
            "published": _date(e), "live_status": status, "availability": avail, "short": tab == "shorts" or "/shorts/" in str(e.get("url") or ""),
            "live": tab in ("streams", "live") or status in ("is_live", "was_live", "post_live"), "upcoming": status == "is_upcoming",
            "unavailable": avail in ("private", "premium_only", "subscriber_only", "needs_auth") or (e.get("title") or "").lower() in ("[private video]", "[deleted video]"),
            "position": position}


class Discovery:
    """Liệt kê + chọn. `lister(url, end) -> dict` (yt-dlp flat) và `detail(url) -> dict` được tiêm vào nên test không cần mạng."""

    def __init__(self, lister: Callable[[str, int | None], dict], detail: Callable[[str], dict] | None = None, max_scan: int = 300,
                 confirm_above: int = 100, hard_max: int = 500) -> None:
        self.lister, self.detail = lister, detail
        self.max_scan, self.confirm_above, self.hard_max = int(max_scan), int(confirm_above), int(hard_max)

    # -- inspect -----------------------------------------------------------------------------------
    def inspect(self, value: str, resolve: bool = True) -> dict:
        info = classify(value)
        if info["kind"] == "video" or not resolve:
            return info
        data = self.lister(self._tab_url(info, "videos" if info["kind"] == "channel" else None), 1)
        cid = data.get("channel_id") or (data.get("uploader_id") if _CHAN_ID.match(str(data.get("uploader_id") or "")) else None)
        handle = next((x for x in (data.get("uploader_id"), data.get("channel")) if isinstance(x, str) and x.startswith("@")), None)
        title = (data.get("channel") if info["kind"] == "channel" else data.get("title")) or data.get("title")
        ch_url = channel_url(channel_id=cid, handle=handle) or (info["canonical_url"] if info["kind"] == "channel" else None)
        out = {**info, "title": (title or "").strip() or None, "channel_id": cid or (info["id"] if info["kind"] == "channel" and _CHAN_ID.match(info["id"]) else None),
               "channel_title": (data.get("channel") or data.get("uploader") or "").strip() or None, "channel_url": ch_url}
        if info["kind"] == "channel" and out["channel_id"]:
            out["canonical_url"] = channel_url(channel_id=out["channel_id"])         # định danh bền: đổi tên/handle vẫn cùng channel id
        return out

    @staticmethod
    def _tab_url(info: dict, tab: str | None) -> str:
        return f"{info['canonical_url']}/{tab}" if tab and info["kind"] == "channel" else info["canonical_url"]

    # -- liệt kê -----------------------------------------------------------------------------------
    def list_entries(self, info: dict, filters: dict) -> tuple[list[dict], dict]:
        """(entries mới nhất trước, meta{title, channel_id, channel_title, channel_url, truncated, scanned, warnings})."""
        tabs = [None] if info["kind"] == "playlist" else ["videos"] + (["shorts"] if filters.get("include_shorts") else []) + ([] if filters.get("skip_live", True) else ["streams"])
        entries: list[dict] = []
        meta: dict = {"truncated": False, "warnings": []}
        for tab in tabs:
            data = self.lister(self._tab_url(info, tab), self.max_scan + 1)
            meta.setdefault("title", data.get("title"))
            meta.setdefault("channel_title", data.get("channel") or data.get("uploader"))
            meta.setdefault("channel_id", data.get("channel_id") if _CHAN_ID.match(str(data.get("channel_id") or "")) else None)
            raw = data.get("entries") or []
            if len(raw) > self.max_scan:
                meta["truncated"] = True
                raw = raw[:self.max_scan]
            for pos, e in enumerate(raw, 1):
                n = _norm(e, pos, tab or "")
                if n:
                    entries.append(n)
        seen, uniq = set(), []
        for e in entries:                                                         # playlist/URL lạ có thể lặp id: giữ lần đầu
            if e["video_id"] not in seen:
                seen.add(e["video_id"])
                uniq.append(e)
        if len(tabs) > 1 and all(e["published"] for e in uniq):
            uniq.sort(key=lambda e: e["published"], reverse=True)                  # gộp Videos + Shorts theo ngày
        for i, e in enumerate(uniq, 1):
            e["position"] = i
        meta["scanned"] = len(uniq)
        meta["channel_url"] = channel_url(channel_id=meta.get("channel_id")) or (info["canonical_url"] if info["kind"] == "channel" else None)
        if meta["truncated"]:
            meta["warnings"].append(f"Chỉ quét {self.max_scan} video mới nhất; 'oldest' và khoảng ngày chỉ chính xác trong phần đã quét.")
        return uniq, meta

    # -- chọn --------------------------------------------------------------------------------------
    def discover(self, value: str, selection: dict | None = None, filters: dict | None = None,
                 processed: Callable[[str], str | None] | None = None) -> dict:
        """Danh sách ứng viên + lựa chọn mặc định. KHÔNG tạo job. `processed(video_id) -> job_id|None` cho bộ lọc 'đã xử lý'."""
        info = classify(value)
        if info["kind"] == "video":
            raise StageError(ErrorClass.POLICY, "NOT_A_COLLECTION", "Đây là link một video, không phải kênh/playlist.", {"hint": "Dán link kênh (@tên) hoặc playlist."})
        f = {"skip_processed": True, "skip_live": True, "skip_upcoming": True, "include_shorts": False, **(filters or {})}
        sel = self._selection(selection)
        entries, meta = self.list_entries(info, f)
        if sel["mode"] == "dates" and any(not e["published"] for e in entries):
            self._fill_dates(entries, meta)
        for e in entries:
            e["skip_reason"], e["processed_job"] = self._skip(e, f, processed)
        passing = [e for e in entries if not e["skip_reason"]]
        picked = self._pick(passing, sel, meta)
        chosen = {e["video_id"] for e in picked}
        for e in entries:
            e["selected"] = e["video_id"] in chosen
        skipped = {}
        for e in entries:
            if e["skip_reason"]:
                skipped[e["skip_reason"]] = skipped.get(e["skip_reason"], 0) + 1
        n = len(chosen)
        return {"source": {**info, "title": meta.get("title"), "channel_id": meta.get("channel_id"), "channel_title": meta.get("channel_title"),
                           "channel_url": meta.get("channel_url")},
                "selection": sel, "filters": f, "entries": entries, "selected": n, "skipped": skipped, "total": len(entries),
                "truncated": meta["truncated"], "warnings": meta["warnings"], "requires_confirmation": n > self.confirm_above,
                "limits": {"confirm_above": self.confirm_above, "hard_max": self.hard_max, "max_scan": self.max_scan}}

    @staticmethod
    def _selection(raw: dict | None) -> dict:
        raw = raw or {}
        mode = raw.get("mode") or "newest"
        if mode not in SELECTION_MODES:
            raise StageError(ErrorClass.POLICY, "INVALID_SELECTION", f"selection.mode không hợp lệ: {mode!r}; hợp lệ: {list(SELECTION_MODES)}")
        out: dict = {"mode": mode}
        try:
            if mode in ("newest", "oldest"):
                out["n"] = int(DEFAULT_N if raw.get("n") is None else raw["n"])
                if out["n"] < 1:
                    raise ValueError
            elif mode == "range":
                out["from"], out["to"] = int(raw["from"]), int(raw["to"])
                if not 1 <= out["from"] <= out["to"]:
                    raise ValueError
            elif mode == "dates":
                out["date_from"], out["date_to"] = raw.get("date_from") or None, raw.get("date_to") or None
                for k in ("date_from", "date_to"):
                    if out[k] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(out[k])):
                        raise ValueError
                if not (out["date_from"] or out["date_to"]):
                    raise ValueError
            else:
                out["ids"] = [str(x) for x in raw.get("ids") or []]
                if not out["ids"] or any(not _VID.match(x) for x in out["ids"]):
                    raise ValueError
        except (KeyError, ValueError, TypeError):
            raise StageError(ErrorClass.POLICY, "INVALID_SELECTION", f"selection không hợp lệ cho chế độ {mode!r}.",
                             {"hint": "newest/oldest cần n ≥ 1; range cần from ≤ to; dates cần YYYY-MM-DD; manual cần danh sách id."}) from None
        return out

    @staticmethod
    def _skip(e: dict, f: dict, processed) -> tuple[str | None, str | None]:
        if e["unavailable"]:
            return "unavailable", None
        if e["live"] and f["skip_live"]:
            return "livestream", None
        if e["upcoming"] and f["skip_upcoming"]:
            return "upcoming", None
        if e["short"] and not f["include_shorts"]:
            return "short", None
        job = processed(e["video_id"]) if (processed and f["skip_processed"]) else None
        return ("processed", job) if job else (None, None)

    def _pick(self, passing: list[dict], sel: dict, meta: dict) -> list[dict]:
        m = sel["mode"]
        if m == "newest":
            return passing[:sel["n"]]
        if m == "oldest":
            return list(reversed(passing))[:sel["n"]]
        if m == "range":
            return passing[sel["from"] - 1:sel["to"]]
        if m == "dates":
            lo, hi = sel.get("date_from"), sel.get("date_to")
            return [e for e in passing if e["published"] and (not lo or e["published"] >= lo) and (not hi or e["published"] <= hi)]
        want = set(sel["ids"])
        return [e for e in passing if e["video_id"] in want]

    def _fill_dates(self, entries: list[dict], meta: dict, cap: int = 60) -> None:
        """Entry flat thiếu ngày đăng: lấy chi tiết (tốn một lần gọi/video) — chỉ khi lọc theo ngày, có trần."""
        if self.detail is None:
            meta["warnings"].append("Không có ngày đăng trong danh sách nhanh; không lọc theo ngày được.")
            return
        todo = [e for e in entries if not e["published"]][:cap]
        for e in todo:
            try:
                e["published"] = _date(self.detail(e["url"]))
            except StageError:
                continue
        if sum(1 for e in entries if not e["published"]) > 0:
            meta["warnings"].append(f"Một số video chưa lấy được ngày đăng (tối đa {cap} video được tra chi tiết); chúng bị bỏ qua khi lọc theo ngày.")
