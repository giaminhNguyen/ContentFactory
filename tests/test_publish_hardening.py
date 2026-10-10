"""Chặn lỗi YouTube metadata TỪ SỚM (TITLE_TOO_LONG, DESCRIPTION_TOO_LONG, INVALID_METADATA, MISSING_MADE_FOR_KIDS, AMBIGUOUS_UPLOAD).

Mọi test dùng đầu vào cố tình sai và chứng minh hệ thống chặn/sửa đúng THỜI ĐIỂM: lúc tạo job (không tốn stage nào), ở Metadata Builder, hoặc ngay trước
khi chạm uploader; không bao giờ gọi YouTube/LLM/mạng thật (daemon giả: tests/fake_yt_uploader.py).
Nguồn giới hạn: modules/yt_uploader/internal/upload/validate.go (title 100 rune, description 5000 byte, tags 500, privacy private|unlisted|public)."""
import json
import unittest
from pathlib import Path

from contentfactory.contracts import ErrorClass, StageError
from contentfactory.orchestrator import ops
from contentfactory.orchestrator.service import Service
from contentfactory.orchestrator.story_router import title_budget
from contentfactory.output import metadata as MD
from contentfactory.publish import stage as publish_stage
from contentfactory.publish.yt_uploader import map_job_error
from tests.support import RootCase, params
from tests.test_automode import write_channel
from tests.test_publishing import UploaderCase

P = {"id": "j1", "title": "Tôi Trùng Sinh", "title_source": "user", "channel_id": "kenh", "language": "vi"}
URL = "https://www.youtube.com/watch?v=abcdefghijk"


def channel(**kw) -> dict:
    return MD.normalize_channel({"name": "Kênh Truyện", **kw}, "kenh")


class StubCtx:
    """Đủ cho publish.stage.run tới điểm gọi adapter."""
    def __init__(self, pm: dict, params_: dict | None = None, chp: dict | None = None, dfl: dict | None = None):
        self.params, self._pm = params_ or {}, pm
        self.config = {"channel_config": {"publishing": chp or {}}, "publishing": {"defaults": dfl or {}}}
        self.logs, self.stage_key, self.job_id = [], "key", "job"

    def read_json(self, kind):
        return self._pm

    def log(self, *a, **k):
        self.logs.append((a, k))

    def one(self, kind):
        return Path("x.mp4")


class SpyPublish:
    platform = "youtube"

    def __init__(self):
        self.reqs = []

    def publish(self, req, ctx):
        self.reqs.append(req)
        return {"state": "failed", "error": None}                                  # dừng ngay sau khi ghi lại request (không cần sequence/ghi file)


def run_stage(pm, **kw) -> tuple[SpyPublish, StageError | None]:
    spy, ctx = SpyPublish(), StubCtx(pm, **kw)
    try:
        publish_stage.run(ctx, spy, None)
    except StageError as e:
        return spy, e
    raise AssertionError("stage phải dừng")


GOOD_PM = {"youtube_title": "[Full Audio] | Truyện Ma", "description": "Mô tả", "sequence": 1, "channel_id": "kenh"}


