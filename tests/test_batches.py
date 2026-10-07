"""Agent Plan Phase 4: nhận dạng + khám phá kênh/playlist YouTube (chỉ metadata), chọn video, Channel Run (batch) với job con độc lập, dedupe/idempotency, hành động hàng loạt, restart."""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from contentfactory.contracts import StageError
from contentfactory.jobs import pipeline as P
from contentfactory.jobs.db import JobStore
from contentfactory.orchestrator.config import load_config
from contentfactory.orchestrator.runner import Orchestrator
from contentfactory.orchestrator.service import Service
from contentfactory.source import discovery as D
from contentfactory.source.youtube import YtDlp
from tests.support import RootCase, params, wait_until

REPO = Path(__file__).resolve().parents[1]
CHAN = "UC" + "a" * 22


def vid(n: int) -> str:
    return f"v{n:010d}"                                                                              # đúng 11 ký tự như id YouTube


def entry(n: int, **kw) -> dict:
    e = {"id": vid(n), "title": f"Video {n}", "duration": 600 + n, "upload_date": f"2026{(n % 12) + 1:02d}{(n % 27) + 1:02d}", "live_status": "not_live", "availability": "public"}
    e.update(kw)
    return e


class FakeYouTube:
    """Giả lập yt-dlp (chỉ liệt kê): trả dữ liệu theo URL tab; ghi lại mọi lời gọi để kiểm 'không tải media', 'có trần quét'."""

    def __init__(self, videos=None, shorts=None, streams=None, playlist=None, title="Truyện ABC", detail=None) -> None:
        self.videos, self.shorts, self.streams, self.playlist = videos or [], shorts or [], streams or [], playlist or []
        self.title, self.calls, self.detail_data = title, [], detail or {}

    def meta(self, entries):
        return {"_type": "playlist", "id": CHAN, "title": self.title, "channel": self.title, "channel_id": CHAN, "uploader_id": "@abc", "entries": entries}

    def list(self, url: str, end: int | None = None) -> dict:
        self.calls.append((url, end))
        if "/shorts" in url:
            e = self.shorts
        elif "/streams" in url:
            e = self.streams
        elif "playlist?list=" in url:
            return {**self.meta(self.playlist[:end] if end else self.playlist), "title": "Danh sách phát A"}
        else:
            e = self.videos
        return self.meta(e[:end] if end else e)

    def info(self, url: str) -> dict:
        return self.detail_data.get(url.rsplit("=", 1)[-1], {})


def discovery(fake: FakeYouTube, **kw) -> D.Discovery:
    return D.Discovery(fake.list, fake.info, **{"max_scan": 300, "confirm_above": 100, "hard_max": 500, **kw})


class ClassifyTest(unittest.TestCase):
    def test_video_forms(self):
        for u in ("https://www.youtube.com/watch?v=abcdefghijk", "https://youtu.be/abcdefghijk", "https://m.youtube.com/watch?v=abcdefghijk&list=PL123456789012",
                  "https://www.youtube.com/shorts/abcdefghijk", "https://www.youtube.com/embed/abcdefghijk", "https://www.youtube.com/live/abcdefghijk"):
            c = D.classify(u)
            self.assertEqual((c["kind"], c["id"], c["canonical_url"]), ("video", "abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk"), u)

    def test_playlist_and_channel_forms(self):
        c = D.classify("https://www.youtube.com/playlist?list=PLabcdefghijkl")
        self.assertEqual((c["kind"], c["id"]), ("playlist", "PLabcdefghijkl"))
        for u in ("https://www.youtube.com/@truyenabc", "https://www.youtube.com/@truyenabc/videos", "@truyenabc"):
            c = D.classify(u)
            self.assertEqual((c["kind"], c["id"], c["canonical_url"]), ("channel", "@truyenabc", "https://www.youtube.com/@truyenabc"), u)
        self.assertEqual(D.classify(f"https://www.youtube.com/channel/{CHAN}/streams")["canonical_url"], f"https://www.youtube.com/channel/{CHAN}")
        self.assertEqual(D.classify("https://www.youtube.com/c/TruyenABC")["kind"], "channel")
        self.assertEqual(D.classify("https://www.youtube.com/user/abc")["kind"], "channel")

    def test_rejects_other_hosts_and_garbage(self):
        for bad in ("https://vimeo.com/123", "https://evil.example/@abc", "javascript:alert(1)", "abc", "", "https://www.youtube.com/", "https://www.youtube.com/watch?v=short",
                    "https://www.youtube.com/playlist", "https://www.youtube.com/channel/UCshort"):
            with self.assertRaises(StageError, msg=bad):
                D.classify(bad)

    def test_canonical_links_are_built_from_validated_ids_only(self):
        self.assertIsNone(D.channel_url(channel_id="bad"))
        self.assertIsNone(D.channel_url(handle="@a b"))
        self.assertEqual(D.channel_url(handle="@ok.name"), "https://www.youtube.com/@ok.name")
        self.assertIsNone(D.channel_url(path="/c/../../x"))


