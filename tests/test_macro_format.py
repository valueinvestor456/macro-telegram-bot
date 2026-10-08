from unittest import TestCase
from unittest.mock import patch

import main


class MacroFormatTests(TestCase):
    def test_summary_uses_compact_usdz26_line_and_hides_pmi_breakdown(self):
        score = {
            "score": -89,
            "short_verdict": "บาทอ่อนค่า",
            "contrib": {"DXY": -0.82, "PMI": 0.06},
        }
        futures = {
            "price": 33.4100,
            "pct": -0.03,
        }
        with (
            patch.object(main, "format_th_timestamp", return_value="07/10/2026 21:49"),
            patch.object(main, "compute_thb_score", return_value=score),
            patch.object(
                main,
                "compute_cip_fair_fixed",
                side_effect=[{"fair": 33.4910}, {"fair": 33.2684}],
            ),
            patch.object(main, "fetch_tfex_usd_futures_dated", return_value=futures),
            patch.object(main, "format_trend_line", return_value="Trend"),
            patch.object(main, "compute_trade_signal", return_value=None),
            patch.object(main, "fetch_calendar", return_value=[]),
        ):
            message = main.format_message({}, None)

        self.assertIn("USDZ26 (Real/Fair): 33.4100 / 33.4910 🔴▼-0.03%", message)
        self.assertIn("USDH27 (Real/Fair): 33.4100 / 33.2684 🟢▲premium 0.14", message)
        self.assertNotIn("มูลค่ายุติธรรม:", message)
        breakdown = next(line for line in message.splitlines() if "DXY " in line)
        self.assertIn("DXY -0.82", breakdown)
        self.assertNotIn("PMI", breakdown)

    def test_usdh27_comparison_uses_price_gap_not_daily_change(self):
        for fair, expected in (
            ({"fair": 33.2215}, "33.2215 🔴▼discount 0.13"),
            ({"fair": 33.0900}, "33.0900 ⚪ fair 0.00"),
            (None, "N/A ⚠️"),
        ):
            with (
                self.subTest(fair=fair),
                patch.object(main, "compute_thb_score", return_value=None),
                patch.object(main, "compute_cip_fair_fixed", side_effect=[None, fair]),
                patch.object(main, "fetch_tfex_usd_futures_dated", return_value={"price": 33.09, "pct": 0.15}),
                patch.object(main, "compute_trade_signal", return_value=None),
                patch.object(main, "fetch_calendar", return_value=[]),
            ):
                message = main.format_message({}, None)
                line = next(line for line in message.splitlines() if line.startswith("USDH27"))
                self.assertEqual(line, f"USDH27 (Real/Fair): 33.0900 / {expected}")
