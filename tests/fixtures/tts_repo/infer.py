"""ToneSpeak: engine TTS giả cho test (ghi sóng vuông WAV, thời lượng tỉ lệ độ dài chữ)."""
import argparse
import struct
import sys
import wave

MAX_CHARS = 480
SAMPLE_RATE = 24000
LANGS = ["vi", "en"]
VOICES = ["alice", "bob"]
CHARS_PER_SEC = 15


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True, help="text to speak")
    ap.add_argument("--out", required=True, help="output wav path")
    ap.add_argument("--voice", default="alice", choices=VOICES)
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--lang", default="vi", choices=LANGS)
    a = ap.parse_args(argv)
    if len(a.text) > MAX_CHARS:
        print(f"text too long: {len(a.text)} > {MAX_CHARS}", file=sys.stderr)
        return 2
    n = int(SAMPLE_RATE * max(0.3, len(a.text) / CHARS_PER_SEC / a.speed))
    amp = 3000 if a.voice == "alice" else 2000
    frames = b"".join(struct.pack("<h", amp if (i // 60) % 2 else -amp) for i in range(n))
    with wave.open(a.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(frames)
    return 0


if __name__ == "__main__":
    sys.exit(main())
