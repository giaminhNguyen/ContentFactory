"""Dữ liệu mẫu để thử ngay khi chưa có truyện/video (D-90): `cf samples` hoặc nút "Tạo dữ liệu mẫu" trong giao diện.

Sinh trong `<root>/samples/`: truyện (.txt, không đánh số chương), phụ đề (.srt), audio (.wav, tiếng bíp có nhịp nghỉ để thử cắt part), hai thư mục video nền
(ngang 16:9 cho YouTube, dọc 9:16 cho TikTok; clip tổng hợp bằng ffmpeg). Thumbnail/video dùng template builtin của ContentFlow (không cần file mẫu). Chỉ ĐĂNG KÝ (không ghi đè) pool vào
`config/config.local.json` khi người dùng chưa cấu hình. Chạy lại an toàn (bỏ qua file đã có, trừ khi force). Chỉ dùng stdlib + ffmpeg có sẵn.
"""
from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

from ..fsutil import atomic_write_json, atomic_write_text
from .config import Config

STORY = """Ngôi nhà cuối ngõ

Tôi chuyển về khu phố cũ vào một chiều cuối thu, khi những chiếc lá bàng đỏ au phủ kín mặt đường. Căn nhà tôi thuê nằm ở cuối ngõ, tường rêu loang lổ, cánh cổng sắt kêu rít mỗi lần đẩy. Bà chủ nhà dặn đi dặn lại một điều: tối đến, nếu nghe ai gọi tên mình ngoài hiên, tuyệt đối đừng mở cửa.

Tuần đầu tiên trôi qua yên ả. Ban ngày tôi đi làm, tối về nấu cơm, đọc sách, rồi ngủ sớm. Chỉ có điều lạ là chiếc đồng hồ treo tường ở phòng khách luôn dừng đúng ba giờ mười lăm, dù tôi đã thay pin hai lần. Tôi nghĩ đó chỉ là cái đồng hồ cũ hỏng máy.

Đêm thứ tám, tôi tỉnh dậy vì tiếng gõ cửa. Ba tiếng chậm rãi, đều đặn, rồi im lặng. Tôi nhìn đồng hồ điện thoại: ba giờ mười lăm. Tim tôi đập mạnh khi một giọng nói rất khẽ vang lên ngoài hiên, gọi đúng tên tôi, giọng của một người phụ nữ nghe vừa xa lạ vừa quen thuộc.

Tôi nhớ lời bà chủ nhà, nên nằm im, kéo chăn trùm kín đầu. Tiếng gọi lặp lại ba lần rồi tắt hẳn. Sáng hôm sau, trên bậc thềm trước cửa có một chiếc khăn len màu xanh, còn ẩm sương, được gấp gọn gàng. Tôi chưa từng thấy chiếc khăn ấy bao giờ.

Tôi hỏi thăm hàng xóm, và một cụ già bán nước đầu ngõ kể cho tôi nghe. Mười năm trước, cô gái từng sống trong căn nhà ấy đi làm về muộn mỗi đêm, và luôn bị khóa ngoài cửa vì người nhà ngủ say. Một đêm mùa đông cô ngồi đợi trước hiên đến sáng, và không bao giờ đứng dậy nữa. Chiếc khăn len xanh là thứ duy nhất cô mang theo.

Đêm đó tôi không ngủ. Đúng ba giờ mười lăm, tiếng gõ cửa lại vang lên. Lần này tôi hít một hơi thật sâu, bước ra phòng khách, và mở cánh cửa. Ngoài hiên chỉ có gió lạnh và ánh trăng. Tôi đặt chiếc khăn len lên bậc thềm, khẽ nói rằng bây giờ đã có người mở cửa cho cô rồi.

Từ hôm ấy, chiếc đồng hồ trên tường chạy trở lại, và đêm nào cũng yên tĩnh. Mỗi khi trời trở rét, tôi vẫn để lại một ngọn đèn nhỏ ngoài hiên, phòng khi có ai đó đi muộn về nhà.
"""

CUES = [
    "Chào các bạn, hôm nay mình kể một câu chuyện về ngôi nhà cuối ngõ.", "Câu chuyện bắt đầu vào một chiều cuối thu, khi lá bàng đỏ phủ kín mặt đường.",
    "Căn nhà nằm tận cuối ngõ, tường rêu loang lổ và cánh cổng sắt kêu rít.", "Bà chủ nhà dặn rằng nếu nghe ai gọi tên mình ngoài hiên thì đừng mở cửa.",
    "Tuần đầu tiên mọi thứ yên ả, chỉ có chiếc đồng hồ luôn dừng ở ba giờ mười lăm.", "Đêm thứ tám, có ba tiếng gõ cửa chậm rãi vang lên.",
    "Rồi một giọng nói rất khẽ gọi đúng tên người thuê nhà.", "Người ấy nằm im, kéo chăn trùm kín đầu cho đến khi tiếng gọi tắt hẳn.",
    "Sáng hôm sau, trước cửa có một chiếc khăn len màu xanh còn ẩm sương.", "Một cụ già đầu ngõ kể lại chuyện cô gái sống ở đây mười năm trước.",
    "Cô đi làm về muộn, bị khóa ngoài cửa và ngồi đợi trước hiên suốt đêm đông.", "Chiếc khăn len xanh là thứ duy nhất cô mang theo.",
    "Đêm đó, đúng ba giờ mười lăm, tiếng gõ cửa lại vang lên.", "Lần này người thuê nhà đã mở cửa và nói rằng có người đang đợi cô về.",
    "Từ hôm ấy chiếc đồng hồ chạy lại và đêm nào cũng yên tĩnh.", "Cảm ơn các bạn đã lắng nghe, hẹn gặp lại ở câu chuyện sau.",
]

