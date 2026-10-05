"""Phase 10 — template system: Channel Config chọn template, job chốt version + snapshot, RenderAdapter gửi tham chiếu (không tọa độ),
khóa cache/invalidation, tương thích layout cũ, migrate, API giao diện. Phần ContentFlow THẬT (Pillow + ffmpeg) chạy khi có CF_TEST_CONTENTFLOW_PYTHON."""
import copy
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

from contentfactory.adapters.fake import FakeRender
from contentfactory.contracts import ErrorClass, StageError
from contentfactory.jobs import pipeline as P
from contentfactory.orchestrator import channels as CH
from contentfactory.orchestrator import ops
from contentfactory.orchestrator import templates as TPL
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.runner import Orchestrator
from contentfactory.orchestrator.stages import StageContract
from contentfactory.orchestrator.template_ops import TemplateOps
from contentfactory.render import profile as PF
from contentfactory.render.contentflow import ContentFlowRender
from contentfactory.render.manager import RenderManager
from contentfactory.tts.autotune import make_ctx
from tests.support import REPO, RootCase, params, wait_until
from tests.test_automode import LENIENT_AUDIO, write_channel, write_config
from tests.test_render import FAKE_CF, FakeCFCase, NO_WAIT

REAL_PY = os.environ.get("CF_TEST_CONTENTFLOW_PYTHON")
HAVE_REAL = bool(REAL_PY and Path(REAL_PY).exists() and shutil.which("ffmpeg") and shutil.which("ffprobe") and (REPO / "modules" / "ContentFlow").is_dir()
                 and (REPO / "modules" / "ContentFlow" / "templating").is_dir())
KIDS = {"publishing": {"made_for_kids": False}, "preset": {"audio": LENIENT_AUDIO}}


def channel(root: Path, cid: str = "k", templates: dict | None = None, **extra) -> None:
    write_channel(root, cid, {"name": "Kênh K", **KIDS, **({"templates": templates} if templates is not None else {}), **extra})


def publish_new(api, tid: str, ttype: str = "video", versions: int = 1) -> None:
    """Tạo template user `tid` và publish đủ `versions` version (v1..vN), đúng vòng đời thật: draft -> publish -> new_draft -> publish."""
    api.create_draft(type=ttype, id=tid, name=tid.replace("_", " ").title())
    api.publish(id=tid, version=1)
    for n in range(2, versions + 1):
        api.new_draft(id=tid)
        api.publish(id=tid, version=n)


class TemplateCase(RootCase):
    def orc(self) -> Orchestrator:
        return Orchestrator(load_config(self.root))

    def api(self, orc):
        return orc.adapters["render"].templates

    def run_to_end(self, orc: Orchestrator, **kw) -> str:
        jid = orc.submit(params(**kw))
        orc.run()
        return jid

    def video_text(self, jid: str, stage: str, name: str) -> str:
        return (self.job_dir(jid) / "render" / stage / name).read_text(encoding="utf-8")


# ============================================================================================= Channel Config
class ChannelConfigTest(TemplateCase):
    def test_section_forms_and_validation(self):
        out, errs = TPL.normalize_section({"thumbnail": "thumb_gold", "youtube_video": {"id": "youtube_framed", "version_policy": 3, "fallback": "youtube_default"}})
        self.assertEqual(errs, [])
        self.assertEqual(out["thumbnail"], {"id": "thumb_gold", "version_policy": "latest_published"})
        self.assertEqual(out["youtube_video"], {"id": "youtube_framed", "version_policy": 3, "fallback": "youtube_default"})
        for bad in ({"x": "a"}, {"thumbnail": 5}, {"thumbnail": {"id": ""}}, {"thumbnail": {"id": "a", "version_policy": "newest"}},
                    {"thumbnail": {"id": "a", "version_policy": 0}}, {"thumbnail": {"id": "a", "x": 10, "y": 20}}, "string"):
            self.assertTrue(TPL.normalize_section(bad)[1], str(bad))

    def test_channel_loads_template_choices_and_rejects_layout_fields(self):
        channel(self.root, "k", {"thumbnail": {"id": "thumb_gold"}, "tiktok_video": "tiktok_framed"})
        ch = CH.load_channel(load_config(self.root), "k")
        self.assertEqual(ch["templates"]["tiktok_video"]["id"], "tiktok_framed")
        self.assertEqual(ch["templates"]["thumbnail"]["version_policy"], "latest_published")
        channel(self.root, "bad", {"thumbnail": {"id": "thumb_gold", "x": 10, "title_y": 5}})                # tọa độ KHÔNG thuộc Channel Config
        with self.assertRaises(StageError) as e:
            CH.load_channel(load_config(self.root), "bad")
        self.assertEqual(e.exception.code, "INVALID_CHANNEL_CONFIG")
        self.assertIn("templates.thumbnail.x", e.exception.message)
        channel(self.root, "none")
        self.assertEqual(CH.load_channel(load_config(self.root), "none")["templates"], {})                   # không bắt buộc, có mặc định


