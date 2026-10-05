import unittest

from contentfactory.contracts import StageError
from contentfactory.story.assembler import assemble
from contentfactory.story.validate import validate_story_text

TAIL = "Tiếng chuông chùa vọng lại từ phía xa, nghe như một lời nhắc nhở nặng nề rơi xuống con hẻm vắng lặng."
LONG = ("Người đàn ông áo đen đứng im lặng trước cánh cửa gỗ cũ kỹ rất lâu mà không nói một lời nào, "
        "mặc cho cơn mưa đêm cứ thế trút xuống vai áo ướt sũng và lạnh buốt.")
OLD = "Bà lão nhìn anh bằng đôi mắt đục mờ rồi gật đầu chậm rãi như thể đã chờ đợi từ rất lâu rồi."

S1 = f"""Chương 1: Đêm mưa

Trời mưa rất to suốt cả đêm, và con hẻm nhỏ chìm trong bóng tối lạnh lẽo đến rợn người.
{LONG}
{TAIL}

---
(Còn tiếp)
"""
S2 = f"""## Section 2

{TAIL}
Cánh cửa cuối cùng cũng mở ra và một ánh đèn vàng hắt xuống nền đất ẩm ướt, loang lổ rêu xanh.
{OLD}
{OLD}
"""
S3 = """第三章 开始

**Phần ba**

Ở chương trước, người đàn ông đã bước vào trong căn nhà cũ kỹ ấy sau bao nhiêu do dự.
Anh theo bà lão đi qua hành lang dài, nơi những bức ảnh cũ treo kín hai bên tường lặng lẽ,
"""
S4 = f"""và anh nhận ra mình đã từng đến đây từ rất lâu về trước, trong một giấc mơ mà anh không còn nhớ nổi.

Chương 4

{LONG.replace("rất lâu", "khá lâu")}
Phần lớn thời gian trong căn nhà ấy trôi qua trong im lặng, và đó là điều anh sợ nhất.
"""


class AssemblerTest(unittest.TestCase):
    def setUp(self):
        # fixture ngắn và cố ý nhồi rác (~39% bị loại) nên nới ngưỡng an toàn; mặc định 35% dành cho chương thật dài
        self.out, self.rep = assemble([S1, S2, S3, S4], max_removed_ratio=0.6)
        self.paras = self.out.strip().split("\n\n")

    def test_no_heading_marker_or_internal_metadata_survives(self):
        self.assertEqual(validate_story_text(self.out), [])
        for junk in ("Chương", "Section", "第三章", "Phần ba", "Còn tiếp", "---", "Ở chương trước"):
            self.assertNotIn(junk, self.out)
        self.assertEqual(len(self.rep["headings_removed"]), 5)       # Chương 1, ## Section 2, 第三章, **Phần ba**, Chương 4
        self.assertEqual(len(self.rep["recaps_removed"]), 1)
        self.assertEqual(len(self.rep["meta_removed"]), 2)           # '---' và '(Còn tiếp)'

    def test_paragraphs_are_separated_by_blank_lines_only(self):
        self.assertTrue(all("\n" not in p for p in self.paras))      # tách đoạn theo dòng trống, như bước TTS cần
        self.assertTrue(self.out.endswith("\n") and not self.out.endswith("\n\n"))

    def test_section_that_repeats_the_previous_tail_is_trimmed(self):
        self.assertEqual(self.out.count("Tiếng chuông chùa vọng lại"), 1)
        self.assertEqual(self.rep["overlaps_trimmed"][0]["sentences"], 1)
        self.assertIn("Cánh cửa cuối cùng cũng mở ra", self.out)

    def test_accidental_duplicates_are_detected(self):
        self.assertEqual(self.out.count("Bà lão nhìn anh"), 1)       # trùng khít liền kề
        self.assertEqual(self.out.count("cánh cửa gỗ cũ kỹ rất lâu"), 1)   # đoạn gần trùng ở section 4
        self.assertNotIn("khá lâu", self.out)
        kinds = {d["kind"] for d in self.rep["duplicates_removed"]}
        self.assertTrue({"exact", "near"} <= kinds, kinds)

    def test_sentence_cut_at_a_section_boundary_is_rejoined(self):
        self.assertIn("treo kín hai bên tường lặng lẽ, và anh nhận ra mình đã từng đến đây", self.out)
        self.assertEqual(self.rep["joins"], [{"section": 3, "kind": "continued_sentence"}])

    def test_real_content_is_kept(self):
        for keep in ("Trời mưa rất to suốt cả đêm", "Người đàn ông áo đen", "Cánh cửa cuối cùng",
                     "Phần lớn thời gian trong căn nhà ấy"):
            self.assertIn(keep, self.out)
        self.assertLess(self.rep["removed_ratio"], 0.6)

    def test_body_sentences_that_look_like_headings_are_not_removed(self):
        text, rep = assemble(["Phần lớn thời gian anh ở nhà một mình, không gặp ai cả.\n"
                              "Hồi ba năm trước anh rời đi và không bao giờ trở lại nữa\n"
                              "Chương 2 đã kết thúc trong im lặng, như mọi chuyện khác ở nơi này.\n"])
        self.assertEqual(rep["headings_removed"], [])
        self.assertEqual(validate_story_text(text), ["CHAPTER_HEADER"])   # validator mới là lưới chặn cuối

    def test_assembling_twice_changes_nothing(self):
        again, _ = assemble([self.out])
        self.assertEqual(again, self.out)

    def test_safety_net_refuses_to_silently_eat_the_story(self):
        with self.assertRaises(StageError) as cm:
            assemble([OLD + "\n" + OLD, OLD + "\n" + OLD, OLD])
        self.assertEqual(cm.exception.code, "ASSEMBLER_REMOVED_TOO_MUCH")


if __name__ == "__main__":
    unittest.main()
