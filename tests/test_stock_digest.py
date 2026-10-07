import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

import stock_digest as sd

TODAY = date(2026, 10, 6)


def table(table_id, headers, rows):
    return ('<h4>ข้อมูลประจำวันที่ 6 ต.ค. 2569</h4>'
            f'<table id="{table_id}"><tr>' + ''.join(f'<th>{h}</th>' for h in headers) + '</tr>'
            + ''.join('<tr>' + ''.join(f'<td>{v}</td>' for v in row) + '</tr>' for row in rows) + '</table>')


def insider(qty='500,000', price='3.00', **kwargs):
    row = ['บริษัท ทดสอบ (TEST)', 'นาย ทดสอบ', 'ผู้รายงาน', 'หุ้นสามัญ',
           '05/10/2569', qty, price, 'ซื้อ',
           '<a href="https://market.sec.or.th/r59/th/report?batchNo=1&amp;transId=2&amp;executor=3">Link</a>']
    for index, value in kwargs.items():
        row[int(index)] = value
    return row


def sample(identifier='a', ticker='TEST', priority=85):
    return sd.Event(identifier, ticker, 'ซื้อขายกิจการ/ลงทุน', TODAY,
                    'หัวข้อประกาศทดสอบ', 'ตรวจราคาดีล', 'https://www.set.or.th/example?id=' + identifier, priority)


class ParsingTests(unittest.TestCase):
    def test_thai_dates_and_bangkok_time(self):
        self.assertEqual(sd.thai_date('05/10/2569'), date(2026, 10, 5))
        self.assertEqual(sd.thai_date('6 ต.ค. 2569 17:01:00'), TODAY)
        msg, _ = sd.render([sample()], [], datetime(2026, 10, 6, 18, tzinfo=timezone.utc))
        self.assertIn('07/10/2026 01:00', msg)

    def test_r59_reads_price_not_relationship(self):
        events = sd.parse_r59(table('gPP09T01', sd.R59_HEADERS, [insider()]), TODAY)
        self.assertEqual(events[0].ticker, 'TEST')
        self.assertEqual(events[0].value, Decimal('1500000'))
        self.assertIn('500,000 หุ้น × 3.00 บาท', events[0].facts)
        self.assertIn('05/10/2026', events[0].facts)
        self.assertNotIn('IPO', events[0].category)

    def test_revoked_missing_price_transfer_and_non_stock_excluded(self):
        rows = [insider(qty='500,000 Revoked by Reporter'), insider(price='-'),
                insider(**{'7': 'รับโอน'}), insider(**{'3': 'ใบสำคัญแสดงสิทธิ'}),
                insider(price='NaN')]
        self.assertEqual(sd.parse_r59(table('gPP09T01', sd.R59_HEADERS, rows), TODAY), [])

    def test_changed_columns_and_stale_source_fail_closed(self):
        html = table('gPP09T01', sd.R59_HEADERS, [insider()])
        with self.assertRaises(ValueError):
            sd.parse_r59(html.replace('<th>ราคา</th>', '<th>อื่น</th>'), TODAY)
        with self.assertRaises(ValueError):
            sd.parse_r59(html.replace('6 ต.ค. 2569', '6 ก.ย. 2569'), TODAY)

    def test_r246_individual_group_and_note_are_distinct(self):
        row = ['TEST', 'ผู้ถือหุ้นทดสอบ', 'จำหน่าย', 'หุ้น', '18.7', '4.8', '13.9',
               '30/09/2569', '20', '4.8', '15.2', 'ฉบับแก้ไข',
               '<a href="https://web-r246-api.sec.or.th/api/forms/pdf/publish/123">PDF</a>', '246-123']
        event = sd.parse_r246(table('gPP10T01', sd.R246_HEADERS, [row]), TODAY)[0]
        self.assertIn('18.7% → 13.9%', event.facts)
        self.assertIn('20% → 15.2%', event.facts)
        self.assertIn('ฉบับแก้ไข', event.facts)
        self.assertIn('ยังไม่ยืนยัน', event.follow_up)

