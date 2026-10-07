"""Watermark Library: revision bất biến, active tường minh, tương thích kênh cũ, job snapshot, bảo vệ tham chiếu, stage key theo NỘI DUNG."""
import json
import shutil
import wave
from pathlib import Path

from contentfactory.contracts import StageError
from contentfactory.jobs import pipeline as P
from contentfactory.orchestrator import channels as CH
from contentfactory.orchestrator import watermarks as WM
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.stages import StageContract
from tests.support import RootCase, params
from tests.test_automode import write_channel


def wav(path: Path, byte: int = 1, seconds: float = 0.5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(bytes([byte, 0]) * int(8000 * seconds))
    return path


class LibraryCase(RootCase):
    def setUp(self):
        super().setUp()
        self.d = write_channel(self.root, "kenh", {"name": "Kênh"})
        self.orc_ = self.orc()
        self.wm = self.orc_.watermarks
        self.src = self.root / "src"

    def upload(self, name="Intro", byte=1, **kw):
        return self.wm.commit_revision("kenh", wav(self.src / f"{name}{byte}.wav", byte), source="upload", name=name, meta={"upload": {"filename": f"{name}.wav"}}, **kw)

    def code(self, fn, *a, **kw):
        with self.assertRaises(StageError) as cm:
            fn(*a, **kw)
        return cm.exception.code


class LibraryCrudTest(LibraryCase):
    def test_empty_library_and_legacy_channel_still_load(self):
        self.assertEqual(self.wm.list("kenh")["items"], [])
        wav(self.d / "watermark.wav", 3)                                                              # kênh cũ: chỉ có file + "watermark" trong channel.json
        raw = json.loads((self.d / "channel.json").read_text(encoding="utf-8"))
        raw["watermark"] = "watermark.wav"
        (self.d / "channel.json").write_text(json.dumps(raw), encoding="utf-8")
        lib = self.wm.list("kenh")
        self.assertEqual([(i["id"], i["source"], i["active"], i["valid"]) for i in lib["items"]], [("legacy", "legacy", True, True)])
        ch = CH.load_channel(self.orc_.cfg, "kenh")
        self.assertEqual(Path(ch["watermark"]), self.d / "watermark.wav")                                # pipeline cũ vẫn resolve
        got = self.wm.resolve_active(ch)
        self.assertEqual((got["ref"]["source"], got["ref"]["id"]), ("legacy", None))
        self.assertEqual(len(got["ref"]["sha256"]), 64)

    def test_create_list_get_persist_across_restart(self):
        item = self.upload("Intro")
        self.assertEqual((item["name"], item["source"], item["current_revision"], item["active"], item["valid"]), ("Intro", "upload", 1, False, True))
        self.assertAlmostEqual(item["duration_sec"], 0.5, places=2)
        again = Orchestrator_like(self.root)                                                            # khởi động lại: registry còn nguyên
        self.assertEqual([i["id"] for i in again.watermarks.list("kenh")["items"]], [item["id"]])
        self.assertEqual(again.watermarks.get("kenh", item["id"])["revisions"][0]["sha"], item["revisions"][0]["sha"])

    def test_replace_creates_new_revision_and_never_mutates_old_one(self):
        a = self.upload("Intro", 1)
        wid = a["id"]
        old_meta = self.wm.revision("kenh", wid, 1)
        old_file = self.wm.revision_path("kenh", wid, old_meta)
        before = old_file.read_bytes()
        b = self.wm.commit_revision("kenh", wav(self.src / "new.wav", 7), source="upload", wm_id=wid, meta={"upload": {"filename": "new.wav"}})
        self.assertEqual((b["current_revision"], b["revision_count"]), (2, 2))
        self.assertEqual(old_file.read_bytes(), before)                                                 # revision cũ bất biến
        self.assertNotEqual(self.wm.revision("kenh", wid, 2)["sha256"], old_meta["sha256"])
        self.assertEqual(self.code(self.wm.revision, "kenh", wid, 9), "WATERMARK_REVISION_NOT_FOUND")

    def test_rename_archive_restore_and_request_id_idempotency(self):
        a = self.upload("Intro", 1, request_id="r1")
        b = self.upload("Intro", 1, request_id="r1")                                                    # bấm đúp: không tạo thêm
        self.assertEqual((a["id"], len(self.wm.list("kenh")["items"])), (b["id"], 1))
        self.assertEqual(self.wm.rename("kenh", a["id"], "  Tên mới ")["name"], "Tên mới")
        self.assertEqual(self.code(self.wm.rename, "kenh", a["id"], "  "), "WATERMARK_NAME_EMPTY")
        self.assertEqual(self.code(self.wm.get, "kenh", "wm_zzzzzzzz"), "WATERMARK_NOT_FOUND")
        self.assertEqual(self.code(self.wm.get, "kenh", "../x"), "WATERMARK_NOT_FOUND")                 # id lạ không chạm được đường dẫn

    def test_invalid_audio_is_rejected_and_leaves_no_trace(self):
        bad = self.src / "x.wav"
        bad.parent.mkdir(parents=True)
        bad.write_bytes(b"not a wav at all")
        for path in (bad, self.src / "e.wav", self.src / "t.txt"):
            if not path.exists():
                path.write_bytes(b"" if path.suffix == ".wav" else b"hello")
            self.assertEqual(self.code(self.wm.commit_revision, "kenh", path, source="upload", name="X"), "WATERMARK_AUDIO_INVALID", path.name)
        short = wav(self.src / "short.wav", 1, 0.01)
        self.assertEqual(self.code(self.wm.commit_revision, "kenh", short, source="upload", name="X"), "WATERMARK_AUDIO_INVALID")
        self.assertEqual(self.wm.list("kenh", include_archived=True)["items"], [])
        self.assertFalse((self.d / "watermarks" / "registry.json").exists())


class ActiveAndFailureTest(LibraryCase):
    def test_activate_sets_channel_ref_and_keeps_legacy_selectable(self):
        wav(self.d / "watermark.wav", 3)
        raw = json.loads((self.d / "channel.json").read_text(encoding="utf-8"))
        raw["watermark"] = "watermark.wav"
        (self.d / "channel.json").write_text(json.dumps(raw), encoding="utf-8")
        a = self.upload("Intro", 1)
        self.wm.activate("kenh", a["id"])
        raw = json.loads((self.d / "channel.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["watermark_ref"], {"id": a["id"], "revision": 1})
        self.assertEqual(raw["watermark"], f"watermarks/{a['id']}/rev_0001.wav")
        self.assertEqual(raw["legacy_watermark"], "watermark.wav")                                       # file cũ không bị xóa, vẫn chọn lại được
        items = {i["id"]: i for i in self.wm.list("kenh")["items"]}
        self.assertEqual((items[a["id"]]["active"], items["legacy"]["active"]), (True, False))
        ch = CH.load_channel(self.orc_.cfg, "kenh")
        self.assertEqual((ch["watermark_ref"], Path(ch["watermark"]).name), ({"id": a["id"], "revision": 1}, "rev_0001.wav"))
        self.wm.activate("kenh", "legacy")
        self.assertIsNone(self.wm.active_ref("kenh"))
        self.assertTrue(self.wm.list("kenh")["items"][0]["active"])
        self.wm.deactivate("kenh")
        self.assertIsNone(CH.load_channel(self.orc_.cfg, "kenh")["watermark"])
        self.assertTrue((self.d / "watermark.wav").is_file())                                           # bỏ khỏi kênh không xóa file

    def test_replacing_the_active_watermark_moves_active_to_the_new_revision(self):
        a = self.upload("Intro", 1)
        self.wm.activate("kenh", a["id"])
        b = self.wm.commit_revision("kenh", wav(self.src / "v2.wav", 5), source="upload", wm_id=a["id"])
        self.assertEqual((b["active_revision"], self.wm.active_ref("kenh")["revision"]), (2, 2))
        other = self.upload("Khác", 9)
        self.wm.commit_revision("kenh", wav(self.src / "o2.wav", 8), source="upload", wm_id=other["id"])  # watermark KHÔNG active: active không đổi
        self.assertEqual(self.wm.active_ref("kenh"), {"id": a["id"], "revision": 2})

    def test_failure_between_audio_and_registry_keeps_active_and_ignores_orphans(self):
        a = self.upload("Intro", 1)
        self.wm.activate("kenh", a["id"])
        orig = WM.atomic_write_json

        def boom(path, obj):
            if Path(path).name.startswith("rev_"):
                raise OSError("disk full")
            return orig(path, obj)
        WM.atomic_write_json = boom
        try:
            with self.assertRaises(OSError):
                self.wm.commit_revision("kenh", wav(self.src / "v2.wav", 5), source="upload", wm_id=a["id"])
        finally:
            WM.atomic_write_json = orig
        self.assertEqual(self.wm.active_ref("kenh"), {"id": a["id"], "revision": 1})                      # active cũ còn nguyên
        self.assertEqual([m["revision"] for m in self.wm.revisions("kenh", a["id"])], [1])                # audio mồ côi (không có json) không phải revision
        self.assertTrue((self.d / "watermarks" / a["id"] / "rev_0002.wav").exists())
        stray = self.d / "watermarks" / a["id"] / "rev_0003.wav.part"
        stray.write_bytes(b"half")
        (self.d / "watermarks" / a["id"] / "rev_0004.json").write_text(json.dumps({"revision": 4, "file": "rev_0004.wav", "bytes": 1}), encoding="utf-8")
        self.assertEqual([m["revision"] for m in self.wm.revisions("kenh", a["id"])], [1])                # .part và json trỏ file thiếu đều bị bỏ qua
        c = self.wm.commit_revision("kenh", wav(self.src / "v3.wav", 6), source="upload", wm_id=a["id"])  # lần sau không đụng số revision đã dùng
        self.assertEqual(c["current_revision"], 5)

    def test_archived_watermark_cannot_be_activated(self):
        a = self.upload("Intro", 1)
        self.wm.activate("kenh", a["id"])
        self.wm.deactivate("kenh")
        reg = json.loads((self.d / "watermarks" / "registry.json").read_text(encoding="utf-8"))
        reg["watermarks"][a["id"]]["archived"] = True
        (self.d / "watermarks" / "registry.json").write_text(json.dumps(reg), encoding="utf-8")
        self.assertEqual(self.wm.list("kenh")["items"], [])
        self.assertEqual(self.code(self.wm.activate, "kenh", a["id"]), "WATERMARK_ARCHIVED")
        self.assertEqual(self.wm.restore("kenh", a["id"])["archived"], False)


class JobSnapshotAndReferenceTest(LibraryCase):
    def test_job_snapshots_revision_and_later_changes_do_not_touch_it(self):
        a = self.upload("Intro", 1)
        self.wm.activate("kenh", a["id"])
        old = self.orc_.submit(params(channel="kenh"), mode="THROUGH_TTS")
        self.wm.commit_revision("kenh", wav(self.src / "v2.wav", 5), source="upload", wm_id=a["id"])      # kênh đổi sang v2 (active đi theo)
        new = self.orc_.submit(params(channel="kenh"), mode="THROUGH_TTS")
        jo, jn = (self.orc_.store.get_job(x)["params"] for x in (old, new))
        self.assertEqual((jo["watermark_ref"]["revision"], jn["watermark_ref"]["revision"]), (1, 2))
        self.assertTrue(jo["watermark"].endswith("rev_0001.wav") and jn["watermark"].endswith("rev_0002.wav"))
        self.assertEqual(jo["watermark_ref"]["sha256"], self.wm.revision("kenh", a["id"], 1)["sha256"])
        self.assertEqual(self.wm.get("kenh", a["id"])["in_use_by_jobs"], 2)
        public = self.orc_.store.get_job(old)["params"]["watermark_ref"]
        self.assertEqual(set(public), {"id", "revision", "sha256", "source"})                            # không lộ đường dẫn/secret

    def test_missing_active_revision_fails_job_creation_instead_of_silently_dropping(self):
        a = self.upload("Intro", 1)
        self.wm.activate("kenh", a["id"])
        shutil.rmtree(self.d / "watermarks" / a["id"])
        with self.assertRaises(StageError) as cm:
            self.orc_.submit(params(channel="kenh"), mode="THROUGH_TTS")
        self.assertEqual(cm.exception.code, "WATERMARK_MISSING")

    def test_referenced_revisions_are_never_deleted_and_active_needs_confirmation(self):
        a = self.upload("Intro", 1)
        self.wm.activate("kenh", a["id"])
        self.assertEqual(self.code(self.wm.delete, "kenh", a["id"]), "WATERMARK_IN_USE")                 # đang active: không để dangling
        jid = self.orc_.submit(params(channel="kenh"), mode="THROUGH_TTS")
        r = self.wm.delete("kenh", a["id"], unset_active=True)                                           # bỏ khỏi kênh + xóa trong một thao tác
        self.assertEqual((r["result"], r["jobs"]), ("archived", 1))                                      # có job tham chiếu ⇒ chỉ lưu trữ
        self.assertIsNone(self.wm.active_ref("kenh"))
        self.assertTrue((self.d / "watermarks" / a["id"] / "rev_0001.wav").is_file())
        self.assertEqual(self.wm.list("kenh")["items"], [])                                              # ẩn khỏi danh sách mặc định
        self.assertEqual(len(self.wm.list("kenh", include_archived=True)["items"]), 1)
        self.orc_.delete_job(jid)                                                                        # job bị xóa không còn giữ tham chiếu
        free = self.upload("Tự do", 2)
        self.assertEqual(self.wm.delete("kenh", free["id"])["result"], "deleted")
        self.assertFalse((self.d / "watermarks" / free["id"]).exists())
        self.assertEqual(self.code(self.wm.get, "kenh", free["id"]), "WATERMARK_NOT_FOUND")

    def test_legacy_file_with_jobs_is_kept_when_removed_from_channel(self):
        wav(self.d / "watermark.wav", 3)
        raw = json.loads((self.d / "channel.json").read_text(encoding="utf-8"))
        raw["watermark"] = "watermark.wav"
        (self.d / "channel.json").write_text(json.dumps(raw), encoding="utf-8")
        self.orc_.submit(params(channel="kenh"), mode="THROUGH_TTS")
        self.assertEqual(self.code(self.wm.delete, "kenh", "legacy"), "WATERMARK_IN_USE")
        r = self.wm.delete("kenh", "legacy", unset_active=True)
        self.assertEqual(r["result"], "archived")                                                       # job cũ còn trỏ vào file: giữ nguyên
        self.assertTrue((self.d / "watermark.wav").is_file())


class PipelineInvalidationTest(LibraryCase):
    def test_changing_the_active_watermark_reruns_only_audio_for_the_new_job(self):
        a = self.upload("Intro", 1)
        self.wm.activate("kenh", a["id"])
        jid = self.orc_.submit(params(channel="kenh"), pipeline={"requested_stages": ["audio"]})
        self.orc_.run()
        first = self.runs(self.orc_, jid)
        self.assertEqual({k: v for k, v in first.items()}, {"source": ["succeeded"], "story": ["succeeded"], "tts": ["succeeded"], "audio": ["succeeded"]})
        yt1 = [x for x in self.orc_.store.artifacts(jid) if x["kind"] == "audio_youtube"][0]
        self.assertTrue(json.loads(yt1["meta"])["watermark"])                                           # YouTube audio dùng watermark
        self.wm.commit_revision("kenh", wav(self.src / "v2.wav", 6), source="upload", wm_id=a["id"])    # đổi watermark của kênh (active đi theo)
        kinds = sorted({k for st in P.STAGES[:3] for k in st.produces})                                  # kết quả Source/Story/TTS của job cũ
        new = self.orc_.submit(params(channel="kenh"), pipeline={"requested_stages": ["audio"]}, from_job={"job_id": jid, "kinds": kinds})
        self.orc_.run()
        again = self.runs(self.orc_, new)
        self.assertEqual(again, {"audio": ["succeeded"]})                                              # Source/Story/TTS KHÔNG chạy lại (kết quả dùng lại từ job cũ)
        old_ref, new_ref = (self.orc_.store.get_job(x)["params"]["watermark_ref"] for x in (jid, new))
        self.assertEqual((old_ref["revision"], new_ref["revision"]), (1, 2))                            # job cũ vẫn v1, job mới v2
        keys = {x: [r["stage_key"] for r in self.orc_.store.stage_runs(x) if r["stage"] == "audio"][0] for x in (jid, new)}
        self.assertNotEqual(keys[jid], keys[new])                                                        # nội dung watermark đổi ⇒ audio có khóa khác


class StageKeyTest(RootCase):
    def key(self, stage, p):
        return StageContract(P.BY_NAME[stage]).stage_key(p, None, {"audio_master": [{"sha256": "x", "path": "p", "kind": "k", "bytes": 1, "meta": {}}]})

    def test_same_path_different_content_changes_audio_key_not_tts_or_unrelated_stages(self):
        f = wav(self.root / "wm.wav", 1)
        base = {"watermark": str(f), "tts": {"voice": "v"}}
        k1 = {s: self.key(s, base) for s in ("tts", "audio", "render_youtube")}
        wav(self.root / "wm.wav", 9)                                                                    # CÙNG đường dẫn, nội dung khác
        k2 = {s: self.key(s, base) for s in ("tts", "audio", "render_youtube")}
        self.assertNotEqual(k1["audio"], k2["audio"])
        self.assertEqual((k1["tts"], k1["render_youtube"]), (k2["tts"], k2["render_youtube"]))           # narration TTS / render không chạy lại
        self.assertEqual(self.key("audio", base), self.key("audio", base))                               # ổn định

    def test_key_unchanged_for_jobs_without_a_watermark_file(self):
        self.assertEqual(self.key("audio", {"tts": {}}), self.key("audio", {"tts": {}, "watermark": None}))
        self.assertEqual(self.key("audio", {"watermark": "w.wav"}), self.key("audio", {"watermark": "w.wav"}))   # đường dẫn không phải file: như cũ


def Orchestrator_like(root):
    from contentfactory.orchestrator.runner import Orchestrator
    return Orchestrator(load_config(root))
