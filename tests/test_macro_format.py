from unittest import TestCase
from unittest.mock import patch

import main


class MacroFormatTests(TestCase):
    def test_summary_uses_mobile_blocks_and_hides_pmi_breakdown(self):
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
            patch.object(main, "compute_trade_signal", return_value="📌 สัญญาณ: ⚪ เอียง LONG (2L/1S: Score LONG, Trend SHORT, Basis CHEAP->LONG)"),
            patch.object(main, "fetch_calendar", return_value=[]),
        ):
            message = main.format_message({}, None)

        self.assertIn("📊 Macro Summary\n🕒 07/10/2026 21:49 (TH)", message)
        self.assertIn("USDZ26 33.4100 ▼-0.03%\nFair 33.4910 · 🟢 CHEAP\nBasis -0.0810 THB", message)
        self.assertIn("USDH27 33.4100 ▼-0.03%\nFair 33.2684 · 🔴 RICH\nBasis +0.1416 THB", message)
        self.assertIn("📌 ⚪ เอียง LONG (2L/1S)\nScore L · Trend S · Basis L", message)
        self.assertTrue(all(len(line) <= 40 for line in message.splitlines()))
        self.assertLessEqual(len(message.splitlines()), 30)
        self.assertNotIn("DXY -0.82", message)
        self.assertNotIn("PMI", message)
