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
        breakdown = next(line for line in message.splitlines() if "DXY " in line)
        self.assertIn("DXY -0.82", breakdown)
        self.assertNotIn("PMI", breakdown)
