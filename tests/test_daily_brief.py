from datetime import datetime, timezone
import json
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

    def model_output(self, direction="weaken"):
        return {
            "news": [{"id": "n1", "title_th": "เฟดส่งสัญญาณดอกเบี้ยสูงนาน",
                      "summary_th": "เฟดกังวลแรงกดดันเงินเฟ้อและยังไม่เร่งลดดอกเบี้ย", "tags": [],
                      "thb_impact": {"direction": direction,
                                     "reason_th": "หากดอกเบี้ยสหรัฐสูงนาน อาจหนุนดอลลาร์และกดดันเงินบาท"}}],
            "events": [],
            "outlook": [
                {"asset": "USD", "summary_th": "ดอลลาร์อาจได้แรงหนุนจากดอกเบี้ย", "tags": ["#หนุนดอลลาร์"]},
                {"asset": "Gold", "summary_th": "ต้นทุนถือทองอาจสูงขึ้น", "tags": ["#กดดันทองคำ"]},
                {"asset": "THB", "summary_th": "เงินบาทอาจอ่อนค่าหากดอลลาร์ได้แรงหนุนจากดอกเบี้ย",
                 "tags": [], "thb_impact": {"direction": direction,
                                           "reason_th": "ดอกเบี้ยสหรัฐอาจหนุนดอลลาร์"}},
            ],
        }

    def analyze(self, generated, news=None):
        payload = {"candidates": [{"content": {"parts": [{"text": json.dumps(generated)}]}}]}
        response = type("Response", (), {"ok": True, "json": lambda self: payload})()
        with (
            patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
            patch.object(brief.requests, "post", return_value=response),
        ):
            return brief.translate_and_analyze(news or [self.item("Fed signals higher rates")], [])

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
        news = [self.item("Fed signals higher rates", "Fed sees inflation risks.")]
        generated = self.model_output()
        generated["news"][0]["tags"] = ["#หนุนดอลลาร์", "#unapproved"]
        payload = {"candidates": [{"content": {"parts": [{"text": (
            json.dumps(generated)
        )}]}}]}
        response = type("Response", (), {"ok": True, "json": lambda self: payload})()
        with (
            patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}),
            patch.object(brief.requests, "post", return_value=response) as post,
        ):
            translated = brief.translate_and_analyze(news, [])
        self.assertEqual(translated["news"][0]["title"], "เฟดส่งสัญญาณดอกเบี้ยสูงนาน")
        self.assertEqual(translated["news"][0]["tags"], ["#หนุนดอลลาร์", "#กดดันเงินบาท", "#บาทอ่อน"])
        self.assertEqual(translated["outlook"][2]["tags"], ["#กดดันเงินบาท", "#บาทอ่อน"])
        self.assertEqual(post.call_args.kwargs["headers"]["x-goog-api-key"], "test-key")
        prompt = post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("ไม่จำเป็นต้องมีคำว่า baht ในต้นฉบับ", prompt)

    def test_empty_model_tags_still_produce_explained_per_story_baht_tag(self):
        analysis = self.analyze(self.model_output())
        text = brief.render(**{key: analysis[key] for key in ("events", "outlook")},
                            items=analysis["news"], now=datetime.now(timezone.utc))
        story = text.split("แหล่งข่าว:")[0]
        self.assertIn("#กดดันเงินบาท", story)
        self.assertIn("ผลต่อเงินบาท (ประเมิน)", story)
        self.assertIn("หากดอกเบี้ยสหรัฐสูงนาน", story)
        self.assertNotIn("ข่าวนี้ยังไม่มีข้อมูลชี้ทิศทางเงินบาท", story)

    def test_all_impact_states_have_distinct_tags(self):
        expected = {"strengthen": "#หนุนเงินบาท", "weaken": "#กดดันเงินบาท",
                    "mixed": "#เงินบาทสองทาง", "neutral": "#ผลต่อเงินบาทจำกัด"}
        for direction, tag in expected.items():
            with self.subTest(direction=direction):
                result = self.analyze(self.model_output(direction))
                self.assertIn(tag, result["news"][0]["tags"])
                self.assertIn(tag, result["outlook"][2]["tags"])
                if direction in {"mixed", "neutral"}:
                    self.assertFalse({"#บาทอ่อน", "#บาทแข็ง"} & set(result["news"][0]["tags"]))

    def test_missing_or_unexplained_impact_is_rejected_before_send(self):
        for invalid in (None, {}, {"direction": "weaken", "reason_th": ""},
                        {"direction": "invented", "reason_th": "เหตุผล"}):
            with self.subTest(invalid=invalid):
                generated = self.model_output()
                generated["news"][0]["thb_impact"] = invalid
                with self.assertRaisesRegex(RuntimeError, "THB impact"):
                    self.analyze(generated)

    def test_overall_outlook_is_not_inferred_from_story_or_future_event_tags(self):
        generated = self.model_output()
        generated["outlook"][2]["thb_impact"] = {
            "direction": "mixed", "reason_th": "ดอกเบี้ยกับแรงซื้อสินทรัพย์เสี่ยงอาจหักล้างกัน"}
        result = self.analyze(generated)
        self.assertIn("#บาทอ่อน", result["news"][0]["tags"])
        self.assertEqual(result["outlook"][2]["tags"], ["#เงินบาทสองทาง"])
        rendered = brief.render(result["news"], [], result["outlook"], datetime.now(timezone.utc))
        self.assertIn("#เงินบาทสองทาง", rendered)
        self.assertNotIn("⚪ ยังไม่มีข้อมูลชี้ทิศทางเงินบาท", rendered)

    def test_keyword_hint_cannot_override_neutral_assessment(self):
        news = [self.item("Dollar rises against the yen")]
        news[0]["tags"] = ["#บาทอ่อน"]
        generated = self.model_output("neutral")
        generated["news"][0]["tags"] = ["#หนุนเงินบาท", "#บาทแข็ง"]
        result = self.analyze(generated, news)
        self.assertEqual(result["news"][0]["tags"], ["#ผลต่อเงินบาทจำกัด"])

    def test_old_baht_and_gold_tag_spellings_are_supported(self):
        self.assertEqual(brief.normalize_tags(["#หนุนบาท", "#หนุนเงินบาท", "#กดดันบาท", "#หนุนทอง"]),
                         ["#หนุนเงินบาท", "#กดดันเงินบาท", "#หนุนทองคำ"])

    def test_policy_news_is_not_crowded_out_by_newer_company_news(self):
        corporate = [dict(self.item("Company raises funds for industrial growth", published="2026-11-17T10:00:00Z"),
                          id=f"c{index}") for index in range(6)]
        policy = self.item("Fed signals interest rates will stay high", published="2026-11-16T20:00:00Z")
        result = brief.summarize([*corporate, policy], datetime.now(timezone.utc))
        self.assertEqual(result[0]["title"], policy["title"])
        self.assertEqual(len(result), 6)

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