class DiscoveryTest(unittest.TestCase):
    def fake(self, n=30, **kw) -> FakeYouTube:
        return FakeYouTube(videos=[entry(i) for i in range(n, 0, -1)], **kw)                       # mới nhất trước

    def test_inspect_resolves_channel_identity_but_never_downloads(self):
        f = self.fake()
        info = discovery(f).inspect("https://www.youtube.com/@abc")
        self.assertEqual((info["kind"], info["channel_id"], info["canonical_url"], info["title"]), ("channel", CHAN, f"https://www.youtube.com/channel/{CHAN}", "Truyện ABC"))
        self.assertEqual(f.calls, [("https://www.youtube.com/@abc/videos", 1)])                    # một lần liệt kê, 1 mục: metadata nhẹ
        calls = len(f.calls)
        v = discovery(f).inspect("https://youtu.be/abcdefghijk")
        self.assertEqual((v["kind"], len(f.calls)), ("video", calls))                              # video: nhận dạng không cần mạng

    def test_default_selection_is_ten_newest_unprocessed_with_reasons(self):
        f = FakeYouTube(videos=[entry(30, live_status="is_upcoming"), entry(29), entry(28, live_status="was_live"), entry(27), entry(26), entry(25, availability="private")]
                        + [entry(i) for i in range(24, 0, -1)])
        done = {vid(27), vid(26), vid(24)}
        r = discovery(f).discover("@abc", None, None, lambda v: "000007" if v in done else None)
        sel = [e["video_id"] for e in r["entries"] if e["selected"]]
        self.assertEqual(len(sel), 10)
        self.assertEqual(sel[:3], [vid(29), vid(23), vid(22)])                                        # bỏ upcoming/livestream/đã xử lý, vẫn lấy đủ 10
        self.assertEqual(r["skipped"], {"upcoming": 1, "livestream": 1, "processed": 3, "unavailable": 1})
        reasons = {e["video_id"]: e["skip_reason"] for e in r["entries"]}
        self.assertEqual((reasons[vid(30)], reasons[vid(28)], reasons[vid(27)], reasons[vid(25)]), ("upcoming", "livestream", "processed", "unavailable"))
        self.assertEqual(next(e for e in r["entries"] if e["video_id"] == vid(27))["processed_job"], "000007")
        self.assertFalse(r["requires_confirmation"])

    def test_filters_can_be_turned_off_and_shorts_included(self):
        f = FakeYouTube(videos=[entry(3), entry(2, live_status="is_upcoming")], shorts=[entry(9, upload_date="20261230")], streams=[entry(8, live_status="was_live")])
        r = discovery(f).discover("@abc", {"mode": "newest", "n": 10}, {"skip_upcoming": False, "include_shorts": True, "skip_live": False, "skip_processed": False})
        self.assertEqual({e["video_id"] for e in r["entries"] if e["selected"]}, {vid(3), vid(2), vid(9), vid(8)})
        base = discovery(f).discover("@abc")
        self.assertEqual({e["video_id"] for e in base["entries"]}, {vid(3), vid(2)})                  # mặc định: không quét Shorts/Live
        self.assertEqual([c[0].rsplit("/", 1)[-1] for c in f.calls[-1:]], ["videos"])

    def test_selection_modes(self):
        d = discovery(self.fake(30))
        ids = lambda r: [e["video_id"] for e in r["entries"] if e["selected"]]
        self.assertEqual(ids(d.discover("@abc", {"mode": "newest", "n": 3})), [vid(30), vid(29), vid(28)])
        self.assertEqual(ids(d.discover("@abc", {"mode": "oldest", "n": 2})), [vid(2), vid(1)])
        self.assertEqual(ids(d.discover("@abc", {"mode": "range", "from": 3, "to": 5})), [vid(28), vid(27), vid(26)])
        self.assertEqual(ids(d.discover("@abc", {"mode": "manual", "ids": [vid(5), vid(9)]})), [vid(9), vid(5)])
        by_date = d.discover("@abc", {"mode": "dates", "date_from": "2026-03-01", "date_to": "2026-03-31"})
        self.assertTrue(by_date["selected"] and all(e["published"].startswith("2026-03") for e in by_date["entries"] if e["selected"]))
        for bad in ({"mode": "newest", "n": 0}, {"mode": "range", "from": 5, "to": 2}, {"mode": "dates"}, {"mode": "manual", "ids": ["x"]}, {"mode": "random"}):
            with self.assertRaises(StageError, msg=str(bad)):
                d.discover("@abc", bad)

    def test_dates_fetches_detail_only_when_the_flat_list_has_no_dates(self):
        f = FakeYouTube(videos=[{"id": vid(2), "title": "A"}, {"id": vid(1), "title": "B"}], detail={vid(2): {"upload_date": "20260301"}, vid(1): {"upload_date": "20250101"}})
        r = discovery(f).discover("@abc", {"mode": "dates", "date_from": "2026-01-01"})
        self.assertEqual([e["video_id"] for e in r["entries"] if e["selected"]], [vid(2)])
        no_detail = D.Discovery(f.list, None).discover("@abc", {"mode": "dates", "date_from": "2026-01-01"})
        self.assertEqual(no_detail["selected"], 0)
        self.assertTrue(no_detail["warnings"])

    def test_scan_cap_confirmation_and_odd_inputs(self):
        big = FakeYouTube(videos=[entry(i % 99 + 1, id=f"b{i:010d}") for i in range(400)])
        r = discovery(big, max_scan=250).discover("@abc", {"mode": "newest", "n": 150})
        self.assertEqual((r["total"], r["truncated"], r["requires_confirmation"]), (250, True, True))
        self.assertTrue(r["warnings"])
        self.assertEqual(big.calls[-1], ("https://www.youtube.com/@abc/videos", 251))              # trần quét gửi cho yt-dlp (không kéo cả kênh)
        odd = FakeYouTube(videos=[entry(1), entry(1), {"id": "tab:videos", "title": "tab"}, {"id": vid(2)}])
        o = discovery(odd).discover("@abc")
        self.assertEqual([e["video_id"] for e in o["entries"]], [vid(1), vid(2)])                  # trùng id + mục không phải video bị loại; thiếu title vẫn dùng được
        self.assertIsNone(o["entries"][1]["title"])
        empty = discovery(FakeYouTube()).discover("@abc")
        self.assertEqual((empty["total"], empty["selected"]), (0, 0))
        with self.assertRaises(StageError):
            discovery(FakeYouTube()).discover("https://youtu.be/abcdefghijk")                       # video không phải bộ sưu tập

    def test_playlist_listing(self):
        f = FakeYouTube(playlist=[entry(i) for i in range(5, 0, -1)])
        r = discovery(f).discover("https://www.youtube.com/playlist?list=PLabcdefghijkl", {"mode": "newest", "n": 2})
        self.assertEqual((r["source"]["kind"], r["source"]["title"], r["selected"]), ("playlist", "Danh sách phát A", 2))
        self.assertEqual(f.calls[-1][0], "https://www.youtube.com/playlist?list=PLabcdefghijkl")