# =============================================================================== 1. TITLE_TOO_LONG
class TitleTooLongTest(RootCase):
    def test_limit_is_checked_before_upload_and_never_truncates(self):
        for n, ok in ((100, True), (101, False)):
            title = "Ă" * (n - len("[Full Audio][Kênh Truyện số 1] | "))
            proj = {**P, "title": title}
            if ok:
                self.assertEqual(len(MD.build(proj, channel(), 1)["youtube_title"]), 100)
            else:
                with self.assertRaises(StageError) as e:
                    MD.build(proj, channel(), 1)
                self.assertEqual((e.exception.error_class, e.exception.code), (ErrorClass.POLICY, "TITLE_TOO_LONG"))
                self.assertEqual(proj["title"], title)                              # KHÔNG cắt, KHÔNG đổi project.title
        spy, e = run_stage({**GOOD_PM, "youtube_title": "T" * 101})                 # metadata tay chỉnh/import lọt qua Builder: chặn lại ngay trước uploader
        self.assertEqual(e.code, "TITLE_TOO_LONG")
        self.assertEqual(spy.reqs, [])                                              # daemon/YouTube không bị gọi

    def test_angle_brackets_are_rejected_not_silently_changed(self):
        with self.assertRaises(StageError) as e:
            MD.build({**P, "title": "A <b> B"}, channel(), 1)
        self.assertEqual(e.exception.code, "INVALID_METADATA")

    def test_title_over_target_is_warned_and_ai_title_budget_aims_at_90(self):
        pad = len("[Full Audio][Kênh Truyện số 1] | ")
        m = MD.build({**P, "title": "x" * (95 - pad)}, channel(), 1)
        self.assertTrue(any("mục tiêu" in w for w in m["warnings"]))
        self.assertEqual(MD.build(P, channel(), 1)["warnings"], [])
        prefix, budget = title_budget({"name": "Kênh Truyện", "id": "kenh"}, "kenh")
        self.assertEqual(budget, MD.TITLE_TARGET_CHARS - len(prefix))
        self.assertEqual(MD.TITLE_TARGET_CHARS, 90)

    def test_user_title_too_long_is_rejected_at_job_creation_before_any_stage(self):
        write_channel(self.root, "kenh_a", {"name": "Kênh Truyện A", "sequence": {"last_used": 26}, "publishing": {"made_for_kids": False}})
        orc = self.orc()
        with self.assertRaises(StageError) as e:
            orc.submit(params(channel="kenh_a", project={"title": "T" * 90}), auto_resume=False)
        self.assertEqual(e.exception.code, "TITLE_TOO_LONG")
        self.assertEqual(orc.store.list_jobs(), [])                                 # không có job, không stage nào chạy/tốn chi phí
        orc.submit(params(channel="kenh_a", project={"title": "T" * 90}), mode="STORY_ONLY", auto_resume=False)   # job không tới output: không bị chặn oan
        orc.submit(params(channel="kenh_a", project={"title": "T" * 90, "title_source": "auto"}), auto_resume=False)   # tên tự đặt có thể được thay bằng tên AI: để Builder kiểm
        self.assertEqual(len(orc.store.list_jobs()), 2)


# =============================================================================== 2. DESCRIPTION_TOO_LONG
class DescriptionTooLongTest(unittest.TestCase):
    def test_limit_counts_utf8_bytes_not_characters(self):
        viet = "ế" * 1700                                                          # 1700 ký tự nhưng 5100 byte (3 byte/ký tự)
        self.assertLess(len(viet), MD.DESCRIPTION_MAX_BYTES)
        self.assertEqual(MD.utf8_len(viet), 5100)
        with self.assertRaises(StageError) as e:
            MD.build(P, channel(description_template=viet), 1)
        self.assertEqual((e.exception.error_class, e.exception.code), (ErrorClass.POLICY, "DESCRIPTION_TOO_LONG"))
        self.assertEqual((e.exception.detail["bytes"], e.exception.detail["chars"]), (5100, 1700))
        exact = "ế" * 1666 + "ab"                                                  # đúng 5000 byte
        self.assertEqual(MD.utf8_len(exact), 5000)
        self.assertEqual(MD.build(P, channel(description_template=exact), 1)["description"], exact)
        for tpl in ("a" * 5000, "ế" * 1667):                                       # 5000 ASCII ok; 5001 byte (1667*3) vượt
            if tpl == "a" * 5000:
                MD.check_description(tpl)
            else:
                with self.assertRaises(StageError):
                    MD.check_description(tpl)

    def test_description_over_target_is_warned_and_rechecked_right_before_upload(self):
        m = MD.build(P, channel(description_template="ế" * 1550), 1)               # 4650 byte: hợp lệ nhưng vượt mục tiêu 4500
        self.assertTrue(any("mục tiêu" in w for w in m["warnings"]))
        spy, e = run_stage({**GOOD_PM, "description": "ế" * 1700})
        self.assertEqual(e.code, "DESCRIPTION_TOO_LONG")
        self.assertEqual(spy.reqs, [])

    def test_invalid_utf8_text_stops_safely(self):
        with self.assertRaises(StageError) as e:
            MD.check_description("abc\ud800")                                       # surrogate lẻ: không mã hóa được, không được crash/đẩy sang uploader
        self.assertEqual(e.exception.code, "INVALID_METADATA")
        with self.assertRaises(StageError):
            MD.check_description("a<script>")


