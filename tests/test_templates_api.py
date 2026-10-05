"""Phase 10 — API giao diện cho Template/Asset (Template Studio): vòng đời qua HTTP, chọn template cho kênh, phục vụ file theo tên, và (ContentFlow thật)
xem trước / render thử / import asset."""
import io
import json
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.runner import Orchestrator
from contentfactory.orchestrator.webui import App, UiServer
from tests.support import REPO
from tests.test_automode import write_config
from tests.test_templates import HAVE_REAL, REAL_PY
from tests.test_ui import UiCase

URL_ = "https://www.youtube.com/watch?v=abcdefghijk"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class _Http(UiCase):
    def setUp(self):
        super().setUp()
        self.app = App(self.o, run_loop=False, opener=lambda p: None)
        self.srv = UiServer(self.app, 0)
        self.srv.start()
        self.addCleanup(self.srv.stop)
        self.base = self.srv.url.rstrip("/")

    def call(self, method, path, body=None, raw=None):
        h = {"Content-Type": "application/json", "X-CF-Token": self.app.token}
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, method=method, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                blob = r.read()
                return r.status, (json.loads(blob or b"{}") if "json" in r.headers.get("Content-Type", "") else blob)
        except urllib.error.HTTPError as e:
            b = e.read()
            return e.code, (json.loads(b) if b else {})


class TemplateApiTest(_Http):
    def test_lifecycle_over_http(self):
        c, d = self.call("GET", "/api/templates")
        self.assertEqual(c, 200)
        self.assertIn("youtube_default", [t["id"] for t in d["templates"]])
        self.assertEqual(d["defaults"]["tiktok_video"], "tiktok_default")
        c, d = self.call("POST", "/api/templates", {"type": "video", "id": "studio_one", "name": "Studio One"})
        self.assertEqual((c, d["template"]["status"], d["template"]["version"]), (200, "draft", 1))
        c, d = self.call("POST", "/api/templates", {"type": "video", "id": "studio_one", "name": "again"})
        self.assertEqual((c, d["error"]["code"]), (400, "TEMPLATE_ID_EXISTS"))
        c, opt = self.call("GET", "/api/templates/options")
        self.assertNotIn("studio_one", [r["id"] for r in opt["options"]["youtube_video"]])                       # draft: chưa chọn được cho kênh
        c, g = self.call("GET", "/api/templates/studio_one")
        doc = g["template"]
        doc["description"] = "đã sửa"
        c, saved = self.call("PUT", "/api/templates/studio_one/1", {"template": doc})
        self.assertEqual((c, saved["template"]["description"]), (200, "đã sửa"))
        c, p1 = self.call("POST", "/api/templates/studio_one/1/publish")
        c, p2 = self.call("POST", "/api/templates/studio_one/1/publish")                                          # bấm đúp: không lỗi, không đổi
        self.assertEqual((p1["changed"], p2["changed"], p2["status"]), (True, False, "published"))
        c, e = self.call("PUT", "/api/templates/studio_one/1", {"template": doc})
        self.assertEqual((c, e["error"]["code"]), (400, "TEMPLATE_IMMUTABLE"))
        c, opt = self.call("GET", "/api/templates/options")
        self.assertIn("studio_one", [r["id"] for r in opt["options"]["youtube_video"]])
        c, nd = self.call("POST", "/api/templates/studio_one/new-draft", {})
        self.assertEqual(nd["template"]["version"], 2)
        c, e = self.call("POST", "/api/templates/studio_one/new-draft", {})
        self.assertEqual(e["error"]["code"], "DRAFT_EXISTS")
        c, dup = self.call("POST", "/api/templates/studio_one/duplicate", {"new_id": "studio_two", "name": "Hai"})
        self.assertEqual((dup["template"]["id"], dup["template"]["status"]), ("studio_two", "draft"))
        c, a = self.call("POST", "/api/templates/studio_one/archive", {})
        self.assertEqual(a["archived"], [1])
        c, e = self.call("GET", "/api/templates/ghost")
        self.assertEqual((c, e["error"]["code"]), (404, "TEMPLATE_NOT_FOUND"))

    def test_channel_template_selection_and_resolution(self):
        c, r = self.call("PUT", "/api/channels/kenh/templates", {"key": "tiktok_video", "template_id": "tiktok_framed"})
        self.assertEqual((c, r["template"]["id"]), (200, "tiktok_framed"))
        c, res = self.call("GET", "/api/channels/kenh/templates")
        self.assertEqual((res["tiktok_video"]["effective"]["id"], res["tiktok_video"]["ok"], res["tiktok_video"]["is_default"]), ("tiktok_framed", True, False))
        self.assertTrue(res["thumbnail"]["is_default"] and res["thumbnail"]["effective"]["id"] == "thumb_default")
        c, e = self.call("PUT", "/api/channels/kenh/templates", {"key": "tiktok_video", "template_id": "thumb_gold"})
        self.assertEqual((c, e["error"]["code"]), (400, "TEMPLATE_WRONG_TYPE"))
        c, e = self.call("PUT", "/api/channels/kenh/templates", {"key": "nope", "template_id": "x"})
        self.assertEqual(c, 400)
        c, p = self.call("POST", "/api/preview", {"input": {"value": URL_}, "channel": "kenh"})
        self.assertEqual(p["templates"]["tiktok"]["id"], "tiktok_framed")
        self.assertFalse([x for x in p["problems"] if "TEMPLATE" in x["code"]])
        self.call("POST", "/api/templates", {"type": "video", "id": "wip_t", "name": "WIP"})
        raw = self.call("GET", "/api/channels/kenh")[1]["raw"]
        raw["templates"] = {"youtube_video": {"id": "wip_t"}}
        self.call("PUT", "/api/channels/kenh", {"raw": raw})                                                      # lưu tay: chỉ kiểm hình dạng
        c, p = self.call("POST", "/api/preview", {"input": {"value": URL_}, "channel": "kenh"})
        self.assertIn("INVALID_CHANNEL_TEMPLATE", [x["code"] for x in p["problems"]])                            # nhưng báo ngay trước khi bấm RUN
        self.assertFalse(p["can_run"])
        c, e = self.call("POST", "/api/runs", {"input": {"value": URL_}, "channel": "kenh", "run": "full"})
        self.assertEqual((c, e["error"]["code"]), (400, "INVALID_CHANNEL_TEMPLATE"))

    def test_extra_keys_in_saved_channel_templates_are_rejected(self):
        raw = self.call("GET", "/api/channels/kenh")[1]["raw"]
        raw["templates"] = {"thumbnail": {"id": "thumb_gold", "title_x": 5}}
        c, e = self.call("PUT", "/api/channels/kenh", {"raw": raw})
        self.assertEqual((c, e["error"]["code"]), (400, "INVALID_CHANNEL_CONFIG"))

    def test_preview_files_are_served_by_name_only(self):
        for bad in ("/api/templates/files/previews/..%2Fconfig.json", "/api/templates/files/secrets/x.png", "/api/templates/files/previews/a/b/c.png"):
            c, e = self.call("GET", bad)
            self.assertIn(c, (400, 404), bad)
        c, e = self.call("POST", "/api/templates/thumb_default/preview", {})
        self.assertEqual(c, 400)                                                                                   # fake render không có xem trước: báo rõ, không 500


