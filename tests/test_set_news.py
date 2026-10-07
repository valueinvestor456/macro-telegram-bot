from datetime import date
from unittest import TestCase
from unittest.mock import MagicMock, Mock, patch

import set_news as sn
import stock_digest as sd

TODAY = date(2026, 10, 7)


def item(i='1', symbol='TEST', headline='แจ้งการเปลี่ยนแปลงประธานเจ้าหน้าที่บริหาร'):
    return {'id': i, 'symbol': symbol, 'headline': headline, 'datetime': '2026-10-06T17:00:00+07:00'}


class FeedTests(TestCase):
    def test_all_pages_are_read_and_multisymbol_rows_preserved(self):
        session = Mock()
        a, b = Mock(), Mock()
        a.json.return_value = {'paginateNews': {'totalCount': 3, 'newsInfoList': [item('1'), item('2')]}}
        b.json.return_value = {'paginateNews': {'totalCount': 3, 'newsInfoList': [item('2', 'OTHER')]}}
        session.get.side_effect = [a, b]
        with patch.object(sn, 'PAGE_SIZE', 2):
            rows = sn.fetch_listing(TODAY, session)
        self.assertEqual(len(rows), 3)
        self.assertEqual(session.get.call_args_list[1].kwargs['params']['page'], 1)
        self.assertEqual(session.get.call_args_list[0].kwargs['params']['fromDate'], '30/09/2026')

    def test_schema_change_not_silently_empty(self):
        session = Mock()
        session.get.return_value.json.return_value = {'error': 'blocked'}
        with self.assertRaises(ValueError):
            sn.fetch_listing(TODAY, session)

    def test_stuck_pagination_fails(self):
        session = Mock()
        session.get.return_value.json.return_value = {'paginateNews': {'totalCount': 3, 'newsInfoList': [item()]}}
        with patch.object(sn, 'PAGE_SIZE', 1), self.assertRaises(ValueError):
            sn.fetch_listing(TODAY, session)

    def test_empty_valid_feed_is_not_a_failure(self):
        session = Mock()
        session.get.return_value.json.return_value = {'paginateNews': {'totalCount': 0, 'newsInfoList': []}}
        self.assertEqual(sn.fetch_listing(TODAY, session), [])

    def test_collect_only_requests_listing_and_skips_previously_sent_news(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.get.return_value.json.return_value = {'paginateNews': {
            'totalCount': 2, 'newsInfoList': [item('1'), item('2')]}}
        with patch.object(sn, 'api_session', return_value=session):
            events, errors = sn.collect_set(TODAY, {sd.key('set', '1')})
        self.assertEqual([e.id for e in events], [sd.key('set', '2')])
        self.assertEqual(errors, [])
        session.get.assert_called_once()
        self.assertEqual(session.get.call_args.args[0], sn.NEWS_API)
        self.assertIn('newsdetails?id=2&symbol=TEST', events[0].url)
        self.assertEqual(events[0].headline, item()['headline'])


class HeadlineTests(TestCase):
    def test_investment_events_qualify(self):
        for title in ['แจ้งการเปลี่ยนแปลงประธานเจ้าหน้าที่บริหาร',
                      'แต่งตั้งผู้รับผิดชอบสูงสุดในสายงานบัญชีและการเงิน',
                      'แจ้งการเปลี่ยนแปลงผู้ถือหุ้นใหญ่', 'คำเสนอซื้อหลักทรัพย์',
                      'มติคณะกรรมการอนุมัติการเข้าซื้อกิจการ', 'จัดตั้งบริษัทย่อยแห่งใหม่',
                      'การปรับโครงสร้างธุรกิจ', 'การเพิ่มทุนแบบเฉพาะเจาะจง',
                      'ลงนามสัญญาร่วมทุน', 'แจ้งการผิดนัดชำระหนี้',
                      'อนุมัติโครงการซื้อหุ้นคืน']:
            with self.subTest(title=title):
                self.assertIsNotNone(sn.classify_headline(title))

    def test_routine_and_unspecific_disclosures_excluded(self):
        for title in ['แบบรายงานผลการซื้อหุ้นคืนกรณีเพื่อการบริหารทางการเงิน',
                      'ขอเสนอชื่อบุคคลเพื่อเลือกตั้งเป็นประธานกรรมการ',
                      'รายงานการใช้เงินเพิ่มทุน', 'แจ้งวันหยุด',
                      'SEC News : สรุปแบบ 59 ประจำวันที่ 5 ตุลาคม 2569',
                      'แจ้งมติคณะกรรมการ', 'แต่งตั้งกรรมการตรวจสอบ',
                      'บริษัทไม่ได้ผิดนัดชำระหนี้', 'บริษัทไม่ผิดนัดชำระหนี้']:
            with self.subTest(title=title):
                self.assertIsNone(sn.classify_headline(title))

    def test_date_uses_bangkok_and_rejects_stale_future_or_naive_dates(self):
        row = item()
        row['datetime'] = '2026-10-06T18:00:00Z'
        self.assertEqual(sn.event_from_row(row, TODAY).published, TODAY)
        for stamp in ['2026-09-29T12:00:00+07:00', '2026-10-08T12:00:00+07:00']:
            row['datetime'] = stamp
            self.assertIsNone(sn.event_from_row(row, TODAY))
        row['datetime'] = '2026-10-06T17:00:00'
        with self.assertRaises(ValueError):
            sn.event_from_row(row, TODAY)