# ============================================================================================= chọn + chốt lúc tạo job
class SelectionTest(TemplateCase):
    def test_defaults_when_channel_chooses_nothing(self):
        channel(self.root)
        o = self.orc()
        jid = o.submit(params(channel="k"))
        t = o.store.get_job(jid)["params"]["templates"]
        self.assertEqual({k: v["id"] for k, v in t.items()}, {"thumbnail": "thumb_default", "youtube": "youtube_default", "tiktok": "tiktok_default"})
        self.assertTrue(all(v["version"] == 1 and v["checksum"] and v["template"]["id"] == v["id"] for v in t.values()))
        auto = {d["what"]: d for d in o.store.get_job(jid)["params"]["auto"]}
        self.assertIn("youtube_default@v1", auto["template.youtube"]["value"])
        self.assertIn("mặc định", auto["template.youtube"]["why"])

    def test_channel_choice_and_pinned_version(self):
        channel(self.root, "k", {"thumbnail": "thumb_gold", "youtube_video": {"id": "story_frame", "version_policy": 2}, "tiktok_video": "tiktok_framed"})
        o = self.orc()
        publish_new(self.api(o), "story_frame", versions=3)
        t = o.store.get_job(o.submit(params(channel="k")))["params"]["templates"]
        self.assertEqual((t["thumbnail"]["id"], t["youtube"]["id"], t["youtube"]["version"], t["tiktok"]["id"]), ("thumb_gold", "story_frame", 2, "tiktok_framed"))

    def test_unusable_template_is_rejected_with_an_actionable_message_and_no_job(self):
        o = self.orc()
        self.api(o).create_draft(type="video", id="only_draft", name="Only Draft")
        for tpl, code in (({"youtube_video": "ghost"}, "TEMPLATE_NOT_FOUND"), ({"youtube_video": "only_draft"}, "NO_PUBLISHED_VERSION"),
                          ({"thumbnail": "youtube_default"}, "TEMPLATE_WRONG_TYPE"), ({"youtube_video": {"id": "youtube_default", "version_policy": 9}}, "TEMPLATE_VERSION_NOT_FOUND")):
            channel(self.root, "k", tpl)
            with self.assertRaises(StageError, msg=str(tpl)) as e:
                o.submit(params(channel="k"))
            self.assertEqual(e.exception.code, "INVALID_CHANNEL_TEMPLATE")
            self.assertEqual(e.exception.detail["template_error"], code)
            self.assertIn("kênh 'k'", e.exception.message)
            self.assertEqual(len(o.store.list_jobs()), 0)                                    # không tạo job nửa vời

    def test_fallback_only_when_declared(self):
        channel(self.root, "k", {"youtube_video": {"id": "ghost", "fallback": "youtube_default"}})
        o = self.orc()
        jid = o.submit(params(channel="k"))
        self.assertEqual(o.store.get_job(jid)["params"]["templates"]["youtube"]["id"], "youtube_default")
        auto = {d["what"] for d in o.store.get_job(jid)["params"]["auto"]}
        self.assertIn("template.youtube.fallback", auto)
        channel(self.root, "k2", {"youtube_video": {"id": "ghost"}})
        with self.assertRaises(StageError):
            o.submit(params(channel="k2"))

    def test_archived_template_is_not_offered_but_old_version_still_resolves(self):
        o = self.orc()
        api = self.api(o)
        publish_new(api, "retired", versions=2)
        api.archive(id="retired")
        channel(self.root, "k", {"youtube_video": "retired"})
        with self.assertRaises(StageError) as e:
            o.submit(params(channel="k"))
        self.assertEqual(e.exception.detail["template_error"], "NO_PUBLISHED_VERSION")
        self.assertEqual(api.resolve(id="retired", policy=1)["version"], 1)                    # job cũ vẫn tái hiện được
        self.assertNotIn("retired", [r["id"] for r in api.list_templates(type="video")["templates"]])
        self.assertIn("retired", [r["id"] for r in api.list_templates(type="video", include_archived=True)["templates"]])

    def test_jobs_that_stop_before_render_do_not_need_templates(self):
        channel(self.root, "k", {"youtube_video": "ghost"})
        o = self.orc()
        jid = o.submit(params(channel="k"), mode="STORY_ONLY")                              # không tới render => không cần (và không gọi) hệ thống template
        self.assertNotIn("templates", o.store.get_job(jid)["params"])
        with self.assertRaises(StageError) as e:                                            # mở rộng sang render => chốt lúc đó; sai thì báo, KHÔNG đổi đích
            o.set_target(jid, "render_youtube")
        self.assertEqual(e.exception.code, "INVALID_CHANNEL_TEMPLATE")
        self.assertEqual(o.store.get_job(jid)["target_stage"], "story")

    def test_legacy_layout_keeps_working_and_is_called_out(self):
        write_config(self.root, render={"profiles": {"youtube": {"viewport": {"x": 10, "y": 10, "width": 100, "height": 100}}}})
        channel(self.root)
        o = self.orc()
        jid = o.submit(params(channel="k"))
        p = o.store.get_job(jid)["params"]
        self.assertNotIn("youtube", p["templates"])                                            # layout cũ: không có template video YouTube...
        self.assertIn("tiktok", p["templates"])                                                # ...nhưng TikTok/thumbnail vẫn dùng template
        auto = {d["what"]: d for d in p["auto"]}
        self.assertEqual(auto["template.youtube"]["value"], "legacy")
        self.assertIn("deprecated", auto["template.youtube"]["why"])
        channel(self.root, "k2", {"youtube_video": "youtube_framed"})                          # chọn template rõ ràng => template thắng, layout cũ bị bỏ qua (có ghi)
        p2 = o.store.get_job(o.submit(params(channel="k2")))["params"]
        self.assertEqual(p2["templates"]["youtube"]["id"], "youtube_framed")
        self.assertIn("template.youtube.legacy_ignored", {d["what"] for d in p2["auto"]})