@unittest.skipUnless(HAVE_REAL, "cần ContentFlow thật")
class RealTemplateApiTest(_Http):
    def orc(self):                                                                                                 # UiCase.setUp dùng self.orc(): dựng bằng ContentFlow thật + user_root tạm
        self.user_root = Path(tempfile.mkdtemp(prefix="cf-user-"))
        self.addCleanup(shutil.rmtree, self.user_root, True)
        write_config(self.root, adapters={"render": "contentflow"},
                     tools={"contentflow": {"root": str(REPO / "modules" / "ContentFlow"), "python": REAL_PY, "user_root": str(self.user_root),
                                            "base_dir": str(self.root / "cfbase")}})
        return Orchestrator(load_config(self.root))

    def test_studio_round_trip_with_real_preview_test_render_assets(self):
        c, d = self.call("POST", "/api/templates", {"type": "thumbnail", "id": "real_t", "name": "Real T"})
        self.assertEqual(c, 200, d)
        doc = d["template"]
        c, pv = self.call("POST", "/api/templates/real_t/preview", {"template": doc})
        self.assertEqual(c, 200, pv)
        c, img = self.call("GET", pv["url"])
        self.assertEqual(img[:8], PNG_MAGIC)
        c, tr = self.call("POST", "/api/templates/real_t/test-render", {"template": doc})
        self.assertEqual((c, tr["kind"]), (200, "thumbnail"))
        c, v = self.call("POST", "/api/templates/real_t/validate", {"template": doc})
        self.assertTrue(v["ok"])
        bad = json.loads(json.dumps(doc))
        bad["elements"][0]["width"] = -1
        c, v = self.call("POST", "/api/templates/real_t/validate", {"template": bad})
        self.assertFalse(v["ok"])
        self.assertIn("BAD_GEOMETRY", [x["code"] for x in v["errors"]])
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGBA", (50, 50), (255, 0, 0, 255)).save(buf, "PNG")
        c, a = self.call("PUT", "/api/assets/studio_logo?type=logo&name=Logo%20Dep.png", raw=buf.getvalue())
        self.assertEqual((c, a["asset"]["scope"], a["asset"]["type"]), (200, "user", "logo"))
        c, lst = self.call("GET", "/api/assets?type=logo")
        self.assertIn("studio_logo", [x["id"] for x in lst["assets"]])
        c, f = self.call("GET", "/api/assets/studio_logo/file")
        self.assertEqual(f[:4], PNG_MAGIC[:4])
        c, e = self.call("PUT", "/api/assets/studio_logo?type=logo&name=x.png", raw=buf.getvalue())
        self.assertEqual((c, e["error"]["code"]), (400, "ASSET_ID_EXISTS"))
        c, e = self.call("PUT", "/api/assets/fake_img?type=frame&name=x.png", raw=b"not an image")
        self.assertEqual((c, e["error"]["code"]), (400, "ASSET_BAD_FILE"))
        doc["elements"].insert(0, {"id": "lg", "type": "image", "asset_id": "studio_logo", "x": 0, "y": 0, "width": 50, "height": 50, "z": 1})
        c, sv = self.call("PUT", "/api/templates/real_t/1", {"template": doc})
        self.assertEqual(c, 200, sv)
        c, e = self.call("DELETE", "/api/assets/studio_logo")
        self.assertEqual((c, e["error"]["code"]), (400, "ASSET_IN_USE"))
        c, e = self.call("DELETE", "/api/assets/frame_thumb_gold")
        self.assertEqual((c, e["error"]["code"]), (400, "ASSET_READONLY"))


if __name__ == "__main__":
    unittest.main()
