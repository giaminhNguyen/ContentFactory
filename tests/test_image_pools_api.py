"""Phase 8 — API giao diện của Image Pool: quản lý pool, quét, ảnh xem thử theo chỉ số, hiển thị/đổi ảnh thumbnail của job, nguồn ảnh cho xem trước template."""
import json
import shutil
import tempfile
from pathlib import Path

from tests.test_automode import write_channel
from tests.test_image_pools import fill
from tests.test_templates_api import PNG_MAGIC, _Http
from tests.test_ui import URL


class ImagePoolApiTest(_Http):
    def setUp(self):
        super().setUp()
        self.pool = Path(tempfile.mkdtemp(prefix="cf-pool-"))
        self.addCleanup(shutil.rmtree, self.pool, True)
        fill(self.pool, 5)
        (self.pool / "hong.png").write_text("không phải ảnh " * 8, encoding="utf-8")

    def put_pool(self, name="anime", mode="shuffle"):
        return self.call("PUT", f"/api/image-pools/{name}", {"folder": str(self.pool), "selection_mode": mode})

    def use_in_channel(self, pool="anime", mode="shuffle", channel="kenh"):
        write_channel(self.root, channel, {"name": "Kênh Thử", "publishing": {"made_for_kids": False}, "thumbnail": {"image_pool": pool, "selection_mode": mode}})

    def test_create_list_scan_and_serve_preview_by_index_only(self):
        c, r = self.put_pool()
        self.assertEqual((c, r["saved"], r["valid"], r["invalid"], r["state"]), (200, True, 5, 1, "ready"))
        c, ov = self.call("GET", "/api/image-pools")
        self.assertEqual([p["name"] for p in ov["pools"]], ["anime"])
        self.assertEqual([m["id"] for m in ov["modes"]], ["shuffle", "random", "sequential"])
        self.assertTrue(all(m["help"] for m in ov["modes"]))
        c, sc = self.call("POST", "/api/image-pools/anime/scan", {})
        self.assertEqual((len(sc["images"]), sc["invalid_files"][0]["rel"]), (5, "hong.png"))
        self.assertIn("JPEG/PNG/WebP", sc["invalid_files"][0]["problem"])
        c, img = self.call("GET", "/api/image-pools/anime/images/0")
        self.assertEqual((c, img[:8]), (200, PNG_MAGIC))
        self.assertEqual(self.call("GET", "/api/image-pools/anime/images/99")[0], 404)
        self.assertEqual(self.call("GET", "/api/image-pools/khong_co/images/0")[0], 404)
        for bad in ("/api/image-pools/anime/images/..%2Fx", "/api/image-pools/..%2Fetc/images/0"):
            self.assertIn(self.call("GET", bad)[0], (400, 404))
        local = json.loads((self.root / "config" / "config.local.json").read_text(encoding="utf-8"))
        self.assertEqual(local["image_pools"]["anime"]["selection_mode"], "shuffle")              # lưu vào config máy, không đụng config.json

    def test_validation_errors_are_friendly(self):
        for body, code in (({"folder": str(self.pool / "khong_co")}, "INVALID_POOL_DIR"), ({"folder": str(self.pool), "selection_mode": "xyz"}, "BAD_SELECTION_MODE")):
            c, e = self.call("PUT", "/api/image-pools/anime", body)
            self.assertEqual((c, e["error"]["code"]), (400, code))
            self.assertTrue(e["error"]["hint"])
        c, e = self.call("PUT", "/api/image-pools/b%20ad", {"folder": str(self.pool)})
        self.assertIn(c, (400, 404))
        empty = Path(tempfile.mkdtemp(prefix="cf-empty-"))
        self.addCleanup(shutil.rmtree, empty, True)
        c, r = self.call("PUT", "/api/image-pools/trong", {"folder": str(empty)})
        self.assertEqual((c, r["state"], r["valid"]), (200, "problem", 0))                          # tạo được nhưng cảnh báo rõ
        self.assertTrue(r["problems"])
        shutil.rmtree(empty)                                                                        # thư mục biến mất (ổ đĩa tắt) -> danh sách vẫn trả về, không lỗi
        c, ov = self.call("GET", "/api/image-pools")
        gone = next(p for p in ov["pools"] if p["name"] == "trong")
        self.assertEqual((c, gone["state"], gone["exists"]), (200, "problem", False))

    def test_delete_is_blocked_while_a_channel_uses_the_pool(self):
        self.put_pool()
        self.use_in_channel()
        c, ov = self.call("GET", "/api/image-pools")
        self.assertEqual(ov["pools"][0]["used_by"][0]["channel"], "kenh")
        c, e = self.call("DELETE", "/api/image-pools/anime")
        self.assertEqual((c, e["error"]["code"]), (400, "POOL_IN_USE"))
        self.assertIn("Kênh Thử", e["error"]["message"])
        write_channel(self.root, "kenh", {"name": "Kênh Thử", "publishing": {"made_for_kids": False}})
        self.assertEqual(self.call("DELETE", "/api/image-pools/anime")[1], {"deleted": True})
        self.assertEqual(self.call("GET", "/api/image-pools")[1]["pools"], [])
        self.assertEqual(self.call("DELETE", "/api/image-pools/anime")[1]["error"]["code"], "IMAGE_POOL_NOT_FOUND")

    def test_job_exposes_its_image_and_reroll_over_http(self):
        self.put_pool()
        self.use_in_channel()
        c, r = self.call("POST", "/api/runs", {"input": {"value": URL}, "channel": "kenh", "run": "full"})
        jid = r["job_id"]
        self.app.stop()                                                                            # không để vòng nền chạy mất job trong test
        c, d = self.call("GET", f"/api/jobs/{jid}")
        t = d["thumbnail"]
        self.assertEqual((t["pool"], t["can_reroll"], d["actions"]["reroll_thumbnail"]), ("anime", True, True))
        self.assertNotIn("folder", json.dumps(t))                                                   # không lộ đường dẫn máy
        c, img = self.call("GET", t["image_url"])
        self.assertEqual((c, img[:8]), (200, PNG_MAGIC))
        c, imp = self.call("POST", f"/api/jobs/{jid}/reroll-thumbnail/impact", {})
        acts = {s["id"]: s["action"] for s in imp["stages"]}
        self.assertEqual(acts["render_youtube"], "RUN")                                             # job mới: chưa chạy nên chỉ “sẽ chạy”
        c, rr = self.call("POST", f"/api/jobs/{jid}/reroll-thumbnail", {})
        self.assertEqual((c, rr["status"]), (200, "applied"))
        t2 = self.call("GET", f"/api/jobs/{jid}")[1]["thumbnail"]
        self.assertNotEqual(t2["sha"], t["sha"])
        self.assertEqual(t2["rerolls"], 1)
        self.assertTrue(rr["message"])

    def test_finished_job_cannot_be_rerolled_in_place_and_says_why(self):
        self.put_pool()
        self.use_in_channel()
        c, r = self.call("POST", "/api/runs", {"input": {"value": URL}, "channel": "kenh", "run": "full"})
        jid = r["job_id"]
        self.app.stop()
        self.o.run()
        d = self.call("GET", f"/api/jobs/{jid}")[1]
        self.assertFalse(d["thumbnail"]["can_reroll"])
        self.assertIn("Chạy lại với thay đổi", d["thumbnail"]["reroll_blocked"])
        self.assertFalse(d["actions"]["reroll_thumbnail"])
        c, e = self.call("POST", f"/api/jobs/{jid}/reroll-thumbnail", {})
        self.assertEqual(c, 400)

    def test_template_preview_sources_include_pool_images_and_never_accept_paths(self):
        self.put_pool()
        s = self.call("GET", "/api/templates/preview-sources?type=thumbnail")[1]
        pool_imgs = [i["id"] for i in s["images"] if i["id"].startswith("pool:anime:")]
        self.assertEqual(pool_imgs, ["pool:anime:0", "pool:anime:1", "pool:anime:2"])
        self.assertIn("img_000.png", s["images"][1]["label"] + s["images"][2]["label"] + s["images"][3]["label"])
        v = self.call("GET", "/api/templates/preview-sources?type=video")[1]
        self.assertEqual([i["id"] for i in v["images"]], ["builtin"])
        calls = []
        api = self.app.templates.ops.api
        d = Path(tempfile.mkdtemp(prefix="cf-pv-"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "previews").mkdir()
        (d / "previews" / "x.png").write_bytes(PNG_MAGIC)
        api.preview = lambda **kw: (calls.append(kw) or {"path": str(d / "previews" / "x.png"), "canvas": [1, 1], "warnings": []})
        self.call("POST", "/api/templates", {"type": "thumbnail", "id": "thumb_pool", "name": "Thumb Pool"})
        c, r = self.call("POST", "/api/templates/thumb_pool/preview", {"sample": {"image": "pool:anime:1"}})
        self.assertEqual(c, 200, r)
        self.assertEqual(Path(calls[-1]["sample"]["image"]), self.pool.resolve() / "img_001.png")
        for bad in ("pool:anime:9", "pool:nope:0", "pool:anime:../../x", "pool:anime:0:1"):
            c, e = self.call("POST", "/api/templates/thumb_pool/preview", {"sample": {"image": bad}})
            self.assertEqual(c, 400, bad)
        self.assertEqual(len(calls), 1)