# ============================================================================================= snapshot / reproducibility
class SnapshotTest(TemplateCase):
    def test_old_job_keeps_v2_after_v3_is_published_and_new_job_gets_v3(self):
        channel(self.root, "k", {"youtube_video": "story_frame"})
        o = self.orc()
        publish_new(self.api(o), "story_frame", versions=2)
        a = o.submit(params(channel="k"))                                                    # resolve latest_published = v2 ngay lúc tạo
        self.assertEqual(o.store.get_job(a)["params"]["templates"]["youtube"]["version"], 2)
        self.api(o).new_draft(id="story_frame")
        self.api(o).publish(id="story_frame", version=3)                                     # v3 xuất bản SAU khi job A đã tạo
        o.run()                                                                                # A chạy bây giờ: vẫn phải dùng v2
        self.assertIn("template=story_frame@v2", self.video_text(a, "youtube", "video.mp4"))
        b = o.submit(params(channel="k"))
        self.assertEqual(o.store.get_job(b)["params"]["templates"]["youtube"]["version"], 3)
        o.run()
        self.assertIn("template=story_frame@v3", self.video_text(b, "youtube", "video.mp4"))

    def test_retry_and_restart_use_the_snapshot_not_latest_published(self):
        channel(self.root, "k", {"youtube_video": "story_frame"})
        gate = self.root / "down"
        gate.write_text("x")
        o = self.orc()
        publish_new(self.api(o), "story_frame", versions=1)
        jid = o.submit(params(channel="k", fake={"render_youtube": {"fail_while_file": str(gate), "error_class": "POLICY", "code": "BOOM"}}))
        o.run()
        self.assertEqual(o.store.get_job(jid)["state"], P.FAILED)
        self.api(o).new_draft(id="story_frame")
        self.api(o).publish(id="story_frame", version=2)                                     # đổi template trong lúc job đang lỗi
        gate.unlink()
        o2 = self.orc()                                                                       # "restart": orchestrator mới, DB cũ, adapter fake mới (registry v1 mới tinh)
        j = o2.store.get_job(jid)
        self.assertEqual(j["params"]["templates"]["youtube"]["version"], 1)                    # snapshot sống qua restart
        o2.retry(jid)
        o2.run()
        self.assertEqual(o2.store.get_job(jid)["state"], P.PUBLISHED)
        self.assertIn("template=story_frame@v1", self.video_text(jid, "youtube", "video.mp4"))

    def test_explicit_retemplate_changes_only_that_kind(self):
        channel(self.root)
        o = self.orc()
        jid = o.submit(params(channel="k"), mode="THROUGH_TTS")
        before = copy.deepcopy(o.store.get_job(jid)["params"])
        self.assertNotIn("templates", before)
        o.set_target(jid, "render_tiktok")                                                    # đích mở rộng tới render: chốt template khi đó
        t0 = o.store.get_job(jid)["params"]["templates"]
        self.assertEqual(t0["youtube"]["id"], "youtube_default")
        snap = o.retemplate(jid, "youtube", "youtube_framed")
        t1 = o.store.get_job(jid)["params"]["templates"]
        self.assertEqual((snap["id"], t1["youtube"]["id"], t1["tiktok"], t1["thumbnail"]), ("youtube_framed", "youtube_framed", t0["tiktok"], t0["thumbnail"]))
        with self.assertRaises(StageError):
            o.retemplate(jid, "youtube", "thumb_gold")                                       # sai loại
        with self.assertRaises(StageError):
            o.retemplate(jid, "instagram", "youtube_default")


