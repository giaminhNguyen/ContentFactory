import re
import unittest

from contentfactory.contracts import ErrorClass, StageError
from contentfactory.source.reconstruct import ReconstructConfig, build_structured, clean_text
from contentfactory.source.subtitles import parse_subtitle
from contentfactory.source.youtube import choose_subtitle, classify_error, parse_video_id
from tests.fakes import FIXTURES


def structured(name: str, cfg: ReconstructConfig | None = None) -> dict:
    cues = parse_subtitle((FIXTURES / name).read_text(encoding="utf-8"))
    return build_structured(cues, cfg or ReconstructConfig(), {})


class CaptionReconstructionTest(unittest.TestCase):
    def test_caption_split_mid_sentence_is_rejoined_and_lines_are_not_sentences(self):
        s = structured("manual_split.srt")
        self.assertEqual(s["stats"]["cues"], 5)
        texts = [x["text"] for x in s["sentences"]]
        self.assertEqual(texts, ["Hôm qua tôi đi chợ và gặp một người bạn cũ ở cổng.",
                                 "Ông ấy hỏi thăm tôi rất lâu, rồi mời tôi đi uống cà phê.",
                                 "Tôi từ chối vì đang vội."])
        cue_texts = {c["text"] for c in s["cues"]}
        self.assertFalse(cue_texts & set(texts[:2]), "câu bị cắt ngang cue không được trùng một dòng caption")
        self.assertNotEqual(len(s["sentences"]), len(s["cues"]))
        # câu đầu ghép từ cue 0 và 1; câu thứ hai bắt đầu giữa cue 1 nên timestamp là nội suy
        self.assertEqual(s["sentences"][0]["cue_range"], [0, 1])
        self.assertTrue(s["sentences"][1]["interpolated_time"])

    def test_timestamp_gaps_drive_sentence_and_paragraph_breaks(self):
        s = structured("gaps.vtt")
        texts = [x["text"] for x in s["sentences"]]
        self.assertEqual(texts, ["Tối hôm đó trời mưa rất to tôi ngồi một mình trong căn nhà cũ.",   # gap 0.2s: cùng câu
                                 "Bỗng có tiếng gõ cửa tôi giật mình nhìn ra ngoài.",                 # gap 1.2s: câu mới
                                 "Sáng hôm sau mọi chuyện đã khác."])                                  # gap 4.0s: đoạn mới
        self.assertEqual([p["sentence_range"] for p in s["paragraphs"]], [[0, 1], [2, 2]])
        self.assertAlmostEqual(s["sentences"][1]["gap_before"], 1.2, places=2)
        self.assertAlmostEqual(s["paragraphs"][1]["gap_before"], 4.0, places=2)
        self.assertEqual(s["stats"]["punctuation_added"], 3)
        self.assertTrue(all(x["capitalized"] for x in s["sentences"]))
        self.assertEqual(clean_text(s).count("\n\n"), 1)                    # đúng một ranh giới đoạn

    def test_structured_keeps_start_end_text_and_gap(self):
        s = structured("gaps.vtt")
        for c in s["cues"]:
            self.assertTrue({"start", "end", "text", "gap_before"} <= set(c))
        for x in s["sentences"]:
            self.assertTrue({"start", "end", "text", "gap_before", "internal_pauses"} <= set(x))
        self.assertEqual(s["cues"][2]["gap_before"], 1.2)

    def test_clean_transcript_has_no_timestamps(self):
        for name in ("manual_split.srt", "gaps.vtt", "auto_rolling.vtt"):
            text = clean_text(structured(name))
            self.assertNotIn("-->", text)
            self.assertIsNone(re.search(r"\d{1,2}:\d{2}", text), name)

    def test_internal_pauses_are_kept_for_later_punctuation_restoration(self):
        cfg = ReconstructConfig(sentence_gap=2.0, paragraph_gap=9.0)        # nâng ngưỡng để pause 1.2s nằm TRONG câu
        s = structured("gaps.vtt", cfg)
        self.assertEqual(s["sentences"][0]["text"].count("."), 1)
        pauses = [p for x in s["sentences"] for p in x["internal_pauses"]]
        self.assertIn(1.2, [p["gap"] for p in pauses])


class AutoCaptionTest(unittest.TestCase):
    def test_rolling_duplicates_tags_entities_and_sound_cues_are_removed(self):
        cues = parse_subtitle((FIXTURES / "auto_rolling.vtt").read_text(encoding="utf-8"))
        self.assertEqual([c.text for c in cues], ["hôm qua tôi đi chợ và gặp", "một người bạn cũ ở cổng",
                                                  "ông ấy hỏi thăm tôi rất lâu", "sau đó chúng tôi chia tay"])
        self.assertEqual(cues[0].start, 0.0)
        self.assertEqual(cues[-1].start, 11.5)

    def test_legit_repeated_caption_is_not_dropped(self):
        srt = "1\n00:00:00,000 --> 00:00:01,500\nKhông.\n\n2\n00:00:01,500 --> 00:00:03,000\nKhông.\n"
        self.assertEqual([c.text for c in parse_subtitle(srt)], ["Không.", "Không."])


class YoutubeHelpersTest(unittest.TestCase):
    def test_video_id_forms(self):
        for url in ("https://www.youtube.com/watch?v=abcdefghijk&t=3", "https://youtu.be/abcdefghijk?si=x",
                    "https://m.youtube.com/shorts/abcdefghijk", "https://www.youtube.com/live/abcdefghijk"):
            self.assertEqual(parse_video_id(url), "abcdefghijk")
        for bad in ("https://example.com/watch?v=abcdefghijk", "https://www.youtube.com/watch?v=short", "not a url"):
            with self.assertRaises(StageError) as cm:
                parse_video_id(bad)
            self.assertEqual(cm.exception.error_class, ErrorClass.POLICY)

    def test_prefers_manual_subtitles_over_auto(self):
        info = {"subtitles": {"en": [{}]}, "automatic_captions": {"vi-orig": [{}], "vi": [{}]}, "language": "vi"}
        self.assertEqual(choose_subtitle(info, ["vi", "en"]), {"lang": "en", "auto": False})   # có sẵn > auto
        info["subtitles"] = {"vi": [{}], "en": [{}]}
        self.assertEqual(choose_subtitle(info, ["vi", "en"]), {"lang": "vi", "auto": False})

    def test_auto_picks_original_language_track(self):
        info = {"subtitles": {}, "automatic_captions": {"en": [{}], "vi-orig": [{}], "vi": [{}]}, "language": "vi"}
        self.assertEqual(choose_subtitle(info, ["en"]), {"lang": "vi-orig", "auto": True})

    def test_no_subtitles(self):
        self.assertIsNone(choose_subtitle({"subtitles": {}, "automatic_captions": {}}, ["vi"]))

    def test_error_classification(self):
        self.assertEqual(classify_error("ERROR: Private video").code, "VIDEO_UNAVAILABLE")
        self.assertEqual(classify_error("Sign in to confirm you're not a bot").error_class, ErrorClass.AUTH)
        self.assertEqual(classify_error("HTTP Error 429: Too Many Requests").error_class, ErrorClass.TRANSIENT)


if __name__ == "__main__":
    unittest.main()
