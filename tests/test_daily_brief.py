from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import daily_brief as brief


class DailyBriefTests(unittest.TestCase):
    def item(self, title, description="", published="2026-11-07T10:00:00+00:00", source="CNBC"):
        return {
            "id": "n1", "source": source, "title": title, "description": description,
            "published": published, "url": "https://example.com/story",
        }

    def test_rss_selection_filters_irrelevant_news(self):
        news = [
            self.item("Dollar gains as gold falls", "The dollar gains and gold falls after the data."),
            self.item("Celebrity announces a new album", "A music release, not related to finance."),
        ]
        result = brief.summarize(news, datetime.now(timezone.utc))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tags"], ["#กดดันทองคำ", "#หนุนดอลลาร์"])
        self.assertEqual(result[0]["summary"], news[0]["description"])

    def test_gemini_translates_news_and_limits_impact_tags(self):
        news = [self.item("Dollar gains", "Dollar rises after data.")]
        payload = {"candidates": [{"content": {"parts": [{"text": (
            '{"items":[{"id":"n1","title_th":"ดอลลาร์แข็งค่าหลังข้อมูลเศรษฐกิจ",'
            '"summary_th":"ดอลลาร์ปรับขึ้นหลังรายงานข้อมูลเศรษฐกิจ",'
            '"tags":["#หนุนดอลลาร์","#unapproved"]}]}'
        )}]}}]}
        response = type("Response", (), {"ok": True, "json": lambda self: payload})()
        with (
            patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
            patch.object(brief.requests, "post", return_value=response) as post,
        ):
            translated = brief.translate_and_analyze(news)
        self.assertEqual(translated[0]["title"], "ดอลลาร์แข็งค่าหลังข้อมูลเศรษฐกิจ")
        self.assertEqual(translated[0]["tags"], ["#หนุนดอลลาร์"])
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "test-key")

    def test_gemini_key_is_required_when_there_are_news_items(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
                brief.translate_and_analyze([self.item("Dollar gains")])

    def test_conflicting_price_direction_does_not_assign_tag(self):
        result = brief.summarize(
            [self.item("Dollar rises; dollar falls as markets weigh Fed outlook")],
            datetime.now(timezone.utc),
        )
        self.assertEqual(result[0]["tags"], [])

    def test_missing_rss_description_is_disclosed(self):
        result = brief.summarize(
            [self.item("Fed discusses interest rates", source="Fed")],
            datetime.now(timezone.utc),
        )
        self.assertIn("RSS ไม่มีคำอธิบาย", result[0]["summary"])


if __name__ == "__main__":
    unittest.main()
