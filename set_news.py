"""Screen official SET headlines; never fetch news attachments or call an LLM."""
from datetime import datetime, timedelta
import re
from urllib.parse import urlencode
from curl_cffi import requests
from stock_digest import BANGKOK, Event, key

NEWS_PAGE = 'https://www.set.or.th/th/market/news-and-alert/news'
NEWS_API = 'https://www.set.or.th/api/cms/v1/news/set'
MAX_PAGES, PAGE_SIZE = 20, 100
HEADERS = {'Accept-Language': 'th-TH,th;q=0.9,en;q=0.8', 'Referer': NEWS_PAGE, 'x-channel': 'WEB_SET'}
LEADER = r'(?:ประธานเจ้าหน้าที่|กรรมการผู้จัดการ|ประธานกรรมการ|ผู้บริหาร|ผู้รับผิดชอบสูงสุด.{0,30}การเงิน|ceo|cfo)'
CHANGE = r'(?:แต่งตั้ง|เปลี่ยนแปลง|ลาออก|พ้นจาก|สิ้นสุดการดำรง)'
RULES = [
    ('ความเสี่ยงกิจการ', r'ผิดนัดชำระ|ฟื้นฟูกิจการ|ล้มละลาย|ถูกเพิกถอน|ยุติ.{0,20}ธุรกิจ|หยุด.{0,20}ประกอบกิจการ', 100),
    ('ผู้ถือหุ้นใหญ่', r'เปลี่ยนแปลง.{0,60}(?:ผู้ถือหุ้นใหญ่|อำนาจควบคุม)|คำเสนอซื้อหลักทรัพย์|tenderoffer', 95),
    ('ผู้บริหารสำคัญ', CHANGE + r'.{0,120}' + LEADER + '|' + LEADER + r'.{0,100}' + CHANGE, 94),
    ('ธุรกิจ/ซื้อขายกิจการ', r'บริษัทย่อย|ซื้อกิจการ|ควบรวม|ร่วมทุน|รายการ.{0,30}(?:ได้มา|จำหน่าย).{0,20}(?:สินทรัพย์|ทรัพย์สิน)|จำหน่ายเงินลงทุน|ขยาย.{0,25}ธุรกิจ|เปลี่ยน.{0,20}ธุรกิจ|ปรับโครงสร้าง.{0,20}ธุรกิจ|รายการที่เกี่ยวโยงกัน', 88),
    ('โครงสร้างทุน', r'เพิ่มทุน|ลดทุน|หุ้นกู้แปลงสภาพ|โครงการซื้อหุ้นคืน', 85),
    ('สัญญา/สิทธิประกอบธุรกิจ', r'ลงนาม.{0,40}สัญญา|ได้รับ.{0,40}(?:งาน|สัมปทาน|ใบอนุญาต)|ยกเลิก.{0,40}(?:สัญญา|สัมปทาน)|เพิกถอน.{0,20}ใบอนุญาต', 82),
]
ROUTINE = ('เสนอวาระ', 'เสนอชื่อบุคคล', 'เสนอชื่อกรรมการ', 'วันหยุด',
           'รายงานผลการซื้อหุ้นคืน', 'รายงานผลการจำหน่ายหุ้นซื้อคืน',
           'รายงานผลการใช้สิทธิ', 'รายงานการใช้เงิน', 'มูลค่าสินทรัพย์สุทธิ',
           'รายงานการประชุม', 'เผยแพร่หนังสือ', 'ส่งคำถาม', 'secnews')


def classify_headline(headline):
    value = re.sub(r'\s+', '', headline).lower()
    if any(term in value for term in ROUTINE):
        return None
    value = re.sub(r'(?:มิได้|ไม่ได้|ไม่|มิใช่)ผิดนัดชำระ', '', value)
    for category, pattern, priority in RULES:
        if re.search(pattern, value):
            return category, priority
    return None


def api_session():
    session = requests.Session(impersonate='chrome', headers=HEADERS)
    try:
        session.get(NEWS_PAGE, timeout=25).raise_for_status()
    except Exception:
        session.close()
        raise
    return session


def fetch_listing(today, session):
    found, keys = [], set()
    for page in range(MAX_PAGES):
        response = session.get(NEWS_API, params={
            'sourceId': 'company', 'securityTypeIds': 'S',
            'fromDate': (today - timedelta(days=7)).strftime('%d/%m/%Y'),
            'toDate': today.strftime('%d/%m/%Y'), 'page': page,
            'perPage': PAGE_SIZE, 'orderBy': 'date', 'lang': 'th',
        }, timeout=25)
        response.raise_for_status()
        data = response.json().get('paginateNews')
        if not isinstance(data, dict) or not isinstance(data.get('newsInfoList'), list):
            raise ValueError('SET news listing schema changed')
        rows, total = data['newsInfoList'], data.get('totalCount')
        if not isinstance(total, int) or total < 0:
            raise ValueError('SET count missing')
        before = len(keys)
        for row in rows:
            if not isinstance(row, dict) or not all(row.get(k) for k in ('id', 'symbol', 'headline', 'datetime')):
                raise ValueError('Incomplete SET news row')
            identity = (str(row['id']), row['symbol'])
            if identity not in keys:
                keys.add(identity)
                found.append(row)
        if (page + 1) * PAGE_SIZE >= total:
            return found
        if not rows or len(keys) == before:
            raise ValueError('SET pagination did not advance')
    raise ValueError('SET feed exceeds safe pagination limit')


def event_from_row(row, today):
    stamp = datetime.fromisoformat(row['datetime'])
    if stamp.tzinfo is None:
        raise ValueError('SET timestamp must have timezone')
    published = stamp.astimezone(BANGKOK).date()
    if not 0 <= (today - published).days <= 7:
        return None
    match = classify_headline(row['headline'])
    if match is None:
        return None
    category, priority = match
    url = 'https://www.set.or.th/th/market/news-and-alert/newsdetails?' + urlencode({'id': row['id'], 'symbol': row['symbol']})
    return Event(key('set', row['id']), row['symbol'], category, published,
                 row['headline'], '', url, priority, headline=row['headline'])


def collect_set(today, seen_ids=()):
    with api_session() as session:
        rows = fetch_listing(today, session)
    events = []
    for row in rows:
        if key('set', row['id']) in seen_ids:
            continue
        event = event_from_row(row, today)
        if event:
            events.append(event)
    print(f'[stocks] SET listed={len(rows)} screened={len(events)}')
    return events, []
