"""Watermark Library qua HTTP: tạo (tác vụ nền) / tải lên / sửa / tạo lại / chọn / xóa / nghe thử; bảo mật token + đường dẫn; lưu kênh không đè active."""
import json
import urllib.error
import urllib.request
import wave

from contentfactory.orchestrator.webui import App, UiServer
from tests import test_ui
from tests.support import wait_until
from tests.test_automode import tts_profile


def wav_bytes(root, byte=1, seconds=0.4) -> bytes:
    p = root / f"w{byte}.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(bytes([byte, 0]) * int(8000 * seconds))
    return p.read_bytes()


class WatermarkHttpTest(test_ui.UiCase):
    def setUp(self):
        super().setUp()
        tts_profile("giong_a", self.root, tuned=True)
        self.app = App(self.o, run_loop=False, opener=lambda p: None)
        self.srv = UiServer(self.app, 0)
        self.srv.start()
        self.addCleanup(self.srv.stop)
        self.base = self.srv.url.rstrip("/")

    def call(self, method, path, body=None, token=True, raw=None):
        h = {"Content-Type": "application/octet-stream" if raw is not None else "application/json", **({"X-CF-Token": self.app.token} if token else {})}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, method=method, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                return r.status, (json.loads(body) if "json" in r.headers.get("Content-Type", "") else body), r.headers
        except urllib.error.HTTPError as e:
            raw_body = e.read()
            return e.code, (json.loads(raw_body) if raw_body else {}), e.headers

    def task(self, resp):
        tid = resp["task"]
        wait_until(lambda: self.call("GET", f"/api/tasks/{tid}")[1]["state"] != "running", 30, "watermark task")
        t = self.call("GET", f"/api/tasks/{tid}")[1]
        self.assertEqual(t["state"], "done", t.get("error"))
        return t["result"]

    def test_full_lifecycle_over_http(self):
        c, ov, _ = self.call("GET", "/api/channels/kenh/watermarks")
        self.assertEqual((c, ov["items"], ov["tts"]["available"], [p["name"] for p in ov["tts"]["profiles"]]), (200, [], True, ["giong_a"]))
        # tạo bằng TTS (tác vụ nền) + lỗi nhập liệu báo NGAY
        c, e, _ = self.call("POST", "/api/channels/kenh/watermarks", {"name": "Intro", "text": "   ", "tts": "auto"})
        self.assertEqual((c, e["error"]["code"]), (400, "WATERMARK_TEXT_EMPTY"))
        c, e, _ = self.call("POST", "/api/channels/kenh/watermarks", {"name": "Intro", "text": "Xin chào.", "tts": "khong_co"})
        self.assertEqual((c, e["error"]["code"]), (404, "TTS_PROFILE_NOT_FOUND"))
        c, started, _ = self.call("POST", "/api/channels/kenh/watermarks", {"name": "Intro", "text": "Bạn đang nghe truyện.", "tts": "giong_a", "activate": True, "request_id": "r1"})
        self.assertEqual(c, 200)
        res = self.task(started)
        item = res["item"]
        self.assertEqual((res["result"], item["source"], item["active"], item["tts"]["profile"]), ("created", "tts", True, "giong_a"))
        self.assertNotIn("path", json.dumps(item))                                                      # không lộ đường dẫn thật
        wid = item["id"]
        # nghe thử: chỉ mã do backend sinh, cần token
        c, body, h = self.call("GET", f"/api/channels/kenh/watermarks/{wid}/audio")
        self.assertEqual((c, h.get_content_type(), body[:4]), (200, "audio/wav", b"RIFF"))
        self.assertEqual(self.call("GET", f"/api/channels/kenh/watermarks/{wid}/audio", token=False)[0], 401)
        self.assertEqual(self.call("GET", "/api/channels/kenh/watermarks/wm_00000000/audio")[0], 404)
        self.assertEqual(self.call("GET", f"/api/channels/kenh/watermarks/{wid}/audio?revision=9")[0], 404)
        # sửa: chỉ đổi tên = đồng bộ; đổi văn bản = bản mới (tác vụ); không đổi gì ảnh hưởng âm thanh = không bản mới
        c, r, _ = self.call("PUT", f"/api/channels/kenh/watermarks/{wid}", {"name": "Intro mới"})
        self.assertEqual((c, r["item"]["name"], r["item"]["current_revision"]), (200, "Intro mới", 1))
        c, started, _ = self.call("PUT", f"/api/channels/kenh/watermarks/{wid}", {"text": "Chào mừng bạn quay lại."})
        res = self.task(started)
        self.assertEqual((res["result"], res["item"]["current_revision"], res["item"]["active_revision"]), ("revised", 2, 2))
        c, r, _ = self.call("POST", f"/api/channels/kenh/watermarks/{wid}/regenerate")
        self.assertEqual((c, r["result"]), (200, "unchanged"))                                          # cùng nội dung: không tốn lượt TTS
        det = self.call("GET", f"/api/channels/kenh/watermarks/{wid}")[1]
        self.assertEqual([(x["revision"], x["active"]) for x in det["revisions"]], [(1, False), (2, True)])
        # tải lên: tạo, thay file, chọn, xóa
        c, up, _ = self.call("PUT", "/api/channels/kenh/watermarks/upload?name=T%E1%BA%A3i%20l%C3%AAn&filename=a.wav", raw=wav_bytes(self.root, 3))
        uid = up["item"]["id"]
        self.assertEqual((c, up["item"]["source"], up["item"]["active"]), (200, "upload", False))
        c, rp, _ = self.call("PUT", f"/api/channels/kenh/watermarks/{uid}/file?filename=b.wav", raw=wav_bytes(self.root, 5))
        self.assertEqual((c, rp["item"]["current_revision"]), (200, 2))
        c, bad, _ = self.call("PUT", "/api/channels/kenh/watermarks/upload?name=X&filename=x.wav", raw=b"hong")
        self.assertEqual((c, bad["error"]["code"]), (400, "WATERMARK_AUDIO_INVALID"))
        c, act, _ = self.call("POST", f"/api/channels/kenh/watermarks/{uid}/activate", {})
        self.assertEqual((c, act["active"]), (200, True))
        self.assertEqual(self.call("GET", "/api/channels/kenh/watermarks")[1]["active"], uid)
        c, e, _ = self.call("DELETE", f"/api/channels/kenh/watermarks/{uid}")
        self.assertEqual((c, e["error"]["code"]), (400, "WATERMARK_IN_USE"))                           # đang active: không để dangling
        self.assertEqual(self.call("DELETE", f"/api/channels/kenh/watermarks/{wid}")[1]["result"], "deleted")
        c, d, _ = self.call("DELETE", f"/api/channels/kenh/watermarks/{uid}?unset=1")
        self.assertEqual((c, d["result"]), (200, "deleted"))
        self.assertEqual(self.call("GET", "/api/channels/kenh/watermarks")[1]["items"], [])
        self.assertEqual(self.call("DELETE", f"/api/channels/kenh/watermarks/{uid}")[0], 404)           # xóa lặp: WATERMARK_NOT_FOUND

    def test_writes_need_token_and_ids_cannot_escape(self):
        self.assertEqual(self.call("POST", "/api/channels/kenh/watermarks", {"name": "A", "text": "x"}, token=False)[0], 401)
        self.assertEqual(self.call("PUT", "/api/channels/kenh/watermarks/upload?name=A&filename=a.wav", raw=b"x", token=False)[0], 401)
        for bad in ("..", "../kenh", "wm_../x"):
            self.assertEqual(self.call("GET", f"/api/channels/kenh/watermarks/{bad}/audio")[0], 404)
        self.assertEqual(self.call("GET", "/api/channels/kenh/watermarks/wm_ABCDEFGH")[0], 404)       # không khớp mẫu id: không có route

    def test_saving_the_channel_form_cannot_overwrite_the_active_watermark(self):
        c, up, _ = self.call("PUT", "/api/channels/kenh/watermarks/upload?name=Intro&filename=a.wav&activate=1", raw=wav_bytes(self.root, 2))
        wid = up["item"]["id"]
        raw = self.call("GET", "/api/channels/kenh")[1]["raw"]
        self.assertEqual(raw["watermark_ref"], {"id": wid, "revision": 1})
        raw["watermark"], raw["watermark_ref"] = "khac.wav", {"id": "wm_00000000", "revision": 4}        # form cũ/độc hại cố đổi
        raw["name"] = "Kênh đổi tên"
        c, e, _ = self.call("PUT", "/api/channels/kenh", {"raw": raw})
        self.assertEqual(c, 200, e)
        after = json.loads((self.root / "channels" / "kenh" / "channel.json").read_text(encoding="utf-8"))
        self.assertEqual((after["name"], after["watermark_ref"], after["watermark"]), ("Kênh đổi tên", {"id": wid, "revision": 1}, f"watermarks/{wid}/rev_0001.wav"))
        self.assertEqual(self.call("POST", "/api/channels/kenh/watermark/deactivate", {})[1]["active"], None)
        self.assertNotIn("watermark_ref", json.loads((self.root / "channels" / "kenh" / "channel.json").read_text(encoding="utf-8")))
