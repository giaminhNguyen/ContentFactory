"""Stub của thư viện youtube-transcript-api CHỈ cho test: để chạy code THẬT của Subtitle_supperVip
(app.services.subtitles) mà không cần mạng. Kịch bản chọn theo video id.

    manualvi_01  có sub thủ công vi + en, và auto en
    autoonly_01  chỉ có auto-caption (en)
    onlygerman1  chỉ có sub thủ công de
    nosubs_0001  video tắt phụ đề       (ném lớp tên NoTranscriptFound)
    blocked_001  YouTube chặn IP        (ném lớp tên IpBlocked)
"""
from types import SimpleNamespace


class NoTranscriptFound(Exception):
    pass


class IpBlocked(Exception):
    pass


class _Transcript:
    def __init__(self, code, language, generated, translatable=True, lines=None):
        self.language_code, self.language = code, language
        self.is_generated, self.is_translatable = generated, translatable
        self._lines = lines or [("Xin chào các bạn", 0.0, 1.5), ("hôm nay trời đẹp", 1.5, 2.0)]

    def fetch(self):
        return SimpleNamespace(snippets=[SimpleNamespace(text=t, start=s, duration=d) for t, s, d in self._lines])

    def translate(self, language):
        return _Transcript(language, language, self.is_generated, False, self._lines)


class YouTubeTranscriptApi:
    def list(self, video_id):
        if video_id == "nosubs_0001":
            raise NoTranscriptFound("no transcript")
        if video_id == "blocked_001":
            raise IpBlocked("YouTube is blocking requests from your IP")
        if video_id == "manualvi_01":
            return [_Transcript("vi", "Vietnamese", False, lines=[("Chào các bạn thân mến", 0.0, 2.0)]),
                    _Transcript("en", "English", False), _Transcript("en", "English (auto-generated)", True)]
        if video_id == "autoonly_01":
            return [_Transcript("en", "English (auto-generated)", True)]
        if video_id == "onlygerman1":
            return [_Transcript("de", "German", False)]
        raise RuntimeError(f"stub: video lạ {video_id}")