# =============================================================================== 3. INVALID_METADATA
class InvalidMetadataTest(RootCase):
    def test_unfixable_values_stop_before_the_uploader(self):
        bad = [{"tags": "abc"}, {"tags": ["ok", 5]}, {"privacy": "secret"}, {"privacy": 1}, {"category": "Entertainment"}, {"category": 22},
               {"playlists": "PL1"}, {"playlists": [1]}, {"account_id": ""}]
        for b in bad:
            spy, e = run_stage(GOOD_PM, params_={"made_for_kids": False, **b})
            self.assertEqual((e.error_class, e.code), (ErrorClass.POLICY, "INVALID_METADATA"), b)
            self.assertEqual(spy.reqs, [], b)                                       # sai cấu trúc không bao giờ được chuyển sang uploader

    def test_formatting_problems_are_fixed_deterministically(self):
        spy, e = run_stage(GOOD_PM, params_={"made_for_kids": True, "privacy": " Unlisted ", "category": " 24 ", "playlists": ["PL1", " PL1", ""],
                                             "tags": ["  truyện   ma ", "Truyện Ma", "", "audio"]})
        self.assertEqual(e.code, "PUBLISH_NOT_COMPLETED")                           # tới được adapter
        [req] = spy.reqs
        self.assertEqual((req["tags"], req["privacy"], req["category"], req["playlists"], req["made_for_kids"]),
                         (["truyện ma", "audio"], "unlisted", "24", ["PL1"], True))
        self.assertEqual((req["title"], req["description"]), (GOOD_PM["youtube_title"], "Mô tả"))

    def test_tags_over_500_drop_whole_trailing_tags_never_cut_one(self):
        tags = [f"tag{i:02d}" + "x" * 44 for i in range(15)]                        # mỗi tag 50 byte => 750
        out, fixes = MD.clean_publishing({"tags": tags, "made_for_kids": False})
        self.assertEqual(out["tags"], tags[:10])
        self.assertLessEqual(MD.tags_cost(out["tags"]), 500)
        self.assertTrue(fixes)
        out, _ = MD.clean_publishing({"tags": ["a" * 600, "ok"], "made_for_kids": False})        # một tag quá dài bị bỏ nguyên, không cắt
        self.assertEqual(out["tags"], ["ok"])
        self.assertEqual(MD.tags_cost(["hai từ"]), len("hai từ".encode()) + 2)       # tag có dấu cách tốn thêm 2 (như daemon); byte UTF-8, không phải ký tự

    def test_bad_values_in_channel_or_params_are_rejected_at_job_creation(self):
        with self.assertRaises(StageError) as e:
            MD.normalize_channel({"publishing": {"tags": ["a", 1]}}, "k")
        self.assertEqual(e.exception.code, "INVALID_CHANNEL_CONFIG")
        with self.assertRaises(StageError):
            MD.normalize_channel({"publishing": {"category": 22}}, "k")
        write_channel(self.root, "kenh_a", {"name": "A", "publishing": {"made_for_kids": False}})
        orc = self.orc()
        for bad in ({"privacy": "secret"}, {"tags": "abc"}, {"category": "Entertainment"}):
            with self.assertRaises(StageError) as e:
                orc.submit(params(channel="kenh_a", **bad), auto_resume=False)
            self.assertEqual(e.exception.code, "INVALID_METADATA", bad)
        self.assertEqual(orc.store.list_jobs(), [])


# =============================================================================== 4. MISSING_MADE_FOR_KIDS
class MadeForKidsTest(RootCase):
    def setUp(self):
        super().setUp()
        write_channel(self.root, "chua_khai", {"name": "Chưa khai"})
        write_channel(self.root, "da_khai", {"name": "Đã khai", "publishing": {"made_for_kids": False}})
        self.orc_ = self.orc()
        self.svc = Service(self.orc_)

    def no_job(self):
        self.assertEqual(self.orc_.store.list_jobs(), [])                           # không job nào => không stage nào tốn Story/TTS/render

    def test_submit_rejects_missing_none_and_non_bool_before_any_stage(self):
        for label, over in (("thiếu", {}), ("None", {"made_for_kids": None}), ('"true"', {"made_for_kids": "true"}), ('"false"', {"made_for_kids": "false"}),
                            ("0", {"made_for_kids": 0}), ("1", {"made_for_kids": 1})):
            p = params(channel="chua_khai")
            p.pop("made_for_kids")
            p.update(over)
            with self.assertRaises(StageError, msg=label) as e:
                self.orc_.submit(p, auto_resume=False)
            self.assertEqual(e.exception.code, "MISSING_MADE_FOR_KIDS", label)
        p = params(channel="da_khai", made_for_kids="false")                       # kênh đã khai bool nhưng job ghi đè bằng chuỗi: cũng bị chặn (params thắng kênh)
        with self.assertRaises(StageError) as e:
            self.orc_.submit(p, auto_resume=False)
        self.assertEqual(e.exception.code, "MISSING_MADE_FOR_KIDS")
        self.no_job()

    def test_valid_declarations_and_jobs_that_never_publish_are_accepted(self):
        p = params(channel="da_khai")
        p.pop("made_for_kids")
        self.orc_.submit(p, auto_resume=False)                                      # kênh đã khai bool
        self.orc_.submit(params(channel="chua_khai", made_for_kids=True), auto_resume=False)
        q = params(channel="chua_khai")
        q.pop("made_for_kids")
        self.orc_.submit(q, mode="STORY_ONLY", auto_resume=False)                   # không tới publish: không cần khai
        self.assertEqual(len(self.orc_.store.list_jobs()), 3)

    def test_ui_cli_and_channel_creation_paths_do_not_coerce_types(self):
        for bad in ("false", "true", 0, 1):
            with self.assertRaises(StageError, msg=repr(bad)) as e:
                self.svc.create_run({"input": {"value": URL}, "channel": "chua_khai", "run": "full", "kids": bad})
            self.assertEqual(e.exception.code, "MISSING_MADE_FOR_KIDS")
        with self.assertRaises(StageError):
            self.svc.create_run({"input": {"value": URL}, "channel": "chua_khai", "run": "full"})            # không khai: không đoán
        with self.assertRaises(StageError):
            ops.go(self.orc_, URL, "chua_khai", kids="no", echo=lambda *_: None)
        for bad in (None, "false", 0):
            with self.assertRaises(StageError):
                self.svc.create_channel("kenh_moi", "K", bad)
            with self.assertRaises(StageError):
                ops.channel_init(self.orc_.cfg, "kenh_moi", "K", kids=bad)
        self.no_job()

    def test_publish_stage_and_adapter_still_refuse_without_a_real_bool(self):
        for kids in (None, "true", 1):
            spy, e = run_stage(GOOD_PM, params_={"made_for_kids": kids} if kids is not None else {})
            self.assertEqual(e.code, "MISSING_MADE_FOR_KIDS", kids)
            self.assertEqual(spy.reqs, [])