class SelectionTests(unittest.TestCase):
    def test_duplicate_rows_and_previous_sends(self):
        self.assertEqual(len(sd.select_events([sample(), sample()], {'sent': {}})), 1)
        self.assertEqual(sd.select_events([sample()], {'sent': {'a': '2026-10-05'}}), [])

    def test_distinct_transactions_same_company_remain_distinct(self):
        html = table('gPP09T01', sd.R59_HEADERS,
                     [insider(), insider(price='4.00', **{'8': '<a href="https://market.sec.or.th/r59/th/report?transId=4">Link</a>'})])
        self.assertEqual(len(sd.select_events(sd.parse_r59(html, TODAY), {'sent': {}})), 2)

    def test_small_insider_only_in_explicit_watchlist(self):
        event = sd.parse_r59(table('gPP09T01', sd.R59_HEADERS, [insider(qty='100')]), TODAY)[0]
        self.assertEqual(sd.select_events([event], {'sent': {}}), [])
        self.assertEqual(sd.select_events([event], {'sent': {}}, {'TEST'}), [event])

    def test_watchlist_prioritized_and_message_limit(self):
        events = [sample(str(i), f'TEST{i}') for i in range(20)]
        events[-1].facts = 'ข้อมูล 🟢 ' * 1500
        selected = sd.select_events(events, {'sent': {}}, {'TEST3'})
        self.assertEqual(selected[0].ticker, 'TEST3')
        msg, included = sd.render(selected, [], watchlist={'TEST3'})
        self.assertLessEqual(sd.utf16_size(msg), 3700)
        self.assertLessEqual(len(included), 5)
        self.assertNotIn(events[-1], included)

    def test_empty_rounds_do_not_send_status_messages(self):
        self.assertEqual(sd.render([], []), ('', []))
        self.assertEqual(sd.render([], ['SEC แบบ 59']), ('', []))

    def test_only_ticker_headline_and_link_are_rendered(self):
        event = sample()
        event.headline = 'แจ้งการซื้อกิจการ'
        msg, included = sd.render([event], ['SEC แบบ 59'])
        self.assertIn('TEST — แจ้งการซื้อกิจการ\n' + event.url, msg)
        self.assertNotIn(event.facts, msg)
        self.assertNotIn(event.follow_up, msg)
        self.assertNotIn(event.category, msg)
        self.assertEqual(included, [event])

    def test_recent_news_precedes_older_high_priority_news(self):
        old, recent = sample('old', priority=100), sample('recent', priority=70)
        old.published = date(2026, 10, 1)
        self.assertEqual(sd.select_events([old, recent], {'sent': {}}), [recent, old])


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.json'

    def tearDown(self):
        self.tmp.cleanup()

    @patch('stock_digest.collect', return_value=([sample()], []))
    def test_failed_send_does_not_mark_news_as_sent(self, collect):
        with self.assertRaises(RuntimeError):
            sd.run_digest(Mock(return_value=False), state_path=self.path)
        self.assertFalse(self.path.exists())

    @patch('stock_digest.collect', return_value=([sample()], []))
    def test_successful_send_persists_and_retry_is_silent(self, collect):
        send = Mock(return_value=True)
        sd.run_digest(send, state_path=self.path)
        sd.run_digest(send, state_path=self.path)
        self.assertEqual(send.call_count, 1)
        self.assertIn('a', sd.load_state(self.path)['sent'])

    @patch('stock_digest.collect', return_value=([sample()], []))
    def test_preview_has_no_side_effects(self, collect):
        send = Mock()
        sd.run_digest(send, dry_run=True, state_path=self.path)
        send.assert_not_called()
        self.assertFalse(self.path.exists())

    @patch('stock_digest.collect', return_value=([], ['SEC แบบ 59']))
    def test_failed_sources_raise_without_sending_or_advancing_state(self, collect):
        send = Mock(return_value=True)
        with self.assertRaises(RuntimeError):
            sd.run_digest(send, state_path=self.path)
        send.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_corrupt_state_refuses_resend(self):
        self.path.write_text('{"sent": []}', encoding='utf-8')
        with self.assertRaises(ValueError):
            sd.load_state(self.path)


if __name__ == '__main__':
    unittest.main()
