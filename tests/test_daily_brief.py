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

    def test_rss_selection_and_tags_need_no_openai_key(self):
        news = [
            self.item("Dollar gains as gold falls", "The dollar gains and gold falls after the data."),
            self.item("Celebrity announces a new album", "A music release, not related to finance."),
        ]
        with patch.object(brief.requests, "post", side_effect=AssertionError("RSS mode must not call an LLM")):
            result = brief.summarize(news, datetime.now(timezone.utc))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tags"], ["#กดดันทองคำ", "#หนุนดอลลาร์"])
        self.assertEqual(result[0]["summary"], news[0]["description"])

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