class RerenderWithNewTemplateTest(TemplateCase):
    def test_new_template_rerenders_video_only_and_never_reruns_tts_or_audio(self):
        channel(self.root, "k", {"youtube_video": "youtube_default", "tiktok_video": "tiktok_default", "thumbnail": "thumb_default"})
        o = self.orc()
        a = self.run_to_end(o, channel="k")
        self.assertEqual(o.store.get_job(a)["state"], P.PUBLISHED)
        channel(self.root, "k", {"youtube_video": "youtube_framed", "tiktok_video": "tiktok_framed", "thumbnail": "thumb_gold"})   # đổi template của kênh
        o2 = self.orc()
        b = ops.rerender(o2, a)                                                                                                       # dựng lại từ audio của job cũ
        o2.run()
        self.assertEqual(o2.store.get_job(b)["params"]["templates"]["youtube"]["id"], "youtube_framed")
        self.assertIn("template=youtube_framed@v1", self.video_text(b, "youtube", "video.mp4"))
        self.assertIn("template=thumb_gold@v1", self.video_text(b, "youtube", "thumbnail.jpg"))
        self.assertIn("template=youtube_default@v1", self.video_text(a, "youtube", "video.mp4"))                                      # job cũ giữ nguyên
        ran = {r["stage"] for r in o2.store.stage_runs(b)}
        self.assertFalse(ran & {"source", "story", "tts", "audio"}, ran)                                                                # Source/Story/TTS/Audio không chạy lại


class InvalidationTest(TemplateCase):
    """Đổi template nào thì chỉ output render tương ứng hết hạn (stage_key); Source/Story/TTS/Audio không đụng."""

    def keys(self, tpls: dict) -> dict:
        p = params(channel="k", templates=tpls)
        return {s.name: StageContract(s).stage_key(p, {"semantic": {}}, {}) for s in P.STAGES}

    def snap(self, o, tid, kind_type):
        return self.api(o).resolve(id=tid, expect_type=kind_type)

    def test_each_template_change_touches_only_its_own_render_stage(self):
        channel(self.root)
        o = self.orc()
        base = {"thumbnail": self.snap(o, "thumb_default", "thumbnail"), "youtube": self.snap(o, "youtube_default", "video"), "tiktok": self.snap(o, "tiktok_default", "video")}
        k0 = self.keys(base)
        yt = self.keys({**base, "youtube": self.snap(o, "youtube_framed", "video")})
        tt = self.keys({**base, "tiktok": self.snap(o, "tiktok_framed", "video")})
        th = self.keys({**base, "thumbnail": self.snap(o, "thumb_gold", "thumbnail")})
        changed = lambda k: sorted(n for n in k if k[n] != k0[n])
        self.assertEqual(changed(yt), ["render_youtube"])
        self.assertEqual(changed(tt), ["render_tiktok"])
        self.assertEqual(changed(th), ["render_youtube"])                                      # thumbnail được dựng trong stage render_youtube, nhưng KHÔNG đụng stage nào khác
        for k in (yt, tt, th):
            for stage in ("source", "story", "tts", "audio", "output", "publish"):
                self.assertEqual(k[stage], k0[stage], stage)


