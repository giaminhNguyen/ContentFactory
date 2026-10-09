"""Tự đặt tên truyện (story/naming.py): ngân sách ký tự, sáng tác lại khi vượt, không bao giờ cắt."""
import unittest

from contentfactory.story.naming import brand_marks, make_titler, parse_title


class FakeLLM:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, prompt, *, system, step, ctx=None):
        self.prompts.append(prompt)
        return {"text": self.replies.pop(0)}


class TitlerTest(unittest.TestCase):
    B = {"title": "Tên Gốc", "language": "vi"}

    def test_too_long_is_rewritten_never_cut(self):
        llm = FakeLLM(["TÔI " * 20, "TÔI RÚT TÀI TRỢ"])
        self.assertEqual(make_titler(llm)("dàn ý", self.B, None, "[K số 99] ", 20), "TÔI RÚT TÀI TRỢ")
        self.assertIn("TỐI ĐA 20 ký tự", llm.prompts[0])
        self.assertIn("VƯỢT ngân sách 20", llm.prompts[1])                       # lượt 2 được báo lý do, yêu cầu sáng tác lại

    def test_still_too_long_after_all_attempts_returns_shortest_uncut(self):
        llm = FakeLLM(["A" * 40, "B" * 30, "C" * 35])
        self.assertEqual(make_titler(llm)("dàn ý", self.B, None, "", 20), "B" * 30)   # không cắt: output sẽ báo TITLE_TOO_LONG

    def test_source_title_is_rejected_and_label_stripped(self):
        self.assertEqual(make_titler(FakeLLM(["Tên gốc", "Tiêu đề: “TÊN MỚI”"]))("x", self.B, None, "", 50), "TÊN MỚI")
        self.assertIsNone(parse_title("  \n "))

    def test_source_brand_traces_are_rejected_and_rewritten(self):
        src = "【Truyện Audio】TÁI SINH, TÔI CẮT TÀI TRỢ | Tinh Hà Audio"
        self.assertEqual(brand_marks(src), ["Truyện Audio", "Tinh Hà Audio"])
        llm = FakeLLM(["TÔI CẮT TÀI TRỢ - TINH HÀ AUDIO", "TÔI CẮT TÀI TRỢ, KẺ THÙ BẼ MẶT"])
        self.assertEqual(make_titler(llm)("x", {"title": src, "language": "vi"}, None, "", 80), "TÔI CẮT TÀI TRỢ, KẺ THÙ BẼ MẶT")
        self.assertIn("còn dấu vết nguồn cũ", llm.prompts[1])                 # lượt 2 được báo lý do


if __name__ == "__main__":
    unittest.main()
