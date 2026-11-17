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

    def outlook(self, thb_tags=None):
        return [
            {"asset": "USD", "summary": "ข้อมูลค่าเงินดอลลาร์ยังไม่ชัดเจน", "tags": []},
            {"asset": "Gold", "summary": "ข่าวยังไม่ชี้ทิศทางทองคำ", "tags": []},
            {"asset": "THB", "summary": "ดอลลาร์แข็งอาจกดดันเงินบาท", "tags": thb_tags or []},
        ]

    def test_rss_selection_filters_irrelevant_news(self):
        news = [
            self.item("Dollar gains as gold falls", "The dollar gains and gold falls after the data."),
            self.item("Celebrity announces a new album", "A music release, not related to finance."),
        ]
        result = brief.summarize(news, datetime.now(timezone.utc))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tags"], ["#กดดันทองคำ", "#บาทอ่อน"])
        self.assertEqual(result[0]["summary"], news[0]["description"])

    def test_gemini_translates_news_and_limits_impact_tags(self):
        news = [self.item("Dollar gains", "Dollar rises after data.")]
        news[0]["tags"] = ["#บาทอ่อน"]
        payload = {"candidates": [{"content": {"parts": [{"text": (
            '{"news":[{"id":"n1","title_th":"ดอลลาร์แข็งค่าหลังข้อมูลเศรษฐกิจ",'
            '"summary_th":"ดอลลาร์ปรับขึ้นหลังรายงานข้อมูลเศรษฐกิจ",'
            '"tags":["#unapproved"]}],"events":[],"outlook":['
            '{"asset":"USD","summary_th":"ข้อมูลยังไม่ชัดเจน","tags":[]},'
            '{"asset":"Gold","summary_th":"ข้อมูลยังไม่ชัดเจน","tags":[]},'
            '{"asset":"THB","summary_th":"ดอลลาร์แข็งอาจกดดันเงินบาท","tags":[]}]}'
        )}]}}]}
        response = type("Response", (), {"ok": True, "json": lambda self: payload})()
        with (
            patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
            patch.object(brief.requests, "post", return_value=response) as post,
        ):
            translated = brief.translate_and_analyze(news, [])
        self.assertEqual(translated["news"][0]["title"], "ดอลลาร์แข็งค่าหลังข้อมูลเศรษฐกิจ")
        self.assertEqual(translated["news"][0]["tags"], ["#บาทอ่อน"])
        self.assertEqual(translated["outlook"][2]["tags"], ["#บาทอ่อน"])
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "test-key")
        prompt = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("#บาทอ่อน หรือ #บาทแข็ง", prompt)

    def test_gemini_key_is_required_when_there_are_news_items(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
                brief.translate_and_analyze([self.item("Dollar gains")], [])

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

    def test_render_shows_baht_direction_in_asset_outlook(self):
        item = self.item("Dollar gains")
        item["title"] = "ดอลลาร์แข็งค่าหลังข้อมูลเศรษฐกิจ"
        item["summary"] = "ดอลลาร์แข็งอาจกดดันเงินบาท"
        item["tags"] = ["#บาทอ่อน"]
        text = brief.render(
            [item], [], self.outlook(["#บาทอ่อน"]),
            datetime(2026, 11, 9, 2, tzinfo=timezone.utc),
        )
        self.assertIn("เงินบาท (USD/THB)", text)
        self.assertIn("#บาทอ่อน", text)
        self.assertNotIn("#รอติดตามทิศทางตลาด", text)

    def test_render_discloses_unclear_baht_direction(self):
        text = brief.render(
            [], [], self.outlook(),
            datetime(2026, 11, 9, 2, tzinfo=timezone.utc),
        )
        self.assertIn("ยังไม่มีข้อมูลชี้ทิศทางเงินบาท", text)

    def test_main_wires_news_events_and_outlook_to_render(self):
        analysis = {
            "news": [], "events": [], "outlook": self.outlook(["#บาทแข็ง"]),
        }
        with (
            patch.dict("os.environ", {"DAILY_BRIEF_DRY_RUN": "true"}),
            patch.object(brief, "fetch_news", return_value=[]),
            patch.object(brief, "fetch_calendar", return_value=[]),
            patch.object(brief, "summarize", return_value=[]),
            patch.object(brief, "translate_and_analyze", return_value=analysis) as translate,
            patch("builtins.print") as print_output,
        ):
            result = brief.main()
        self.assertEqual(result, 0)
        translate.assert_called_once_with([], [])
        self.assertIn("#บาทแข็ง", print_output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