class RenderManagerTemplateCacheTest(FakeCFCase):
    """Cùng audio + cùng template/version => dùng lại; template/version mới => render lại, không tái dùng bố cục cũ."""

    def setUp(self):
        super().setUp()
        self.stage_dir = self.root / "ws" / "render" / "tiktok"
        self.stage_dir.mkdir(parents=True)
        for i in (1, 2, 3):
            (self.root / "ws" / f"audio_{i}.wav").write_bytes(f"audio-{i}".encode())
        self.fake = FakeRender()

    def run_tiktok(self, tpl: dict | None) -> dict:
        refs = [{"path": f"audio_{i}.wav", "kind": "audio_tiktok", "sha256": f"s{i}", "bytes": 7, "meta": {"index": i}} for i in (1, 2, 3)]
        c = make_ctx(self.root / "ws")
        c.stage_dir, c.workspace = self.stage_dir, self.root / "ws"
        c.inputs = {"audio_tiktok": refs}
        c.params = {"render": {"tiktok": NO_WAIT}, **({"templates": {"tiktok": tpl}} if tpl else {})}
        c.config = {"render": {}}
        return RenderManager(self.fake).tiktok(c).data

    def test_same_template_reuses_new_version_or_template_rerenders(self):
        api = self.fake.templates
        v1 = api.resolve(id="tiktok_default")
        self.assertEqual(self.run_tiktok(v1)["rendered"], 3)
        d = self.run_tiktok(v1)
        self.assertEqual((d["rendered"], d["reused"]), (0, 3))
        self.assertIn("template=tiktok_default@v1", (self.stage_dir / "part_01.mp4").read_text())
        framed = api.resolve(id="tiktok_framed")
        d = self.run_tiktok(framed)
        self.assertEqual((d["rendered"], d["reused"]), (3, 0))
        self.assertIn("template=tiktok_framed@v1", (self.stage_dir / "part_01.mp4").read_text())
        pub = copy.deepcopy(v1)
        pub["fingerprint"] = "different-fingerprint"                                             # cùng id/version nhưng asset/nội dung khác => KHÔNG được tái dùng
        self.assertEqual(self.run_tiktok(pub)["rendered"], 3)

    def test_profile_comes_from_the_template_canvas(self):
        prof = PF.apply_template(PF.resolve("tiktok", None, None), {"id": "x", "version": 1, "fingerprint": "f", "summary": {"canvas": [720, 1280], "fps": 24}})
        self.assertEqual((prof["width"], prof["height"], prof["resolution"], prof["fps"], prof["frame_path"], prof["viewport"]), (720, 1280, "720x1280", 24, None, None))
        self.assertEqual(prof["template"], {"id": "x", "version": 1, "fingerprint": "f"})