class YtDlpListingTest(unittest.TestCase):
    """Lớp subprocess thật (yt-dlp giả): lỗi từng phần, tab không có, lỗi mạng."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cf-ytdlp-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.script = self.tmp / "fake_yt.py"
        self.script.write_text(
            "import os, sys, json\nmode = os.environ.get('FAKE_MODE', 'ok')\n"
            "if mode == 'partial':\n    print(json.dumps({'entries': [{'id': 'v0000000001'}]}))\n    sys.stderr.write('ERROR: [youtube] x: Video unavailable\\n')\n    sys.exit(1)\n"
            "if mode == 'notab':\n    sys.stderr.write('ERROR: This channel does not have a shorts tab\\n')\n    sys.exit(1)\n"
            "if mode == 'net':\n    sys.stderr.write('ERROR: unable to download webpage: getaddrinfo failed\\n')\n    sys.exit(1)\n"
            "print(json.dumps({'entries': [{'id': 'v0000000001'}], 'args': sys.argv[1:]}))\n", encoding="utf-8")
        self.yt = YtDlp([sys.executable, str(self.script)])

    def run_mode(self, mode: str, **kw):
        os.environ["FAKE_MODE"] = mode
        self.addCleanup(os.environ.pop, "FAKE_MODE", None)
        return self.yt.list_flat("https://www.youtube.com/@abc/videos", **kw)

    def test_flat_metadata_only_flags_and_end(self):
        d = self.run_mode("ok", end=5)
        a = d["args"]
        self.assertIn("--flat-playlist", a)
        self.assertIn("--playlist-end", a)
        self.assertEqual(a[a.index("--playlist-end") + 1], "5")
        self.assertNotIn("--no-playlist", a)
        self.assertFalse(any(x in a for x in ("--write-subs", "--write-auto-subs", "-f", "--format", "-x")))      # không tải media/phụ đề

    def test_partial_json_is_used_missing_tab_is_empty_network_is_an_error(self):
        self.assertEqual(self.run_mode("partial")["entries"][0]["id"], "v0000000001")
        self.assertEqual(self.run_mode("notab"), {"entries": []})
        with self.assertRaises(StageError) as cm:
            self.run_mode("net")
        self.assertEqual((cm.exception.code, cm.exception.resource), ("YTDLP_FAILED", "network"))


class BatchCase(RootCase):
    def setUp(self):
        super().setUp()
        self.orc_ = None

    def make(self, fake: FakeYouTube | None = None, **dkw) -> tuple[Orchestrator, "BatchService", FakeYouTube]:
        fake = fake or FakeYouTube(videos=[entry(i) for i in range(30, 0, -1)])
        orc = self.orc()
        bs = orc.batch_service()
        bs._discovery = discovery(fake, **dkw)
        return orc, bs, fake

    def payload(self, **kw) -> dict:
        return {"url": "@abc", "output_channel": "default", "run": "story", "request_id": "r-1", **kw}


class CreateBatchTest(BatchCase):
    def test_creates_one_batch_with_independent_enqueued_children(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload())
        self.assertEqual((d["counts"]["total"], d["counts"]["queued"], d["status"], d["deduped"]), (10, 10, "QUEUED", False))
        self.assertEqual(d["source"]["channel_id"], CHAN)
        jobs = orc.store.batch_job_index(d["id"])
        self.assertEqual(len(jobs), 10)
        self.assertTrue(all(j["state"] == P.NEW and not orc.store.stage_runs(j["id"]) for j in jobs))      # chỉ enqueue: batch không chạy gì
        full = orc.store.get_job(jobs[0]["id"])
        self.assertEqual((full["batch_id"], full["source_key"], full["channel_id"]), (d["id"], f"youtube:{vid(30)}", "default"))
        self.assertEqual(full["params"]["source"]["video_url"], f"https://www.youtube.com/watch?v={vid(30)}")
        self.assertEqual(full["params"]["input"], {"kind": "youtube_url", "value": f"https://www.youtube.com/watch?v={vid(30)}"})
        self.assertEqual([j["source_key"] for j in jobs][:2], [f"youtube:{vid(30)}", f"youtube:{vid(29)}"])
        self.assertEqual(d["skipped"], {})

    def test_double_submit_is_idempotent_and_processed_videos_are_skipped_next_time(self):
        orc, bs, _ = self.make()
        a = bs.create(self.payload())
        b = bs.create(self.payload())
        self.assertEqual((b["id"], b["deduped"], len(orc.store.list_jobs())), (a["id"], True, 10))
        c2 = bs.create(self.payload(request_id="r-2", selection={"mode": "newest", "n": 10}))     # request mới, cùng lựa chọn: 10 video đã xử lý bị bỏ, lấy 10 video kế tiếp
        self.assertEqual((c2["counts"]["total"], c2["skipped"].get("processed")), (10, 10))
        self.assertEqual(len(orc.store.list_batches()), 2)
        keys = [j["source_key"] for j in orc.store.list_jobs()]
        self.assertEqual(len(keys), len(set(keys)), "mỗi video chỉ một job cho kênh này")
        self.assertEqual(len(keys), 20)

    def test_same_video_is_allowed_for_a_different_output_channel_and_rerun_policy(self):
        orc, bs, _ = self.make()
        bs.create(self.payload(selection={"mode": "newest", "n": 3}))
        other = bs.create(self.payload(request_id="r-x", output_channel="khac", selection={"mode": "newest", "n": 3}))
        self.assertEqual(other["counts"]["total"], 3)                               # cùng video nhưng kênh xuất bản khác => hợp lệ
        again = bs.create(self.payload(request_id="r-y", selection={"mode": "newest", "n": 3}, skip_policy="rerun"))
        self.assertEqual(again["counts"]["total"], 3)                               # rerun: tạo job mới có chủ đích
        self.assertEqual(len(orc.store.list_jobs()), 9)

    def test_nothing_to_run_and_safety_caps(self):
        orc, bs, _ = self.make(FakeYouTube(videos=[entry(1, live_status="is_live")]))
        with self.assertRaises(StageError) as cm:
            bs.create(self.payload())
        self.assertEqual(cm.exception.code, "NOTHING_TO_RUN")
        self.assertEqual((orc.store.list_batches(), orc.store.list_jobs()), ([], []))      # lỗi => không tạo batch nửa vời
        orc, bs, _ = self.make(FakeYouTube(videos=[entry(i % 99 + 1, id=f"c{i:010d}") for i in range(600)]), max_scan=600, hard_max=500)
        with self.assertRaises(StageError) as cm:
            bs.create(self.payload(selection={"mode": "newest", "n": 150}))
        self.assertEqual(cm.exception.code, "CONFIRM_LARGE_BATCH")
        with self.assertRaises(StageError) as cm:
            bs.create(self.payload(selection={"mode": "newest", "n": 550}, confirm_large=True))
        self.assertEqual(cm.exception.code, "BATCH_TOO_LARGE")
        self.assertEqual(orc.store.list_jobs(), [])
        ok = bs.create(self.payload(selection={"mode": "newest", "n": 120}, confirm_large=True))
        self.assertEqual(ok["counts"]["total"], 120)

    def test_invalid_child_spec_rejects_the_whole_batch_before_writing(self):
        orc, bs, _ = self.make()
        for bad in (self.payload(run="nope"), self.payload(run="full"), self.payload(pipeline={"mode": "custom", "requested_stages": ["bogus"]})):
            with self.assertRaises(StageError, msg=str(bad)):
                bs.create(bad)
        self.assertEqual((orc.store.list_batches(), orc.store.list_jobs()), ([], []))
        ok = bs.create(self.payload(run="full", kids=False))
        self.assertEqual(ok["counts"]["total"], 10)

    def test_custom_pipeline_children_store_their_own_spec(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(run=None, pipeline={"mode": "custom", "requested_stages": ["tts"]}, selection={"mode": "newest", "n": 2}))
        j = orc.store.get_job(orc.store.batch_job_index(d["id"])[0]["id"])
        self.assertEqual(j["pipeline"]["requested_stages"], ["tts"])

    def test_manual_video_ids_from_the_selection_screen(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(video_ids=[vid(7), vid(3)]))
        self.assertEqual([j["source_key"] for j in orc.store.batch_job_index(d["id"])], [f"youtube:{vid(7)}", f"youtube:{vid(3)}"])


class RunAndStatusTest(BatchCase):
    def test_children_run_independently_one_failure_does_not_stop_the_batch(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(selection={"mode": "newest", "n": 4}))
        jobs = orc.store.batch_job_index(d["id"])
        bad = jobs[1]["id"]
        job = orc.store.get_job(bad)
        orc.store.set_params(bad, {**job["params"], "fake": {"story": {"error_class": "POLICY", "fail_until_attempt": 99, "code": "BAD"}}}, "test")
        orc.run()
        det = bs.detail(d["id"])
        self.assertEqual((det["status"], det["counts"]["completed"], det["counts"]["failed"]), ("COMPLETED_WITH_ERRORS", 3, 1))
        self.assertEqual(orc.store.get_job(bad)["failed_stage"], "story")
        self.assertTrue(all(orc.store.get_job(j["id"])["state"] == P.STORY_READY for j in jobs if j["id"] != bad))     # target story: ba job còn lại xong
        r = bs.retry_failed(d["id"])                                                # chỉ job lỗi được retry; job khác không đổi
        self.assertEqual((r["retried"], r["skipped"]), (1, 3))
        self.assertEqual(orc.store.get_job(bad)["state"], P.SOURCE_READY)
        orc.store.set_params(bad, {**job["params"]}, "fix")
        orc.run()
        self.assertEqual(bs.detail(d["id"])["status"], "COMPLETED")

    def test_status_is_derived_and_filters_work(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(selection={"mode": "newest", "n": 3}))
        ids = [j["id"] for j in orc.store.batch_job_index(d["id"])]
        orc.pause_job(ids[0])
        self.assertEqual(bs.detail(d["id"])["counts"]["paused"], 1)
        self.assertEqual(bs.detail(d["id"])["status"], "QUEUED")
        for i in ids:
            orc.pause_job(i)
        self.assertEqual(bs.detail(d["id"])["status"], "PAUSED")
        det = bs.detail(d["id"], status="paused")
        self.assertEqual((len(det["items"]), det["total_items"]), (3, 3))
        self.assertEqual(len(bs.detail(d["id"], status="running")["items"]), 0)
        one = bs.detail(d["id"], limit=2, offset=2)
        self.assertEqual((len(one["items"]), one["has_more"]), (1, False))
        row = det["items"][0]
        self.assertEqual(row["links"]["source_video"], f"https://www.youtube.com/watch?v={vid(30)}")
        self.assertEqual(row["links"]["source_channel"], f"https://www.youtube.com/channel/{CHAN}")
        self.assertEqual(det["actions"]["resume"], True)

    def test_pause_all_resume_eligible_keeps_user_pauses_and_holds(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(selection={"mode": "newest", "n": 4}))
        ids = [j["id"] for j in orc.store.batch_job_index(d["id"])]
        orc.pause_job(ids[0], "USER")                                               # người dùng tự dừng một video
        p = bs.pause(d["id"])
        self.assertEqual((p["paused"], p["unchanged"]), (3, 1))
        self.assertEqual(bs.pause(d["id"])["paused"], 0)                            # idempotent
        self.assertEqual(orc.store.get_job(ids[0])["pause_origin"], "USER")
        self.assertEqual(orc.store.get_job(ids[1])["pause_origin"], "BATCH")
        r = bs.resume(d["id"])
        self.assertEqual((r["resumed"], r["kept_paused_by_user"]), (3, 1))
        self.assertEqual(orc.store.get_job(ids[0])["control_state"], "PAUSED")
        orc.run()
        self.assertEqual([orc.store.get_job(i)["state"] == P.STORY_READY for i in ids], [False, True, True, True])

    def test_cancel_queued_keeps_started_work(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(selection={"mode": "newest", "n": 4}, run="subtitle"))
        ids = [j["id"] for j in orc.store.batch_job_index(d["id"])]
        orc.pause_job(ids[2])
        # chạy riêng job đầu cho tới xong, các job khác chưa bắt đầu
        for i in ids[1:]:
            orc.pause_job(i)
        orc.run()
        r = bs.cancel_queued(d["id"])
        self.assertEqual((r["cancelled_jobs"], r["kept"]), (3, 1))
        self.assertEqual(sorted(bs.detail(d["id"])["counts"][k] for k in ("completed", "cancelled")), [1, 3])
        self.assertEqual(bs.detail(d["id"])["status"], "COMPLETED")

    def test_cancel_batch_and_derived_cancelled(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(selection={"mode": "newest", "n": 3}))
        r = bs.cancel(d["id"])
        self.assertEqual(r["cancelled_jobs"], 3)
        self.assertEqual(bs.detail(d["id"])["status"], "CANCELLED")
        with self.assertRaises(StageError):
            bs.pause(d["id"])
        orc.run()
        self.assertEqual(orc.store.nonterminal_count(), 0)

    def test_update_pipeline_scopes_and_partial_success(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(selection={"mode": "newest", "n": 4}, run="subtitle"))
        ids = [j["id"] for j in orc.store.batch_job_index(d["id"])]
        for i in ids[1:]:
            orc.pause_job(i)
        orc.run()                                                                   # job 0 xong (subtitle)
        r = bs.update_pipeline(d["id"], "story", "unstarted")
        self.assertEqual((r["counts"]["applied"], len(r["results"])), (3, 3))        # job đã chạy/xong không thuộc phạm vi 'unstarted'
        r = bs.update_pipeline(d["id"], "tts", "unfinished")
        self.assertEqual((r["counts"]["applied"], r["results"][0]["held"]), (4, True))   # job đã xong: lưu đích mới và giữ chờ “Chạy tiếp”, không tự chạy
        self.assertEqual(orc.store.get_job(ids[0])["pause_origin"], "EDIT")
        orc.resume(ids[0])
        orc.run()                                                                   # chỉ job 0 chạy tiếp (các job khác đang bị batch tạm dừng)
        sel = bs.update_pipeline(d["id"], "story", "selected", [ids[1], ids[0]])
        self.assertEqual((sel["counts"]["rejected"], sel["counts"]["applied"]), (1, 1))   # job 0 đã chạy tới tts: không lùi trước tiến độ
        self.assertEqual(sel["results"][0]["result"], "rejected")
        with self.assertRaises(StageError):
            bs.update_pipeline(d["id"], "bogus", "unfinished")
        with self.assertRaises(StageError):
            bs.update_pipeline(d["id"], "story", "everything")
        self.assertEqual(orc.store.get_job(ids[1])["target_stage"], "story")

    def test_rescan_adds_only_new_videos(self):
        fake = FakeYouTube(videos=[entry(i) for i in range(5, 0, -1)])
        orc, bs, _ = self.make(fake)
        d = bs.create(self.payload(selection={"mode": "newest", "n": 3}))
        self.assertEqual(d["counts"]["total"], 3)
        fake.videos = [entry(7), entry(6)] + fake.videos
        r = bs.rescan(d["id"])
        self.assertEqual(r["added"], 2)
        self.assertEqual(bs.rescan(d["id"])["added"], 0)
        self.assertEqual(len(orc.store.batch_job_index(d["id"])), 5)
        bs.pause(d["id"])
        fake.videos = [entry(8)] + fake.videos
        bs.rescan(d["id"])
        self.assertEqual(orc.store.get_job(orc.store.batch_job_index(d["id"])[-1]["id"])["control_state"], "PAUSED")     # job thêm vào batch đang tạm dừng cũng tạm dừng


class RecoveryAndConcurrencyTest(BatchCase):
    def test_crash_after_batch_row_before_jobs_is_completed_on_restart(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(selection={"mode": "newest", "n": 3}))
        for j in orc.store.batch_job_index(d["id"]):                                # mô phỏng crash: batch + item có, job chưa có
            orc.store.discard_job(j["id"])
        orc.store.set_batch_item(d["id"], vid(30), status="pending")
        orc.store.set_batch_item(d["id"], vid(29), status="pending")
        orc.store.set_batch_item(d["id"], vid(28), status="pending")
        orc2, bs2, _ = self.make()
        orc2.run()                                                                 # khởi động lại: hoàn tất việc tạo job rồi chạy
        det = bs2.detail(d["id"])
        self.assertEqual((det["counts"]["total"], det["counts"]["completed"], det["counts"]["pending_creation"]), (3, 3, 0))
        self.assertEqual(len(orc2.store.list_jobs()), 3)

    def test_crash_after_job_before_item_link_does_not_create_a_duplicate(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(selection={"mode": "newest", "n": 2}))
        existing = orc.store.batch_job_index(d["id"])[0]["id"]
        orc.store.set_batch_item(d["id"], vid(30), status="pending")                # job tồn tại nhưng item chưa nối
        orc.store.set_batch_item(d["id"], vid(30), job_id="", status="pending")
        self.assertEqual(bs.ensure_created(d["id"]), 1)
        self.assertEqual(len(orc.store.batch_job_index(d["id"])), 2)
        self.assertEqual(orc.store.batch_items(d["id"])[0]["job_id"], existing)

    def test_cancelled_pending_items_are_not_recreated(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(selection={"mode": "newest", "n": 2}))
        for j in orc.store.batch_job_index(d["id"]):
            orc.store.discard_job(j["id"])
        for it in orc.store.batch_items(d["id"]):
            orc.store.set_batch_item(d["id"], it["source_video_id"], status="pending")
        bs.cancel_queued(d["id"])
        self.assertEqual(bs.ensure_created(), 0)
        self.assertEqual(orc.store.batch_job_index(d["id"]), [])

    def test_concurrent_creates_with_the_same_request_make_one_batch(self):
        orc, bs, _ = self.make()
        out, errs = [], []

        def go():
            try:
                out.append(bs.create(self.payload(selection={"mode": "newest", "n": 5}))["id"])
            except Exception as e:                                                 # noqa: BLE001
                errs.append(repr(e))
        ts = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in ts]
        [t.join(60) for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(len(set(out)), 1)
        self.assertEqual(len(orc.store.list_batches()), 1)
        keys = [j["source_key"] for j in orc.store.list_jobs()]
        self.assertEqual((len(keys), len(set(keys))), (5, 5))

    def test_two_workers_cannot_claim_the_same_child(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(selection={"mode": "newest", "n": 6}, run="subtitle"))
        a, b = self.orc(), self.orc()
        got = []
        lock = threading.Lock()

        def claim(o):
            for c in o.store.claim(P.BY_NAME["source"], 6, 6, o.owner, 30):
                with lock:
                    got.append(c.job_id)
        ts = [threading.Thread(target=claim, args=(o,)) for o in (a, b)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        self.assertEqual((len(got), len(set(got))), (6, 6))
        self.assertEqual(len(orc.store.batch_job_index(d["id"])), 6)

    def test_batch_aggregate_stays_correct_under_concurrent_child_updates(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(selection={"mode": "newest", "n": 6}, run="subtitle"))
        t = threading.Thread(target=orc.run)
        t.start()
        for _ in range(20):
            c = bs.detail(d["id"])["counts"]
            self.assertEqual(sum(c[k] for k in ("running", "queued", "waiting", "paused", "attention", "failed", "completed", "cancelled")), 6)
        t.join(60)
        self.assertEqual(bs.detail(d["id"])["counts"]["completed"], 6)


class JobListTest(BatchCase):
    """Danh sách job cấp cao = job đơn + Channel Run; job con chỉ nằm trong chi tiết batch."""

    def test_children_are_not_top_level_rows_and_batch_counts_once(self):
        orc, bs, _ = self.make()
        svc = Service(orc)
        single = svc.create_run({"input": {"value": "https://youtu.be/abcdefghijk"}, "channel": "default", "run": "story"})["job_id"]
        d = bs.create(self.payload(selection={"mode": "newest", "n": 12}))
        lst = svc.list_jobs()
        self.assertEqual([(r["type"], r["id"]) for r in lst["jobs"]], [("batch", d["id"]), ("job", single)])
        self.assertEqual((lst["total"], lst["counts"]["all"], lst["counts"]["running"]), (2, 2, 2))
        row = lst["jobs"][0]
        self.assertEqual((row["status"], row["batch_status"], row["counts"]["total"], row["next_action"], row["output_channel"]["id"]), ("queued", "QUEUED", 12, "pause", "default"))
        self.assertEqual(row["source"]["channel_url"], f"https://www.youtube.com/channel/{CHAN}")
        self.assertTrue(all(j["batch_id"] is None for j in lst["jobs"] if j["type"] == "job"))

    def test_filters_use_the_same_groups_for_batches_and_jobs(self):
        orc, bs, _ = self.make()
        svc = Service(orc)
        d = bs.create(self.payload(selection={"mode": "newest", "n": 3}))
        bs.pause(d["id"])
        self.assertEqual(svc.list_jobs("waiting")["jobs"][0]["status"], "paused")             # tạm dừng nằm nhóm "đang chờ"
        self.assertEqual(svc.list_jobs("completed")["total"], 0)
        bs.resume(d["id"])
        j = orc.store.batch_job_index(d["id"])[0]["id"]
        job = orc.store.get_job(j)
        orc.store.set_params(j, {**job["params"], "fake": {"story": {"error_class": "POLICY", "fail_until_attempt": 99}}}, "t")
        orc.run()
        row = svc.list_jobs("attention")["jobs"][0]
        self.assertEqual((row["id"], row["batch_status"], row["status"]), (d["id"], "COMPLETED_WITH_ERRORS", "attention"))
        self.assertEqual(svc.list_jobs()["counts"]["attention"], 1)                          # huy hiệu "cần xử lý" tính batch một lần

    def test_list_does_not_query_per_child_and_version_tracks_batch_changes(self):
        orc, bs, _ = self.make()
        svc = Service(orc)
        bs.create(self.payload(selection={"mode": "newest", "n": 2}))
        calls = {"n": 0}
        orig = orc.store._q

        def counted(*a, **k):
            calls["n"] += 1
            return orig(*a, **k)
        orc.store._q = counted
        svc.list_jobs()
        small = calls["n"]
        bs.create(self.payload(request_id="r-big", selection={"mode": "newest", "n": 20}))
        calls["n"] = 0
        svc.list_jobs()
        two_batches = calls["n"]
        self.assertLessEqual(two_batches, small + 3, "số truy vấn không được tăng theo số job con")
        v1 = orc.store.jobs_version()
        orc.store.set_batch_control("B000001", "PAUSED")
        self.assertNotEqual(orc.store.jobs_version(), v1)
        self.assertEqual(svc.list_jobs(since=orc.store.jobs_version())["changed"], False)

    def test_pagination_mixes_rows_newest_first(self):
        orc, bs, _ = self.make()
        svc = Service(orc)
        for i in range(3):
            svc.create_run({"input": {"value": f"https://youtu.be/abcdefghij{i}"}, "channel": "default", "run": "story"})
        bs.create(self.payload(selection={"mode": "newest", "n": 2}))
        page = svc.list_jobs(limit=2, offset=0)
        self.assertEqual((len(page["jobs"]), page["has_more"], page["total"]), (2, True, 4))
        self.assertEqual(page["jobs"][0]["type"], "batch")
        self.assertEqual(len(svc.list_jobs(limit=2, offset=2)["jobs"]), 2)

    def test_child_detail_links_back_to_its_batch(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(selection={"mode": "newest", "n": 1}))
        det = Service(orc).job_detail(orc.store.batch_job_index(d["id"])[0]["id"])
        self.assertEqual((det["batch"]["id"], det["batch"]["position"], det["links"]["source_video_url"]), (d["id"], 1, f"https://www.youtube.com/watch?v={vid(30)}"))
        self.assertEqual(det["links"]["source_channel_url"], f"https://www.youtube.com/channel/{CHAN}")


class BulkActionsTest(BatchCase):
    def test_bulk_reports_every_job_and_validates_each(self):
        orc, bs, _ = self.make()
        svc = Service(orc)
        d = bs.create(self.payload(selection={"mode": "newest", "n": 4}, run="subtitle"))
        ids = [j["id"] for j in orc.store.batch_job_index(d["id"])]
        orc.pause_job(ids[3], "USER")
        orc.run()                                                                 # ids[0..2] xong; ids[3] vẫn tạm dừng
        r = svc.bulk("pause", ids[:2] + [ids[3]] + ["999999"])
        self.assertEqual(r["counts"], {"done": 0, "unchanged": 1, "skipped": 2, "error": 1})
        by = {x["job_id"]: x for x in r["results"]}
        self.assertEqual(by[ids[0]]["result"], "skipped")                       # đã hoàn tất: không có gì để tạm dừng, nói rõ lý do
        self.assertIn("hoàn tất", by[ids[0]]["reason"])
        self.assertEqual(by[ids[3]]["result"], "unchanged")
        self.assertEqual(by["999999"]["result"], "error")
        self.assertEqual(svc.bulk("resume", [ids[3]])["counts"]["done"], 1)
        self.assertEqual(orc.store.get_job(ids[3])["control_state"], "RUNNING")
        bad = svc.bulk("retry", [ids[0], ids[3]])
        self.assertEqual((bad["counts"]["done"], bad["counts"]["skipped"]), (0, 2))     # chỉ job lỗi mới retry được
        c = svc.bulk("cancel", [ids[3], ids[0]])
        self.assertEqual((c["counts"]["done"], c["counts"]["skipped"]), (1, 1))
        for bad_args in (("explode", ids), ("pause", []), ("pause", [str(i) for i in range(501)])):
            with self.assertRaises(StageError):
                svc.bulk(*bad_args)

    def test_bulk_delete_soft_deletes_and_is_idempotent(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(selection={"mode": "newest", "n": 2}, run="subtitle"))
        ids = [j["id"] for j in orc.store.batch_job_index(d["id"])]
        svc = Service(orc)
        r = svc.bulk("delete", ids)
        self.assertEqual(r["counts"]["done"], 2)
        self.assertTrue(all(orc.store.get_job(i)["control_state"] == "DELETED" for i in ids))
        self.assertEqual(svc.bulk("delete", ids)["counts"]["unchanged"], 2)            # xóa lặp lại là no-op

    def test_bulk_delete_channel_run_removes_children_and_batch(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(selection={"mode": "newest", "n": 3}, run="subtitle"))
        bid = d["id"]
        ids = [j["id"] for j in orc.store.batch_job_index(bid)]
        svc = Service(orc)
        r = svc.bulk("delete", [bid])
        self.assertEqual(r["counts"]["done"], 1)
        self.assertTrue(all(orc.store.get_job(i)["control_state"] == "DELETED" for i in ids))
        self.assertIsNone(orc.store.get_batch(bid))
        self.assertNotIn(bid, [b["id"] for b in orc.store.list_batches()])
        self.assertEqual(orc.store.pending_batch_items(), [])                           # không tạo lại job con cho batch đã xóa
        self.assertEqual(svc.bulk("delete", [bid])["counts"]["unchanged"], 1)           # xóa lặp lại là no-op

    def test_bulk_retry_only_touches_failed_jobs(self):
        orc, bs, _ = self.make()
        d = bs.create(self.payload(selection={"mode": "newest", "n": 2}, run="story"))
        ids = [j["id"] for j in orc.store.batch_job_index(d["id"])]
        job = orc.store.get_job(ids[0])
        orc.store.set_params(ids[0], {**job["params"], "fake": {"story": {"error_class": "POLICY", "fail_until_attempt": 99}}}, "t")
        orc.run()
        r = Service(orc).bulk("retry", ids)
        self.assertEqual({x["job_id"]: x["result"] for x in r["results"]}, {ids[0]: "done", ids[1]: "skipped"})
        self.assertEqual(orc.store.get_job(ids[0])["state"], P.SOURCE_READY)


class SourceLinksAndMigrationTest(BatchCase):
    def test_single_job_keeps_canonical_source_and_safe_links(self):
        orc, bs, _ = self.make()
        svc = Service(orc)
        r = svc.create_run({"input": {"value": "https://youtu.be/abcdefghijk?si=track"}, "channel": "default", "run": "story"})
        j = orc.store.get_job(r["job_id"])
        self.assertEqual((j["source_key"], j["channel_id"], j["batch_id"]), ("youtube:abcdefghijk", "default", None))
        self.assertEqual(j["params"]["source"]["video_url"], "https://www.youtube.com/watch?v=abcdefghijk")
        d = svc.job_detail(r["job_id"])
        self.assertEqual(d["links"]["source_video_url"], "https://www.youtube.com/watch?v=abcdefghijk")
        for bad in ("javascript:alert(1)", "http://www.youtube.com/watch?v=x", "https://evil.example/watch", "https://user:pw@www.youtube.com/x", None, 5):
            self.assertIsNone(Service._safe_yt(bad), bad)
        self.assertEqual(Service._safe_yt("https://www.youtube.com/watch?v=abcdefghijk"), "https://www.youtube.com/watch?v=abcdefghijk")

    def test_single_job_duplicate_detection_uses_the_same_source_key_as_batches(self):
        orc, bs, fake = self.make()
        Service(orc).create_run({"input": {"value": f"https://www.youtube.com/watch?v={vid(30)}"}, "channel": "default", "run": "story"})
        det = bs.discover({"url": "@abc", "output_channel": "default"})
        e = next(x for x in det["entries"] if x["video_id"] == vid(30))
        self.assertEqual(e["skip_reason"], "processed")
        self.assertIsNotNone(e["processed_job"])

    def test_detect_input_recognises_channel_and_playlist_and_create_run_refuses(self):
        orc, bs, _ = self.make()
        svc = Service(orc)
        d = svc.detect_input("https://www.youtube.com/@abc")
        self.assertEqual((d["ok"], d["kind"], d["collection"], [m["id"] for m in d["modes"]]), (True, "youtube_channel", True, ["full", "through_tts", "story", "subtitle"]))
        self.assertEqual(svc.detect_input("https://www.youtube.com/playlist?list=PLabcdefghijkl")["kind"], "youtube_playlist")
        self.assertEqual(svc.detect_input("https://www.youtube.com/watch?v=abcdefghijk")["kind"], "youtube_url")
        with self.assertRaises(StageError) as cm:
            svc.create_run({"input": {"value": "https://www.youtube.com/@abc"}, "channel": "default"})
        self.assertEqual(cm.exception.code, "USE_CHANNEL_RUN")
        self.assertEqual(len(orc.store.list_jobs()), 0)

    def test_v5_migration_on_a_v4_database(self):
        db = self.root / "v4.db"
        st = JobStore(db)
        jid = st.create_job({"channel": "kenh_x", "input": {"kind": "youtube_url", "value": "u"}})
        c = sqlite3.connect(db)
        c.execute("UPDATE jobs SET channel_id=NULL, batch_id=NULL")
        c.execute("PRAGMA user_version=4")
        c.commit()
        c.close()
        st2 = JobStore(db)
        j = st2.get_job(jid)
        self.assertEqual((st2.schema_version(), j["channel_id"], j["batch_id"]), (6, "kenh_x", None))        # job cũ: kênh lấy từ params, vẫn là Single Job
        self.assertEqual(st2.list_batches(), [])
        self.assertEqual(JobStore(db).schema_version(), 6)                                                  # idempotent


if __name__ == "__main__":
    unittest.main()
