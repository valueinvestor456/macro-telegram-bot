"""Verified Thai disclosures, deterministic selection, and delivery state.

Gapfocus is discovery only. Company facts come from the linked SET page or
SEC tables. No LLM, target-price guesses, or inferred portfolio holdings.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup
from curl_cffi import requests

BANGKOK = timezone(timedelta(hours=7))
R59 = 'https://market.sec.or.th/public/idisc/th/r59'
R246 = 'https://market.sec.or.th/public/idisc/th/r246'
GAP = 'https://stock.gapfocus.com/'
STATE_PATH = Path(__file__).with_name('stock_digest_state.json')
MONTHS = ['ม.ค.', 'ก.พ.', 'มี.ค.', 'เม.ย.', 'พ.ค.', 'มิ.ย.',
          'ก.ค.', 'ส.ค.', 'ก.ย.', 'ต.ค.', 'พ.ย.', 'ธ.ค.']

# These are screening rules, not investment recommendations.
CATEGORIES = [
    ('ความเสี่ยงการเงิน', ('ผิดนัด', 'ฟื้นฟูกิจการ', 'ล้มละลาย', 'เพิกถอน'),
     'ตรวจภาระหนี้ กำหนดชำระ และความเสี่ยงต่อผู้ถือหุ้นก่อนประเมินมูลค่า', 100),
    ('ทุนและโครงสร้างหุ้น', ('เพิ่มทุน', 'ลดทุน', 'หุ้นกู้แปลงสภาพ', 'ซื้อหุ้นคืน'),
     'ตรวจจำนวนหุ้นใหม่ ราคา เงื่อนไข และผลต่อสัดส่วนถือหุ้น/กำไรต่อหุ้น', 90),
    ('ซื้อขายกิจการ/ลงทุน', ('บริษัทย่อย', 'ซื้อกิจการ', 'จำหน่ายเงินลงทุน', 'ร่วมทุน', 'ควบรวม'),
     'ตรวจราคาดีล แหล่งเงินทุน สัดส่วนลงทุน และกำหนดเริ่มรับรู้ผลประกอบการ', 85),
    ('ผลประกอบการ', ('งบการเงิน', 'ผลการดำเนินงาน', 'คำอธิบายและการวิเคราะห์'),
     'เทียบกำไรปกติ กระแสเงินสด และหนี้กับงวดก่อน แยกรายการพิเศษ', 80),
    ('ผู้บริหารและธุรกิจ', ('กรรมการผู้จัดการ', 'ประธานเจ้าหน้าที่', 'ลาออก', 'แต่งตั้ง',
                          'สัญญา', 'สัมปทาน', 'ใบอนุญาต', 'ปรับโครงสร้าง'),
     'ตรวจวันมีผล ขอบเขตหน้าที่/สัญญา และความต่อเนื่องของแผนธุรกิจ', 75),
]


def text(node):
    return re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip()


def thai_date(value: str) -> date:
    numeric = re.search(r'(\d{1,2})/(\d{1,2})/(\d{4})', value)
    if numeric:
        day, month, year = map(int, numeric.groups())
    else:
        match = re.search(r'(\d{1,2})\s+(' + '|'.join(map(re.escape, MONTHS)) + r')\s+(\d{4})', value)
        if not match:
            raise ValueError('Source date missing')
        day, month, year = int(match[1]), MONTHS.index(match[2]) + 1, int(match[3])
    return date(year - 543 if year > 2400 else year, month, day)


def number(value):
    try:
        result = Decimal(value.replace(',', '').strip())
        return result if result.is_finite() and result >= 0 else None
    except InvalidOperation:
        return None


def key(*values):
    return hashlib.sha256('|'.join(map(str, values)).encode()).hexdigest()[:32]


def canonical_url(url):
    parts = urlparse(url)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
        raise ValueError('Invalid source URL')
    query = parse_qs(parts.query)
    query = {k: query[k] for k in sorted(query) if not k.lower().startswith('utm_')}
    return urlunparse((parts.scheme, parts.netloc.lower(), parts.path, '', urlencode(query, doseq=True), ''))


def fetch(url):
    # Browser-compatible TLS is required by SEC; ordinary urllib connections
    # are closed by the server on some networks. Certificate checks stay on.
    response = requests.get(url, impersonate='chrome', timeout=25,
                            headers={'Accept-Language': 'th-TH,th;q=0.9,en;q=0.8'})
    response.raise_for_status()
    return response.text


@dataclass
class Event:
    id: str
    ticker: str
    category: str
    published: date
    facts: str
    follow_up: str
    url: str
    priority: int
    value: Decimal = Decimal(0)


def sec_table(html, table_id, expected, today):
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find('table', id=table_id)
    heading = next((text(h) for h in soup.select('h4') if 'ข้อมูลประจำวันที่' in text(h)), '')
    published = thai_date(heading)
    if not 0 <= (today - published).days <= 7:
        raise ValueError('SEC publication date stale or in future')
    if table is None:
        raise ValueError('SEC table missing')
    headers = [re.sub(r'\s+', '', text(h)) for h in table.select('th')]
    if headers != expected:
        raise ValueError('SEC columns changed')
    return table, published


R59_HEADERS = ['ชื่อบริษัท', 'ชื่อผู้บริหาร', 'ความสัมพันธ์*', 'ประเภทหลักทรัพย์',
               'วันที่ได้มา/จำหน่าย', 'จำนวน', 'ราคา', 'วิธีการได้มา/จำหน่าย', 'หมายเหตุ']
R246_HEADERS = ['หลักทรัพย์', 'ชื่อผู้ได้มา/จำหน่าย', 'วิธีการ', 'ประเภทหลักทรัพย์1',
                '%ก่อนได้มา/จำหน่าย', '%ได้มา/จำหน่าย', '%หลังได้มา/จำหน่าย', 'วันที่ได้มา/จำหน่าย',
                '%ก่อนได้มา/จำหน่าย(กลุ่ม)2', '%ได้มา/จำหน่าย(กลุ่ม)2',
                '%หลังได้มา/จำหน่าย(กลุ่ม)2', 'หมายเหตุ3', 'PDF', 'หมายเลข']


def parse_r59(html, today):
    table, published = sec_table(html, 'gPP09T01', R59_HEADERS, today)
    events = []
    for row in table.select('tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) != 9:
            continue
        values = list(map(text, cells))
        if re.search(r'revoked|ยกเลิก', ' '.join(values), re.I):
            continue
        ticker = re.search(r'\(([A-Z0-9&.\-]+)\)\s*$', values[0])
        link = cells[8].find('a', href=True)
        qty, price = number(values[5]), number(values[6])
        if not ticker or not link or values[3] != 'หุ้นสามัญ' or values[7] not in ('ซื้อ', 'ขาย'):
            continue
        if qty is None or price is None or qty <= 0 or price <= 0:
            continue
        trans_date = thai_date(values[4])
        if not 0 <= (published - trans_date).days <= 31:
            continue
        url = canonical_url(link['href'])
        if urlparse(url).hostname != 'market.sec.or.th' or not urlparse(url).path.startswith('/r59/'):
            continue
        event_id = key('r59', url)
        facts = (f'{values[1]} | {values[2]}\n'
                 f'{values[7]} {qty:,.0f} หุ้น × {price:,.2f} บาท ≈ {qty * price:,.0f} บาท\n'
                 f'ทำรายการ {trans_date:%d/%m/%Y}; SEC เผยแพร่ {published:%d/%m/%Y}')
        events.append(Event(event_id, ticker[1], 'ผู้บริหาร/บุคคลเกี่ยวข้อง — แบบ 59', published,
                            facts, 'ตรวจสัดส่วนเทียบหุ้นที่ถือเดิมและรายการต่อเนื่อง; รายการนี้ไม่ยืนยันแนวโน้มราคา',
                            url, 40, qty * price))
    return events


def parse_r246(html, today):
    table, published = sec_table(html, 'gPP10T01', R246_HEADERS, today)
    events = []
    for row in table.select('tr'):
        cells = row.find_all('td', recursive=False)
        if len(cells) != 14:
            continue
        v = list(map(text, cells))
        link = cells[12].find('a', href=True)
        if not link or not re.fullmatch(r'[A-Z0-9&.\-]+', v[0]) or v[3] != 'หุ้น':
            continue
        before, change, after = map(number, v[4:7])
        if any(n is None or n > 100 for n in (before, change, after)):
            continue
        trans_date = thai_date(v[7])
        if not 0 <= (published - trans_date).days <= 31:
            continue
        url = canonical_url(link['href'])
        if urlparse(url).hostname not in ('web-r246-api.sec.or.th', 'market.sec.or.th'):
            continue
        facts = (f'{v[1]} — {v[2]}\n'
                 f'สัดส่วนรายบุคคล {before}% → {after}% (รายการ {change} จุดเปอร์เซ็นต์)\n'
                 f'ทำรายการ {trans_date:%d/%m/%Y}; SEC เผยแพร่ {published:%d/%m/%Y}')
        if v[8:11] != v[4:7] and all(number(x) is not None for x in v[8:11]):
            facts += f'\nสัดส่วนทั้งกลุ่ม {v[8]}% → {v[10]}%'
        if v[11]:
            facts += '\nหมายเหตุ SEC: ' + v[11]
        events.append(Event(key('r246', v[13], url), v[0], 'การได้มา/จำหน่ายหุ้น — แบบ 246-2',
                            published, facts, 'ตรวจตัวผู้ถือหุ้นและเงื่อนไขในเอกสาร; สัดส่วนเปลี่ยนยังไม่ยืนยันการเปลี่ยนอำนาจควบคุม',
                            url, 95, change))
    return events


def discover_set(html):
    soup = BeautifulSoup(html, 'html.parser')
    rows = soup.select('.talk-row')
    if not rows:
        raise ValueError('Gapfocus news layout missing')
    urls = []
    for row in rows:
        link = row.find('a', href=True)
        if not link:
            continue
        parts = urlparse(link['href'])
        if parts.hostname not in ('www.set.or.th', 'set.or.th') or parts.path != '/th/market/news-and-alert/newsdetails':
            continue
        if not parse_qs(parts.query).get('id'):
            continue
        url = canonical_url(link['href'])
        if url not in urls:
            urls.append(url)
    return urls[:12]


def parse_set(html, url, today):
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup.select('script, style'):
        node.decompose()
    body = text(soup)
    match = re.search(r'วันที่/เวลา\s+(.+?)\s+แชร์', body)
    if not match:
        raise ValueError('SET publication timestamp missing; page starts: ' + body[:180])
    published = thai_date(match[1])
    if not 0 <= (today - published).days <= 3:
        return None
    heading = re.search(r'หัวข้อข่าว\s+(.+?)\s+หลักทรัพย์\s+([A-Z0-9&.\-]+)\s+แหล่งข่าว', body)
    if not heading:
        raise ValueError('SET title/symbol missing')
    title, ticker = heading.groups()
    for category, terms, follow_up, priority in CATEGORIES:
        if any(term in title for term in terms):
            # Preserve the confirmed headline, without pretending to have read
            # the PDF attachment or assigning a speculative positive/negative impact.
            return Event(key('set', parse_qs(urlparse(url).query)['id'][0]), ticker,
                         category, published, f'หัวข้อประกาศ: {title}\nเผยแพร่ {match[1]} (ไทย)',
                         follow_up + '; ยังไม่ได้สรุปตัวเลขจากเอกสารแนบ', url, priority)
    return None


def collect(today=None):
    today = today or datetime.now(BANGKOK).date()
    events, errors = [], []
    def source(name, url, parser):
        try:
            return parser(fetch(url), today), None
        except Exception as exc:
            print(f'[stocks] {name}: {type(exc).__name__}')
            return [], name
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = [pool.submit(source, 'SEC แบบ 59', R59, parse_r59),
                pool.submit(source, 'SEC แบบ 246-2', R246, parse_r246)]
        try:
            urls = discover_set(fetch(GAP))
            for url in urls:
                try:
                    event = parse_set(fetch(url), url, today)
                    if event:
                        events.append(event)
                except Exception as exc:
                    print(f'[stocks] SET detail: {type(exc).__name__}: {exc}')
                    errors.append('ประกาศ SET บางรายการ')
        except Exception as exc:
            print(f'[stocks] SET discovery: {type(exc).__name__}')
            errors.append('ค้นประกาศ SET ผ่าน Gapfocus')
        for job in jobs:
            found, error = job.result()
            events.extend(found)
            if error:
                errors.append(error)
    return events, sorted(set(errors))


def load_state(path=STATE_PATH):
    if not path.exists():
        return {'sent': {}}
    state = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(state, dict) or not isinstance(state.get('sent'), dict):
        raise ValueError('Invalid stock digest state; refusing to resend')
    for identifier, timestamp in state['sent'].items():
        if not isinstance(identifier, str) or not isinstance(timestamp, str):
            raise ValueError('Invalid stock digest state entry')
        date.fromisoformat(timestamp)
    return state


def save_state(state, path=STATE_PATH):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def select_events(events, state, watchlist=(), minimum=Decimal('1000000')):
    unique = {}
    for event in events:
        if event.id in state['sent'] or event.id in unique:
            continue
        if event.category.endswith('แบบ 59') and event.value < minimum and event.ticker not in watchlist:
            continue
        unique[event.id] = event
    return sorted(unique.values(), key=lambda e: (e.ticker in watchlist, e.priority, e.published, e.value), reverse=True)


def utf16_size(value):
    return len(value.encode('utf-16-le')) // 2


def render(events, errors, now=None, watchlist=()):
    now = (now or datetime.now(BANGKOK)).astimezone(BANGKOK)
    blocks = [f'📈 Thai Stock Updates — {now:%d/%m/%Y %H:%M} (ไทย)',
              'คัดประกาศบริษัท • ผู้ถือหุ้น • รายการผู้บริหาร\nรายการใหม่ที่ตรวจพบ ไม่ใช่ข่าวทั้งหมดของตลาด']
    if errors:
        blocks.append('⚠️ ข้อมูลไม่ครบ: ' + ', '.join(errors) + ' — ดึง/ตรวจข้อมูลไม่สำเร็จ')
    selected = []
    counts = {}
    for event in events:
        if len(selected) >= 5:
            break
        if counts.get(event.ticker, 0) >= 2:
            continue
        star = '⭐ ' if event.ticker in watchlist else ''
        block = (f'{len(selected)+1}. {star}{event.ticker} | {event.category}\n'
                 f'{event.facts}\nติดตาม: {event.follow_up}\nเอกสาร: {event.url}')
        if utf16_size('\n\n'.join(blocks + [block])) > 3700:
            continue
        blocks.append(block)
        selected.append(event)
        counts[event.ticker] = counts.get(event.ticker, 0) + 1
    if not selected and not errors:
        return '', []
    if not selected:
        blocks.append('ยังสรุปว่าไม่มีข่าวสำคัญไม่ได้ กรุณาตรวจแหล่งต้นทาง')
    return '\n\n'.join(blocks), selected


def run_digest(send=None, *, dry_run=False, state_path=STATE_PATH):
    now = datetime.now(BANGKOK)
    state = load_state(state_path)
    minimum = number(os.environ.get('STOCK_MIN_INSIDER_VALUE', '1000000'))
    if minimum is None:
        raise ValueError('STOCK_MIN_INSIDER_VALUE must be non-negative')
    watchlist = set(filter(None, re.split(r'[\s,]+', os.environ.get('STOCK_WATCHLIST', '').upper())))
    events, errors = collect(now.date())
    selected = select_events(events, state, watchlist, minimum)
    message, included = render(selected, errors, now, watchlist)
    print(f'[stocks] parsed={len(events)} selected={len(included)} errors={len(errors)}')
    if dry_run:
        print(message or '[stocks] No new qualifying items; nothing to send')
        return message
    if not message:
        return ''
    # Health-only notification at most once a day; it never marks news as sent.
    health_id = key('health', now.date(), *errors)
    if not included and health_id in state['sent']:
        return ''
    if send is None or not send(message):
        raise RuntimeError('Telegram delivery not confirmed; state not advanced')
    for event in included:
        state['sent'][event.id] = now.date().isoformat()
    if errors:
        state['sent'][health_id] = now.date().isoformat()
    cutoff = now.date() - timedelta(days=60)
    state['sent'] = {k: v for k, v in state['sent'].items() if date.fromisoformat(v) >= cutoff}
    save_state(state, state_path)
    return message
