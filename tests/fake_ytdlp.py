"""Giả lập dòng lệnh yt-dlp (chạy bằng subprocess thật) để test lớp YtDlp: --dump-single-json và --write-(auto-)subs."""
import json
import os
import re
import shutil
import sys
from pathlib import Path

args = sys.argv[1:]
mode = os.environ.get("FAKE_YTDLP_MODE", "ok")
if mode == "private":
    sys.stderr.write("ERROR: [youtube] abcdefghijk: Private video. Sign in if you've been granted access\n")
    sys.exit(1)
if mode == "rate":
    sys.stderr.write("ERROR: unable to download webpage: HTTP Error 429: Too Many Requests\n")
    sys.exit(1)
fixture = Path(os.environ["FAKE_FIXTURE"])
if "--dump-single-json" in args:
    track = {"vi": [{"ext": fixture.suffix[1:]}]}
    print(json.dumps({"id": "abcdefghijk", "title": "Chuyện ma ở nhà cũ", "language": "vi",
                      "subtitles": track, "automatic_captions": {"vi-orig": track, "en": track}}))
    sys.exit(0)
lang = re.sub(r"\\", "", args[args.index("--sub-langs") + 1])
template = args[args.index("-o") + 1]
out = Path(template.replace("%(id)s", "abcdefghijk").replace("%(ext)s", fixture.suffix[1:])).with_name(
    f"abcdefghijk.{lang}{fixture.suffix}")
out.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(fixture, out)