# =============================================================================== 5. AMBIGUOUS_UPLOAD
class AmbiguousUploadTest(UploaderCase):
    def test_ambiguous_is_never_auto_retried_and_never_creates_a_second_video(self):
        self.srv.mode = "fail:AMBIGUOUS_UPLOAD"
        for _ in range(3):                                                          # lần đầu + hai lần chạy lại (mô phỏng retry tự động của hệ thống)
            with self.assertRaises(StageError) as e:
                self.up().publish(self.req(), self.ctx)
            self.assertEqual((e.exception.error_class, e.exception.code), (ErrorClass.AMBIGUOUS, "AMBIGUOUS_UPLOAD"))
        self.assertEqual((self.srv.creates(), self.srv.count("POST", "retry"), len(self.srv.jobs)), (1, 0, 1))

    def test_ambiguous_class_wins_even_when_the_error_code_looks_retryable(self):
        job = {"id": "j", "state": "failed", "error_class": "AMBIGUOUS_PUBLISH", "last_error": {"code": "network_error", "message": "mất kết nối giữa upload"}}
        self.assertEqual(map_job_error(job).error_class, ErrorClass.AMBIGUOUS)
        self.srv.mode = "fail:AMBIGUOUS_UPLOAD"
        with self.assertRaises(StageError):
            self.up().publish(self.req(), self.ctx)
        [j] = self.srv.jobs.values()
        j["last_error"]["code"] = "network_error"                                   # daemon báo class AMBIGUOUS_PUBLISH nhưng mã trông "tạm thời"
        with self.assertRaises(StageError) as e:
            self.up().publish(self.req(), self.ctx)
        self.assertEqual(e.exception.error_class, ErrorClass.AMBIGUOUS)
        self.assertEqual(self.srv.count("POST", "retry"), 0)

    def test_same_key_after_lost_connection_finds_the_job_instead_of_creating_one(self):
        self.srv.mode = "slow"
        import threading
        threading.Timer(0.3, self.ctx.cancel.set).start()
        with self.assertRaises(StageError):
            self.up().publish(self.req(), self.ctx)                                 # mất kết nối/shutdown giữa upload
        from contentfactory.contracts import CancelToken
        self.ctx.cancel = CancelToken()
        self.srv.release()
        self.assertEqual(self.up().publish(self.req(), self.ctx)["state"], "completed")
        self.assertEqual((self.srv.creates(), len(self.srv.jobs)), (1, 1))

    def test_adapter_never_coerces_made_for_kids(self):
        with self.assertRaises(StageError) as e:
            self.up().publish(self.req(made_for_kids="false"), self.ctx)
        self.assertEqual(e.exception.code, "MISSING_MADE_FOR_KIDS")
        self.assertEqual(self.srv.creates(), 0)


if __name__ == "__main__":
    unittest.main()