# ============================================================================================= RenderAdapter gửi gì
class AdapterRequestTest(FakeCFCase):
    def captured(self, req):
        a = self.adapter()
        seen = {}

        def fake_run(jtype, key, out_dir, params, inputs, deadline_s, ctx, on_progress=None):
            seen.update(jtype=jtype, params=params, inputs=inputs)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / params["output_name"]).write_bytes(b"x" * 10)
            return {"events": [], "artifacts": [], "replayed": False}
        a._run_worker = fake_run
        a._check_video = lambda *x, **k: {"verified": False}
        a._probe = lambda p: None
        if "thumb" in req:
            a.render_thumbnail(req["thumb"], type("C", (), {"cancel": None, "job_id": "j", "attempt": 1})())
        else:
            a.render_video(req, type("C", (), {"cancel": None, "job_id": "j", "attempt": 1})())
        return seen

    def test_template_request_is_semantic_only(self):
        snap = FakeRender().templates.resolve(id="youtube_framed")
        prof = PF.apply_template(PF.resolve("youtube", None, None), snap)
        seen = self.captured({"audio": self.audio(), "profile": prof, "output": self.root / "o" / "v.mp4", "key": "k", "template": snap})
        self.assertEqual(seen["params"]["template"]["id"], "youtube_framed")
        self.assertNotIn("config", seen["params"])                                              # không viewport, không tọa độ
        self.assertEqual([i["type"] for i in seen["inputs"]], ["audio"])                         # không frame
        flat = json.dumps({k: v for k, v in seen["params"].items() if k != "template"})
        for banned in ("viewport", "frame_padding", "title_x", "title_y", "video_width"):
            self.assertNotIn(banned, flat)

    def test_legacy_request_still_carries_frame_and_viewport(self):
        prof = PF.resolve("youtube", None, None)
        seen = self.captured({"audio": self.audio(), "profile": prof, "output": self.root / "o" / "v.mp4", "key": "k"})
        self.assertNotIn("template", seen["params"])
        self.assertIn("viewport", seen["params"]["config"]["video_generator"])
        self.assertEqual([i["type"] for i in seen["inputs"]], ["audio", "frame"])

    def test_thumbnail_template_replaces_config_overrides(self):
        snap = FakeRender().templates.resolve(id="thumb_gold")
        seen = self.captured({"thumb": {"title": "T", "channel_name": "C", "output": self.root / "o" / "t.jpg", "key": "t", "template": snap,
                                        "config_overrides": {"title": {"font": "x.ttf"}}}})
        self.assertEqual(seen["params"]["template"]["id"], "thumb_gold")
        self.assertNotIn("config", seen["params"])


