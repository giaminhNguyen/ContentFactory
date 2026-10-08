"""Kho nhân vật (Phase 2): DB rỗng mặc định, hồ sơ có revision/khóa/lưu trữ, trùng lặp, audit, xuất/nhập Excel (dry-run, xung đột), đồng thời."""
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from contentfactory.contracts import StageError
from contentfactory.universe import Universe, UniverseDB
from contentfactory.universe import exchange as EX
from contentfactory.universe.xlsx import Sheet, XlsxError, read_workbook, write_workbook

LAN = {"display_name": "Lan Phương", "aliases": ["Phương"], "core_personality": "Điềm tĩnh, quan sát tinh tế, hay nghi ngờ lời hứa.", "motivations": ["Tìm lại em gái"],
       "flaws": ["Khó tin người"], "genre_affinities": ["trinh thám", "kinh dị"]}
HUNG = {"display_name": "Hùng Sói", "core_personality": "Nóng nảy, trọng nghĩa khí, nói ít làm nhiều.", "motivations": ["Trả ơn người cứu mình"], "flaws": ["Bốc đồng"],
        "genre_affinities": ["hành động"]}


def code(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except StageError as e:
        return e.code
    return None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = UniverseDB(Path(self.tmp.name) / "universe.db")
        self.addCleanup(self.db.close)
        self.u = Universe(self.db)


class StoreTest(Base):
    def test_fresh_install_is_empty_with_roles_only(self):
        s = self.u.summary()
        self.assertEqual((s["active"], s["archived"], s["staged_candidates"], s["stories"], s["revision"]), (0, 0, 0, 0, 0))   # LU-001: không có nhân vật nào được cấy sẵn
        self.assertEqual(self.u.list_characters()["total"], 0)
        self.assertIn("protagonist", self.u.role_codes())

    def test_create_assigns_stable_id_and_audits(self):
        c = self.u.create_character(LAN)
        self.assertRegex(c["character_id"], r"^ch_[0-9a-f]{12}$")
        self.assertEqual((c["revision"], c["origin"], c["status"], c["locked"]), (1, "user_created", "active", False))
        self.assertEqual(self.u.revision(), 1)
        a = self.u.audit()
        self.assertEqual((a[0]["action"], a[0]["after"]["display_name"]), ("create", "Lan Phương"))

    def test_validation_never_truncates_silently(self):
        for bad in ({"display_name": ""}, {"display_name": "x" * 81}, {**LAN, "motivations": "chuỗi"}, {**LAN, "flaws": [f"y{i}" for i in range(9)]}, {**LAN, "zzz": 1}, "str"):
            self.assertEqual(code(self.u.create_character, bad), "INVALID_CHARACTER", bad)
        self.assertEqual(self.u.list_characters()["total"], 0)

    def test_duplicates_by_name_diacritics_alias_and_personality(self):
        self.u.create_character(LAN)
        self.assertEqual(code(self.u.create_character, {**HUNG, "display_name": "lan phuong"}), "DUPLICATE_CHARACTER")        # bỏ dấu/hoa thường
        self.assertEqual(code(self.u.create_character, {**HUNG, "display_name": "Ai Đó", "aliases": ["PHƯƠNG"]}), "DUPLICATE_CHARACTER")
        clone = {**LAN, "display_name": "Mai Anh", "aliases": []}                                                            # tên khác nhưng tính cách y hệt
        self.assertEqual(code(self.u.create_character, clone), "DUPLICATE_CHARACTER")
        self.u.create_character(HUNG)                                                                                        # người khác hẳn thì được
        self.assertEqual(self.u.list_characters()["total"], 2)

    def test_optimistic_concurrency_rejects_stale_write_and_counts_conflict(self):
        c = self.u.create_character(LAN)
        v2 = self.u.update_character(c["character_id"], {"temperament": "Lạnh"}, 1)
        self.assertEqual(v2["revision"], 2)
        self.assertEqual(code(self.u.update_character, c["character_id"], {"temperament": "Nóng"}, 1), "REVISION_CONFLICT")
        self.assertEqual(self.u.character(c["character_id"])["temperament"], "Lạnh")                                         # không bị ghi đè
        self.assertEqual(self.u.summary()["write_conflicts"], 1)
        self.assertEqual(code(self.u.update_character, c["character_id"], {"temperament": "x"}, None), "INVALID_CHARACTER")

    def test_noop_update_keeps_revision(self):
        c = self.u.create_character(LAN)
        self.assertEqual(self.u.update_character(c["character_id"], {"temperament": ""}, 1)["revision"], 1)

    def test_lock_protects_core_identity_but_not_cues(self):
        c = self.u.create_character(LAN)
        c = self.u.set_lock(c["character_id"], True, 1)
        self.assertTrue(c["locked"])
        self.assertEqual(code(self.u.update_character, c["character_id"], {"core_personality": "Đổi hẳn"}, c["revision"]), "CHARACTER_LOCKED")   # LU-004
        ok = self.u.update_character(c["character_id"], {"visual_cues": "Áo khoác xám"}, c["revision"])
        self.assertEqual(ok["visual_cues"], "Áo khoác xám")
        c = self.u.set_lock(c["character_id"], False, ok["revision"])
        self.assertEqual(self.u.update_character(c["character_id"], {"core_personality": "Mới"}, c["revision"])["core_personality"], "Mới")

    def test_archive_hides_from_default_list_and_blocks_edits_until_restored(self):
        c = self.u.create_character(LAN)
        a = self.u.set_status(c["character_id"], "archived", 1)
        self.assertEqual(self.u.list_characters(status="")["items"][0]["status"], "archived")          # vẫn thấy, kèm badge
        self.assertEqual(code(self.u.update_character, c["character_id"], {"temperament": "x"}, a["revision"]), "CHARACTER_ARCHIVED")   # LU-011
        r = self.u.set_status(c["character_id"], "active", a["revision"])
        self.assertEqual(self.u.update_character(c["character_id"], {"temperament": "x"}, r["revision"])["temperament"], "x")
        self.assertEqual(self.u.list_characters(status="archived")["total"], 0)

    def test_search_filter_and_detail_history(self):
        a = self.u.create_character(LAN)
        self.u.create_character(HUNG)
        self.assertEqual([x["display_name"] for x in self.u.list_characters(q="lan phuong")["items"]], ["Lan Phương"])
        self.assertEqual(self.u.list_characters(genre="hành động")["total"], 1)
        self.assertEqual(self.u.list_characters(q=a["character_id"])["total"], 1)
        self.u.update_character(a["character_id"], {"temperament": "Lạnh"}, 1)
        d = self.u.character(a["character_id"], detail=True)
        self.assertEqual([e["action"] for e in d["audit"]], ["update", "create"])
        self.assertEqual(d["audit"][0]["before"], {"temperament": ""})

    def test_concurrent_creates_do_not_lose_or_duplicate(self):
        names = ["Aa", "Bb", "Cc", "Dd", "Ee", "Ff", "Gg", "Hh"]
        words = "sông núi lửa băng gió đá rừng biển mây sao trăng nắng mưa sét cát đồng hoa".split()
        errs = []

        def mk(n):
            try:
                self.u.create_character({"display_name": n, "core_personality": " ".join(words[(names.index(n) * 2 + j) % len(words)] + f"x{names.index(n)}{j}" for j in range(4))})
            except Exception as e:                                                                                          # noqa: BLE001
                errs.append(e)
        ts = [threading.Thread(target=mk, args=(n,)) for n in names]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertEqual(self.u.list_characters()["total"], 8)
        self.assertEqual(self.u.revision(), 8)                                                                              # mỗi ghi một revision, không mất

    def test_two_connections_serialize_writes(self):
        db2 = UniverseDB(self.db.path)
        self.addCleanup(db2.close)
        u2 = Universe(db2)
        c = self.u.create_character(LAN)
        self.u.update_character(c["character_id"], {"temperament": "A"}, 1)
        self.assertEqual(code(u2.update_character, c["character_id"], {"temperament": "B"}, 1), "REVISION_CONFLICT")        # LU-008 (tiến trình khác)
        self.assertEqual(u2.character(c["character_id"])["temperament"], "A")


class XlsxTest(unittest.TestCase):
    def test_roundtrip_with_special_chars_and_structure(self):
        data = write_workbook([Sheet("A", ["x", "y"], [["Tiếng Việt <&> \"q\"", "line1\nline2"], [1, 2.5]], lists={0: ["a", "b"]})])
        z = zipfile.ZipFile(io.BytesIO(data))
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
        for needle in ('state="frozen"', "<autoFilter", "<dataValidation ", "inlineStr"):
            self.assertIn(needle, sheet)
        book = read_workbook(data)
        self.assertEqual(book["A"], [["x", "y"], ["Tiếng Việt <&> \"q\"", "line1\nline2"], ["1", "2.5"]])

    def test_rejects_garbage(self):
        with self.assertRaises(XlsxError):
            read_workbook(b"not a zip")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("x.txt", "hi")
        with self.assertRaises(XlsxError):
            read_workbook(buf.getvalue())

    def test_reads_shared_strings_like_excel_saves(self):
        data = write_workbook([Sheet("S", ["a"], [["z"]])])
        src = zipfile.ZipFile(io.BytesIO(data))
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for n in src.namelist():
                body = src.read(n)
                if n == "xl/worksheets/sheet1.xml":
                    body = body.replace(b't="inlineStr"><is><t xml:space="preserve">z</t></is>', b't="s"><v>0</v>').replace(b't="inlineStr"><is><t xml:space="preserve">a</t></is>', b't="s"><v>1</v>')
                z.writestr(n, body)
            z.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>z</t></si><si><t>a</t></si></sst>')
        self.assertEqual(read_workbook(out.getvalue())["S"], [["a"], ["z"]])


class ExchangeTest(Base):
    def edit(self, data: bytes, fn) -> bytes:
        """Sửa sheet Characters của file xuất (như người dùng sửa trong Excel) rồi ghi lại."""
        book = read_workbook(data)
        fn(book["Characters"])
        sheets = [Sheet(n, rows[0], rows[1:]) for n, rows in book.items()]
        return write_workbook(sheets)

    def col(self, rows, name):
        return rows[0].index(name)

    def test_export_is_consistent_snapshot_with_all_sheets(self):
        self.u.create_character(LAN)
        self.u.create_character(HUNG)
        book = read_workbook(EX.export_workbook(self.u))
        self.assertEqual(list(book), EX.SHEETS)
        self.assertEqual(len(book["Characters"]) - 1, 2)
        self.assertEqual(dict(book["Settings"][1:3])["universe_revision"], "2")
        self.assertEqual(book["Characters"][0], EX.CHAR_COLS)

    def test_empty_export_still_valid(self):
        book = read_workbook(EX.export_workbook(self.u))
        self.assertEqual(len(book["Characters"]), 1)

    def test_roundtrip_unchanged_has_nothing_to_apply(self):
        self.u.create_character(LAN)
        rep = EX.preview_import(self.u, EX.export_workbook(self.u))
        self.assertEqual(rep["counts"], {"create": 0, "update": 0, "unchanged": 1, "conflict": 0, "error": 0})
        self.assertFalse(rep["can_apply"])

    def test_import_updates_and_creates_with_dry_run_then_apply(self):
        a = self.u.create_character(LAN)
        data = EX.export_workbook(self.u)

        def fn(rows):
            i = self.col(rows, "temperament")
            rows[1][i] = "Lạnh lùng"
            row = [""] * len(rows[0])
            row[self.col(rows, "display_name")] = "Bảo Châu"
            row[self.col(rows, "core_personality")] = "Hoạt bát, hay đùa, giấu nỗi sợ bóng tối."
            row[self.col(rows, "motivations")] = "Chứng minh bản thân\nBảo vệ bạn bè"
            rows.append(row)
        data = self.edit(data, fn)
        rep = EX.preview_import(self.u, data)
        self.assertEqual((rep["counts"]["update"], rep["counts"]["create"], rep["can_apply"]), (1, 1, True))
        self.assertEqual(self.u.list_characters()["total"], 1)                                                              # dry-run không ghi
        res = EX.apply_import(self.u, data)
        self.assertEqual((len(res["created"]), len(res["updated"])), (1, 1))
        self.assertEqual(self.u.character(a["character_id"])["temperament"], "Lạnh lùng")
        new = self.u.character(res["created"][0])
        self.assertEqual((new["origin"], new["motivations"]), ("approved_import", ["Chứng minh bản thân", "Bảo vệ bạn bè"]))

    def test_import_conflict_when_db_changed_after_export_is_never_overwritten(self):
        a = self.u.create_character(LAN)
        data = EX.export_workbook(self.u)
        self.u.update_character(a["character_id"], {"temperament": "Sửa trong app"}, 1)                                     # người dùng sửa trong app khi file đang mở
        data = self.edit(data, lambda rows: rows[1].__setitem__(self.col(rows, "temperament"), "Sửa trong Excel"))
        rep = EX.preview_import(self.u, data)
        self.assertEqual((rep["counts"]["conflict"], rep["stale"]), (1, True))                                              # LU-009
        self.assertEqual(code(EX.apply_import, self.u, data), "IMPORT_CONFLICTS")
        self.assertEqual(self.u.character(a["character_id"])["temperament"], "Sửa trong app")
        res = EX.apply_import(self.u, data, skip_conflicts=True)
        self.assertEqual((res["updated"], res["skipped_conflicts"]), ([], 1))
        self.assertEqual(self.u.character(a["character_id"])["temperament"], "Sửa trong app")

    def test_invalid_ids_and_bad_rows_reject_whole_import(self):
        self.u.create_character(LAN)
        data = EX.export_workbook(self.u)

        def fn(rows):
            ok = self.col(rows, "temperament")
            rows[1][ok] = "Đổi"
            for cid in ("abc", "ch_000000000000"):
                r = list(rows[1])
                r[0] = cid
                rows.append(r)
            dup = list(rows[1])
            rows.append(dup)
        rep = EX.preview_import(self.u, self.edit(data, fn))
        codes = [r["code"] for r in rep["rows"] if r["action"] == "error"]
        self.assertEqual(sorted(codes), ["DUPLICATE_ID", "INVALID_ID", "UNKNOWN_ID"])
        self.assertFalse(rep["can_apply"])
        self.assertEqual(code(EX.apply_import, self.u, self.edit(data, fn)), "IMPORT_HAS_ERRORS")
        self.assertEqual(self.u.list_characters()["items"][0]["temperament"], "")                                          # không nhập gì cả

    def test_import_blocks_duplicates_locked_core_and_archived(self):
        a = self.u.create_character(LAN)
        self.u.create_character(HUNG)
        locked = self.u.set_lock(a["character_id"], True, 1)
        data = EX.export_workbook(self.u)

        def fn(rows):
            rows[1][self.col(rows, "core_personality")] = "Đổi hẳn tính cách"
            row = [""] * len(rows[0])
            row[self.col(rows, "display_name")] = "Hùng Sói"
            rows.append(row)
        rep = EX.preview_import(self.u, self.edit(data, fn))
        self.assertEqual(sorted(r["code"] for r in rep["rows"] if r["action"] == "error"), ["CHARACTER_LOCKED", "DUPLICATE_CHARACTER"])
        self.assertEqual(locked["revision"], 2)

    def test_import_status_archive_restore_and_not_a_workbook(self):
        a = self.u.create_character(LAN)
        data = EX.export_workbook(self.u)
        EX.apply_import(self.u, self.edit(data, lambda rows: rows[1].__setitem__(self.col(rows, "status"), "archived")))
        self.assertEqual(self.u.character(a["character_id"])["status"], "archived")
        self.assertEqual(code(EX.preview_import, self.u, b"hello"), "INVALID_XLSX")
        no_char = write_workbook([Sheet("X", ["a"], [])])
        self.assertEqual(code(EX.preview_import, self.u, no_char), "INVALID_XLSX")


if __name__ == "__main__":
    unittest.main()