VIDEO_SOURCES = ("testsrc2", "smptebars", "gradients")


def _ts(sec: float) -> str:
    ms = int(round(sec * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def make_srt(path: Path) -> None:
    out, t = [], 0.0
    for i, c in enumerate(CUES, 1):
        dur = max(2.5, len(c) / 14)
        out.append(f"{i}\n{_ts(t)} --> {_ts(t + dur)}\n{c}\n")
        t += dur + 0.3
    atomic_write_text(path, "\n".join(out))


def make_wav(path: Path, seconds: int = 45, rate: int = 16000) -> None:
    """Âm bíp có nhịp nghỉ (như câu nói + quãng nghỉ) để thử Audio Pipeline và cắt part; KHÔNG phải giọng đọc."""
    frames = bytearray()
    t = 0.0
    k = 0
    while t < seconds:
        burst = 2.2 + (k % 3) * 0.5
        n = int(burst * rate)
        f = 220 + 40 * (k % 4)
        for i in range(n):
            env = min(1.0, i / (0.03 * rate), (n - i) / (0.05 * rate))
            frames += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * f * i / rate)))
        gap = int((0.7 + (k % 2) * 0.4) * rate)
        frames += b"\x00\x00" * gap
        t += burst + gap / rate
        k += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    tmp.replace(path)


def _ffmpeg(cfg: Config) -> str | None:
    f = (cfg.data.get("tools") or {}).get("ffmpeg") or "ffmpeg"
    return shutil.which(f) or (f if Path(f).exists() else None)


def make_video(ffmpeg: str, dst: Path, source: str, size: str, seconds: int = 8) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name("." + dst.name + ".part.mp4")
    cmd = [ffmpeg, "-hide_banner", "-v", "error", "-y", "-f", "lavfi", "-i", f"{source}=size={size}:rate=30:duration={seconds}", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "32", "-pix_fmt", "yuv420p", "-an", "-movflags", "+faststart", str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dst)
    return True


def _local(cfg: Config) -> tuple[Path, dict]:
    f = cfg.root / "config" / "config.local.json"
    try:
        return f, (json.loads(f.read_text(encoding="utf-8-sig")) if f.is_file() else {})
    except (OSError, ValueError):
        return f, {}


def make_samples(cfg: Config, dest: Path | None = None, register: bool = True, force: bool = False) -> dict:
    dest = Path(dest or cfg.root / "samples").resolve()
    dest.mkdir(parents=True, exist_ok=True)
    res: dict = {"dir": str(dest), "created": [], "skipped": [], "warnings": []}

    def put(name: str, writer) -> Path:
        p = dest / name
        if p.exists() and not force:
            res["skipped"].append(name)
        else:
            writer(p)
            res["created"].append(name)
        return p

    res["story"] = str(put("truyen_mau.txt", lambda p: atomic_write_text(p, STORY)))
    res["subtitle"] = str(put("phu_de_mau.srt", make_srt))
    res["audio"] = str(put("audio_mau.wav", make_wav))
    ff = _ffmpeg(cfg)
    res["video_dirs"] = {"landscape": str(dest / "video_ngang"), "portrait": str(dest / "video_doc")}
    if not ff:
        res["warnings"].append("Chưa có ffmpeg nên chưa tạo được video mẫu. Chạy setup để cài ffmpeg, rồi tạo lại dữ liệu mẫu.")
    else:
        for d, size in (("video_ngang", "1280x720"), ("video_doc", "720x1280")):
            for k, src in enumerate(VIDEO_SOURCES, 1):
                name = f"{d}/clip{k}.mp4"
                if (dest / name).exists() and not force:
                    res["skipped"].append(name)
                elif make_video(ff, dest / name, src, size):
                    res["created"].append(name)
                else:
                    res["warnings"].append(f"Không tạo được {name} (nguồn ffmpeg '{src}' không có trên máy này).")
    res["registered"] = []
    if register:
        f, local = _local(cfg)
        render = local.setdefault("render", {})
        pools = render.setdefault("pools", {})
        have = cfg.data.get("render", {}).get("pools") or {}
        if ff and (dest / "video_ngang").is_dir() and not have:
            pools["gameplay"] = {"raw_dir": str(dest / "video_ngang"), "orientation": "landscape"}
            pools["gameplay_vertical"] = {"raw_dir": str(dest / "video_doc"), "orientation": "portrait"}
            cfg.data.setdefault("render", {}).setdefault("pools", {}).update(pools)
            res["registered"] += ["pool gameplay (ngang)", "pool gameplay_vertical (dọc)"]
        if res["registered"]:
            atomic_write_json(f, local)
    res["next"] = ("Thử: dán đường dẫn truyện mẫu vào ô Đầu vào (chạy 'Chỉ đọc truyện'/'Đọc + dựng video'), hoặc audio mẫu để dựng video. "
                   "Muốn thử từ link YouTube thật thì cần Story/TTS thật (hiện có thể đang dùng bản giả).")
    return res
