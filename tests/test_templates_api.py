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
    def test_create_edit_and_use_over_http_without_publish(self):
        c, d = self.call("GET", "/api/templates")
        self.assertEqual(c, 200)
        self.assertIn("youtube_default", [t["id"] for t in d["templates"]])
        self.assertEqual(d["defaults"]["tiktok_video"], "tiktok_default")
        c, d = self.call("POST", "/api/templates", {"type": "video", "id": "studio_one", "name": "Studio One"})
        self.assertEqual((c, d["template"]["status"], d["template"]["version"]), (200, "published", 1))                 # dùng được ngay
        c, d = self.call("POST", "/api/templates", {"type": "video", "id": "studio_one", "name": "again"})
        self.assertEqual((c, d["error"]["code"]), (400, "TEMPLATE_ID_EXISTS"))
        c, opt = self.call("GET", "/api/templates/options")
        self.assertIn("studio_one", [r["id"] for r in opt["options"]["youtube_video"]])                                  # chọn được cho kênh ngay
        c, g = self.call("GET", "/api/templates/studio_one")
        doc = g["template"]
        for note in ("đã sửa", "sửa lần nữa"):                                                                          # sửa tại chỗ nhiều lần, không bị khoá
            doc["description"] = note
            c, saved = self.call("PUT", "/api/templates/studio_one/1", {"template": doc})
            self.assertEqual((c, saved["template"]["description"], saved["template"]["version"]), (200, note, 1))
        c, e = self.call("PUT", "/api/templates/thumb_default/1", {"template": self.call("GET", "/api/templates/thumb_default")[1]["template"]})
        self.assertEqual((c, e["error"]["code"]), (400, "TEMPLATE_READONLY"))                                            # có sẵn: nhân bản để sửa
        c, dup = self.call("POST", "/api/templates/studio_one/duplicate", {"new_id": "studio_two", "name": "Hai"})
        self.assertEqual((dup["template"]["id"], dup["template"]["status"]), ("studio_two", "published"))
        for gone in ("publish", "archive", "new-draft", "restore"):                                                      # vòng đời cũ đã bỏ
            self.assertEqual(self.call("POST", f"/api/templates/studio_one/{gone}", {})[0], 404, gone)
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
        raw = self.call("GET", "/api/channels/kenh")[1]["raw"]
        raw["templates"] = {"youtube_video": {"id": "ghost_t"}}
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