# ============================================================================================= vận hành: use / doctor / migrate
class TemplateOpsTest(TemplateCase):
    def test_use_validates_and_writes_only_the_selection(self):
        channel(self.root, "k", {}, sequence={"last_used": 7})
        o = self.orc()
        ops = TemplateOps(o.cfg, api=self.api(o))
        ops.set_channel_template("k", "thumbnail", "thumb_gold")
        raw = json.loads((self.root / "channels" / "k" / "channel.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["templates"], {"thumbnail": {"id": "thumb_gold", "version_policy": "latest_published"}})
        self.assertEqual(raw["sequence"], {"last_used": 7})                                      # phần khác của kênh nguyên vẹn
        self.api(o).create_draft(type="video", id="wip", name="Wip")
        for key, tid, code in (("youtube_video", "ghost", "TEMPLATE_NOT_FOUND"), ("youtube_video", "wip", "NO_PUBLISHED_VERSION"),
                               ("youtube_video", "thumb_gold", "TEMPLATE_WRONG_TYPE")):
            with self.assertRaises(StageError, msg=tid) as e:
                ops.set_channel_template("k", key, tid)
            self.assertEqual(e.exception.code, code)
        with self.assertRaises(StageError):
            ops.set_channel_template("k", "x_y", "thumb_gold")
        ops.set_channel_template("k", "thumbnail", None)                                         # bỏ chọn => dùng mặc định
        self.assertNotIn("templates", json.loads((self.root / "channels" / "k" / "channel.json").read_text(encoding="utf-8")))
        ops.set_channel_template("k", "tiktok_video", "tiktok_framed")
        self.assertEqual([u["key"] for u in ops.usage("tiktok_framed")], ["tiktok_video"])

    def test_options_list_only_published_templates_per_key(self):
        o = self.orc()
        ops = TemplateOps(o.cfg, api=self.api(o))
        self.api(o).create_draft(type="video", id="wip", name="Wip")
        publish_new(self.api(o), "pubd")
        opt = ops.options()["options"]
        self.assertIn("pubd", [r["id"] for r in opt["youtube_video"]])
        self.assertNotIn("wip", [r["id"] for r in opt["youtube_video"]])
        self.assertTrue(all(r["id"].startswith("thumb") for r in opt["thumbnail"]))

    def test_health_reports_a_channel_template_that_stopped_working(self):
        channel(self.root, "k", {"youtube_video": "story_frame"})
        o = self.orc()
        ops = TemplateOps(o.cfg, api=self.api(o))
        self.assertTrue(any(h["level"] == "fail" and "story_frame" in h["message"] for h in ops.health()))
        publish_new(self.api(o), "story_frame")
        self.assertFalse(any(h["level"] == "fail" for h in ops.health()))

    def test_health_warns_about_legacy_layout(self):
        write_config(self.root, render={"profiles": {"tiktok": {"frame_path": "x.png"}}})
        o = self.orc()
        ops = TemplateOps(o.cfg, api=self.api(o))
        self.assertTrue(any(h["level"] == "warn" and "tiktok" in h["message"] and "migrate" in h["message"] for h in ops.health()))


class TemplateCliTest(TemplateCase):
    def cli(self, *argv) -> tuple[int, str]:
        import contextlib
        import io
        from contentfactory.orchestrator.cli import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = main(["--root", str(self.root), *argv])
        return rc, out.getvalue()

    def test_list_use_show_and_errors(self):
        channel(self.root, "k")
        rc, out = self.cli("templates", "list")
        self.assertEqual(rc, 0)
        self.assertIn("youtube_framed", out)
        self.assertIn("builtin", out)
        rc, out = self.cli("templates", "use", "k", "youtube_video", "youtube_framed")
        self.assertEqual(rc, 0)
        self.assertEqual(CH.load_channel(load_config(self.root), "k")["templates"]["youtube_video"]["id"], "youtube_framed")
        rc, out = self.cli("templates", "list")
        self.assertIn("k:youtube_video", out)                                                  # 'used by' hiện kênh đang chọn
        rc, out = self.cli("templates", "use", "k", "youtube_video", "ghost")
        self.assertEqual(rc, 2)
        self.assertIn("ghost", out)
        rc, out = self.cli("templates", "use", "k")
        self.assertEqual(rc, 2)                                                                 # thiếu tham số: báo gọn, không traceback

    def test_job_params_public_show_the_chosen_templates(self):
        from contentfactory.orchestrator.service import Service
        channel(self.root, "k", {"youtube_video": "youtube_framed"})
        o = self.orc()
        jid = o.submit(params(channel="k"))
        pub = Service._public_params(o.store.get_job(jid)["params"])
        self.assertEqual((pub["templates"]["youtube"]["id"], pub["templates"]["youtube"]["version"]), ("youtube_framed", 1))
        self.assertNotIn("template", pub["templates"]["youtube"])                               # không đẩy cả tài liệu template ra giao diện


class PipelineTemplateE2ETest(TemplateCase):
    def test_full_job_renders_youtube_thumbnail_and_tiktok_with_the_chosen_templates(self):
        channel(self.root, "k", {"thumbnail": "thumb_gold", "youtube_video": "youtube_framed", "tiktok_video": "tiktok_framed"})
        o = self.orc()
        jid = self.run_to_end(o, channel="k")
        self.assertEqual(o.store.get_job(jid)["state"], P.PUBLISHED)
        self.assertIn("template=youtube_framed@v1", self.video_text(jid, "youtube", "video.mp4"))
        self.assertIn("template=thumb_gold@v1", self.video_text(jid, "youtube", "thumbnail.jpg"))
        parts = sorted((self.job_dir(jid) / "render" / "tiktok").glob("part_*.mp4"))
        self.assertTrue(parts and all("template=tiktok_framed@v1" in p.read_text() for p in parts))
        rep = json.loads((self.job_dir(jid) / "render" / "youtube" / "render_report.json").read_text(encoding="utf-8"))
        self.assertEqual((rep["templates"]["video"]["id"], rep["templates"]["thumbnail"]["id"]), ("youtube_framed", "thumb_gold"))


# ============================================================================================= ContentFlow THẬT
@unittest.skipUnless(HAVE_REAL, "cần CF_TEST_CONTENTFLOW_PYTHON (Python có Pillow), ffmpeg/ffprobe và module ContentFlow có hệ thống template")
class RealTemplatesTest(RootCase):
    def setUp(self):
        super().setUp()
        cfg = self.root / "config" / "config.json"
        c = json.loads(cfg.read_text(encoding="utf-8"))
        self.user_root = self.root / "cf_user"
        c.update({"adapters": {"render": "contentflow"},
                  "tools": {"contentflow": {"root": str(REPO / "modules" / "ContentFlow"), "python": REAL_PY, "base_dir": str(self.root / "cfbase"),
                                            "user_root": str(self.user_root), "verify_output": True}}})
        cfg.write_text(json.dumps(c), encoding="utf-8")
        self.o = Orchestrator(load_config(self.root))
        self.api = self.o.adapters["render"].templates

    def test_lists_builtin_templates_and_isolates_user_data(self):
        ids = {t["id"] for t in self.api.list_templates()["templates"]}
        self.assertTrue({"thumb_default", "youtube_default", "tiktok_default"} <= ids)
        self.api.create_draft(type="video", id="mine", name="Mine")
        self.assertTrue((self.user_root / "templates" / "video" / "mine" / "v1.json").is_file())          # dữ liệu user ở user_root, không phải trong module

    def test_thumbnail_and_video_renders_use_the_template(self):
        a = self.o.adapters["render"]
        snap = self.api.resolve(id="thumb_gold", expect_type="thumbnail")
        out = self.root / "t" / "thumb.png"
        a.render_thumbnail({"title": "Tiêu đề thử nghiệm", "channel_name": "Kênh", "output": out, "key": "tk1", "template": snap}, type("C", (), {"cancel": None, "job_id": "j", "attempt": 1})())
        from PIL import Image
        with Image.open(out) as im:
            self.assertEqual(im.size, (1648, 928))
        t = self.api.test_render(id="tiktok_framed", version=1)
        self.assertTrue(t["ok"] and t["probe_size"] == [1080, 1920])

    def test_missing_template_in_worker_is_an_input_problem(self):
        a = self.o.adapters["render"]
        with self.assertRaises(StageError) as e:
            a.render_thumbnail({"title": "T", "channel_name": "C", "output": self.root / "t" / "x.png", "key": "tk2",
                                "template": {"id": "ghost_tpl", "version": 1}}, type("C", (), {"cancel": None, "job_id": "j", "attempt": 1})())
        self.assertEqual((e.exception.error_class, e.exception.code, e.exception.resource), (ErrorClass.POLICY, "MISSING_INPUT", "input"))
        self.assertIn("ghost_tpl", e.exception.message)

    def test_migrate_legacy_config_to_templates(self):
        from PIL import Image
        tpl = self.root / "old_template.png"
        Image.new("RGBA", (1648, 928), (255, 200, 200, 255)).save(tpl)
        frame = self.root / "old_frame.png"
        Image.new("RGBA", (1280, 720), (0, 0, 0, 0)).save(frame)
        (self.root / "config" / "config.local.json").write_text(json.dumps({"render": {"profiles": {"youtube": {
            "frame_path": str(frame), "viewport": {"x": 40, "y": 30, "width": 1200, "height": 660}, "resolution": "1280x720",
            "thumbnail": {"config_overrides": {"template": {"file": str(tpl)}}}}}}}), encoding="utf-8")
        ops = TemplateOps(load_config(self.root))
        plan = ops.migrate(apply=False)
        self.assertEqual(sorted(a["kind"] for a in plan), ["thumbnail", "youtube"])
        self.assertTrue(json.loads((self.root / "config" / "config.local.json").read_text())["render"]["profiles"]["youtube"]["frame_path"])      # dry-run không ghi
        done = ops.migrate(apply=True)
        self.assertEqual(len(done), 2)
        local = json.loads((self.root / "config" / "config.local.json").read_text())
        self.assertEqual(local["templates"]["defaults"], {"youtube_video": "legacy_youtube", "thumbnail": "legacy_thumbnail"})
        self.assertNotIn("frame_path", json.dumps(local))
        self.assertTrue(list((self.root / "config").glob("config.local.json.*.bak")))
        snap = ops.api.resolve(id="legacy_youtube", expect_type="video")
        self.assertEqual(snap["summary"]["canvas"], [1280, 720])
        self.assertEqual(ops.migrate(apply=True), [])                                              # idempotent
        # job mới dùng template đã migrate
        write_config(self.root, templates={"defaults": local["templates"]["defaults"]})
        o2 = Orchestrator(load_config(self.root))
        channel(self.root, "k")
        tpls = o2.store.get_job(o2.submit(params(channel="k"), mode="THROUGH_TTS", target_stage="render_tiktok"))["params"]["templates"]
        self.assertEqual((tpls["youtube"]["id"], tpls["thumbnail"]["id"], tpls["tiktok"]["id"]), ("legacy_youtube", "legacy_thumbnail", "tiktok_default"))


if __name__ == "__main__":
    unittest.main()