class TemplateLifecycleUxTest(_Http):
    """Xoá template — backend quyết định hành động hợp lệ; lỗi dễ hiểu; không phá khả năng tái lập của job cũ (job giữ snapshot)."""

    def row(self, tid):
        return next(t for t in self.call("GET", "/api/templates")[1]["templates"] if t["id"] == tid)

    def point_channel_at(self, tid, key="youtube_video"):
        raw = self.call("GET", "/api/channels/kenh")[1]["raw"]
        raw["templates"] = {key: {"id": tid}}
        self.assertEqual(self.call("PUT", "/api/channels/kenh", {"raw": raw})[0], 200)

    def clear_channel(self):
        raw = self.call("GET", "/api/channels/kenh")[1]["raw"]
        raw.pop("templates", None)
        self.call("PUT", "/api/channels/kenh", {"raw": raw})

    def test_builtin_is_read_only_duplicate_only(self):
        a = self.row("thumb_default")["actions"]
        self.assertEqual((a["open"], a["duplicate"], a["delete"]), (True, True, None))
        self.assertEqual(sorted(a), ["delete", "duplicate", "open"])                                         # không còn publish/lưu trữ/nháp
        c, e = self.call("DELETE", "/api/templates/thumb_default")
        self.assertEqual((c, e["error"]["code"]), (400, "TEMPLATE_READONLY"))
        self.assertIn("chỉ đọc", e["error"]["message"])
        self.assertIn("Nhân bản", e["error"]["hint"])

    def test_a_whole_template_can_be_deleted_and_double_delete_is_safe(self):
        self.call("POST", "/api/templates", {"type": "video", "id": "del_pub", "name": "Pub"})
        self.assertEqual((self.row("del_pub")["actions"]["delete"]["enabled"], self.row("del_pub")["actions"]["delete"]["versions"]), (True, 1))
        c, r = self.call("DELETE", "/api/templates/del_pub")
        self.assertEqual((c, r["deleted"]), (200, "del_pub"))
        self.assertNotIn("del_pub", [t["id"] for t in self.call("GET", "/api/templates")[1]["templates"]])
        self.assertEqual(self.call("DELETE", "/api/templates/del_pub")[1].get("already_deleted"), True)        # bấm đúp an toàn

    def test_delete_template_is_refused_for_builtin_and_for_a_template_a_channel_uses(self):
        self.assertIsNone(self.row("thumb_default")["actions"]["delete"])
        c, e = self.call("DELETE", "/api/templates/thumb_default")
        self.assertEqual((c, e["error"]["code"]), (400, "TEMPLATE_READONLY"))
        self.call("POST", "/api/templates", {"type": "video", "id": "del_used", "name": "Used"})
        self.call("POST", "/api/templates/del_used/1/publish")
        self.point_channel_at("del_used")
        a = self.row("del_used")["actions"]["delete"]
        self.assertFalse(a["enabled"])
        self.assertIn("YouTube", a["blocked"])
        c, e = self.call("DELETE", "/api/templates/del_used")
        self.assertEqual((c, e["error"]["code"]), (400, "TEMPLATE_IN_USE"))
        self.assertEqual(self.row("del_used")["latest_published"], 1)
        self.clear_channel()
        self.assertEqual(self.call("DELETE", "/api/templates/del_used")[0], 200)

    def test_job_snapshot_survives_deleting_the_template(self):
        self.call("POST", "/api/templates", {"type": "video", "id": "snap_del", "name": "Snap"})
        snap = self.o.adapters["render"].templates.resolve(id="snap_del")
        self.call("DELETE", "/api/templates/snap_del")
        self.assertEqual((snap["id"], snap["version"]), ("snap_del", 1))                                      # job đã chốt snapshot nên không cần template nữa


class TemplatePreviewSamplesTest(_Http):
    """Phase 7: nguồn mẫu do backend mô tả; `sample` từ client chỉ là {id,image,channel} (không nhận đường dẫn); render thử tách khỏi xem trước và nói rõ khi bị tắt."""

    def capture(self):
        calls = []
        d = Path(tempfile.mkdtemp(prefix="cf-pv-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "previews").mkdir()
        (d / "previews" / "x.png").write_bytes(PNG_MAGIC)

        def fake(**kw):
            calls.append(kw)
            return {"path": str(d / "previews" / "x.png"), "canvas": [10, 10], "warnings": []}
        api = self.app.templates.ops.api
        api.preview = fake
        api.test_render = fake
        return calls

    def test_sources_describe_samples_images_channels_and_test_render(self):
        c, s = self.call("GET", "/api/templates/preview-sources?type=thumbnail")
        self.assertEqual(c, 200)
        self.assertEqual([x["id"] for x in s["samples"]], ["s1", "s2", "s3"])
        self.assertTrue(all(x["title"] and x["sequence"] for x in s["samples"]))                            # dữ liệu thực tế, không phải "TITLE"
        self.assertEqual(s["images"][0]["id"], "builtin")
        self.assertIn("kenh", [x["id"] for x in s["channels"]])
        self.assertTrue(s["test_render"]["enabled"])
        self.assertIsNone(s["note"])
        c, v = self.call("GET", "/api/templates/preview-sources?type=video")
        self.assertEqual(v["images"], [{"id": "builtin", "label": "Ảnh mẫu có sẵn"}])                      # video: ảnh nền không áp dụng
        self.assertIn("SOURCE VIDEO", v["note"])
        self.assertEqual(self.call("GET", "/api/templates/preview-sources?type=nope")[0], 400)

    def test_client_sample_is_a_descriptor_never_a_path(self):
        calls = self.capture()
        self.call("POST", "/api/templates", {"type": "thumbnail", "id": "thumb_x", "name": "Thumb X"})
        c, r = self.call("POST", "/api/templates/thumb_x/preview", {"sample": {"id": "s2", "channel": "kenh"}})
        self.assertEqual(c, 200, r)
        self.assertEqual(calls[-1]["sample"]["title"], "Đêm mưa đó, anh ấy đã không quay lại")
        self.assertNotIn("image", calls[-1]["sample"])
        self.assertNotEqual(calls[-1]["sample"]["channel"], "Truyện Đêm Khuya")                          # lấy tên kênh thật
        for bad in ("C:/Windows/win.ini", "../../etc/passwd", "frame:../x:0", "frame:p:9", "frame:nope:0"):
            c, e = self.call("POST", "/api/templates/thumb_x/preview", {"sample": {"image": bad}})
            self.assertEqual(c, 400, bad)
            self.assertIn(e["error"]["code"], ("BAD_SAMPLE", "NO_SAMPLE_MEDIA"), bad)
        self.assertEqual(len(calls), 1)                                                                     # không lần nào chạm tới renderer
        c, e = self.call("POST", "/api/templates/thumb_x/preview", {"sample": {"channel": "khong_co"}})
        self.assertEqual((c, e["error"]["code"]), (404, "CHANNEL_NOT_FOUND"))
        c, r = self.call("POST", "/api/templates/thumb_x/preview", {"sample": {"id": "khong-co-mau"}})      # id lạ: dùng mẫu đầu, không lỗi
        self.assertEqual((c, calls[-1]["sample"]["title"]), (200, "Cô gái trở về năm 1998 và phát hiện bí mật của cả dòng họ"))

    def test_preview_accepts_unsaved_document_and_keeps_draft_untouched(self):
        calls = self.capture()
        self.call("POST", "/api/templates", {"type": "thumbnail", "id": "thumb_y", "name": "Thumb Y"})
        doc = self.call("GET", "/api/templates/thumb_y")[1]["template"]
        doc["description"] = "chưa lưu"
        c, r = self.call("POST", "/api/templates/thumb_y/preview", {"template": doc})
        self.assertEqual(c, 200)
        self.assertEqual(calls[-1]["template"]["description"], "chưa lưu")                                  # xem trước chính bản đang sửa
        self.assertIsNone(calls[-1]["id"])
        self.assertEqual(self.call("GET", "/api/templates/thumb_y")[1]["template"]["description"], "")      # nhưng không tự lưu

    def test_test_render_is_separate_and_disabled_with_reason_when_ffmpeg_is_missing(self):
        calls = self.capture()
        self.call("POST", "/api/templates", {"type": "video", "id": "vid_x", "name": "Vid X"})
        self.app.templates.samples.ffmpeg = lambda: None
        s = self.call("GET", "/api/templates/preview-sources?type=video")[1]
        self.assertFalse(s["test_render"]["enabled"])
        self.assertIn("ffmpeg", s["test_render"]["reason"])
        c, e = self.call("POST", "/api/templates/vid_x/test-render", {})
        self.assertEqual((c, e["error"]["code"]), (400, "TEST_RENDER_UNAVAILABLE"))
        self.assertIn("ffmpeg", e["error"]["hint"])
        self.assertEqual(calls, [])
        c, r = self.call("POST", "/api/templates/vid_x/preview", {})                                       # xem trước vẫn dùng được (không cần ffmpeg)
        self.assertEqual(c, 200)
        self.assertTrue(self.call("GET", "/api/templates/preview-sources?type=thumbnail")[1]["test_render"]["enabled"])      # thumbnail không cần ffmpeg


@unittest.skipUnless(HAVE_REAL, "cần ContentFlow thật")
class RealTemplateApiTest(_Http):
    def orc(self):                                                                                                 # UiCase.setUp dùng self.orc(): dựng bằng ContentFlow thật + user_root tạm
        self.user_root = Path(tempfile.mkdtemp(prefix="cf-user-"))
        self.addCleanup(shutil.rmtree, self.user_root, True)
        write_config(self.root, adapters={"render": "contentflow"},
                     tools={"contentflow": {"root": str(REPO / "modules" / "ContentFlow"), "python": REAL_PY, "user_root": str(self.user_root),
                                            "base_dir": str(self.root / "cfbase")}})
        return Orchestrator(load_config(self.root))

    def test_real_preview_with_pool_frame_samples_and_invalid_template(self):
        import subprocess
        ff = shutil.which("ffmpeg")
        pool = Path(tempfile.mkdtemp(prefix="cf-pool-"))
        self.addCleanup(shutil.rmtree, pool, True)
        subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10:duration=3", "-pix_fmt", "yuv420p", str(pool / "a.mp4")], check=True, timeout=60)
        self.o.cfg.data.setdefault("render", {})["pools"] = {"demo": {"raw_dir": str(pool)}}
        s = self.call("GET", "/api/templates/preview-sources?type=thumbnail")[1]
        self.assertIn("frame:demo:0", [i["id"] for i in s["images"]])
        self.call("POST", "/api/templates", {"type": "thumbnail", "id": "real_pv", "name": "Real PV"})
        doc = self.call("GET", "/api/templates/real_pv")[1]["template"]
        outs = {}
        for key, sample in (("builtin", {"id": "s1"}), ("title2", {"id": "s2"}), ("frame", {"id": "s1", "image": "frame:demo:0"})):
            c, pv = self.call("POST", "/api/templates/real_pv/preview", {"template": doc, "sample": sample})
            self.assertEqual(c, 200, pv)
            outs[key] = self.call("GET", pv["url"])[1]
            self.assertEqual(outs[key][:8], PNG_MAGIC)
        self.assertNotEqual(outs["builtin"], outs["title2"])                                               # đổi mẫu chữ thì ảnh đổi
        self.assertNotEqual(outs["builtin"], outs["frame"])                                                # đổi ảnh nền thì ảnh đổi
        bad = json.loads(json.dumps(doc))
        bad["elements"][0]["width"] = -1
        c, e = self.call("POST", "/api/templates/real_pv/preview", {"template": bad})
        self.assertEqual(c, 400)
        self.assertTrue(e["error"]["message"])                                                              # lỗi rõ ràng, không 500
        c, e = self.call("POST", "/api/templates/real_pv/preview", {"template": doc, "sample": {"image": "frame:demo:7"}})
        self.assertEqual((c, e["error"]["code"]), (400, "BAD_SAMPLE"))

    def test_edit_in_place_and_delete_with_real_contentflow(self):
        def row(tid):
            return next(t for t in self.call("GET", "/api/templates")[1]["templates"] if t["id"] == tid)
        self.call("POST", "/api/templates", {"type": "video", "id": "real_del", "name": "Real Del"})
        a = row("real_del")["actions"]
        self.assertEqual((a["delete"]["enabled"], sorted(a)), (True, ["delete", "duplicate", "open"]))
        doc = self.call("GET", "/api/templates/real_del")[1]["template"]
        doc["description"] = "sửa tại chỗ"
        c, saved = self.call("PUT", "/api/templates/real_del/1", {"template": doc})                       # đã "dùng được" vẫn sửa được
        self.assertEqual((c, saved["template"]["description"], saved["template"]["status"]), (200, "sửa tại chỗ", "published"))
        self.assertIn("real_del", [r["id"] for r in self.call("GET", "/api/templates/options")[1]["options"]["youtube_video"]])
        self.assertEqual(self.call("DELETE", "/api/templates/real_del")[1]["deleted"], "real_del")
        self.assertTrue(self.call("DELETE", "/api/templates/real_del")[1]["already_deleted"])
        self.assertNotIn("real_del", [t["id"] for t in self.call("GET", "/api/templates")[1]["templates"]])
        c, e = self.call("DELETE", "/api/templates/thumb_default")
        self.assertEqual((c, e["error"]["code"]), (400, "TEMPLATE_READONLY"))

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
