"""Build and send a source-grounded Thai world-economy morning brief."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import escape, unescape
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BANGKOK = ZoneInfo("Asia/Bangkok")
NEWS_WINDOW = timedelta(hours=24)
MAX_NEWS = 36
MAX_MESSAGE = 4096
CALENDAR_URL = "https://usd.deepsleep456.com/calendar.json"
FEEDS = (
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss"),
    ("Bloomberg Economics", "https://feeds.bloomberg.com/economics/news.rss"),
    ("Fed", "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("ECB", "https://www.ecb.europa.eu/rss/press.html"),
)
HEADERS = {"User-Agent": "MacroMorningBrief/1.0 (+https://github.com/valueinvestor456/macro-telegram-bot)"}
ALLOWED_TAGS = {
    "#หนุนทองคำ", "#กดดันทองคำ",
    "#หนุนดอลลาร์", "#กดดันดอลลาร์",
    "#หนุนบาท", "#กดดันบาท",
    "#บาทอ่อน", "#บาทแข็ง",
}
TAG_ALIASES = {
    "#หนุนดอลลาร์ บาทอ่อน": "#บาทอ่อน",
    "#กดดันดอลลาร์ บาทแข็ง": "#บาทแข็ง",
    "#หนุนบาท บาทแข็ง": "#บาทแข็ง",
    "#กดดันบาท บาทอ่อน": "#บาทอ่อน",
}


def clean(value: str | None, limit: int = 1200) -> str:
    if not value:
        return ""
    text = BeautifulSoup(unescape(value), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def feed_date(item: ET.Element) -> datetime | None:
    for element in item.iter():
        if local_name(element.tag) not in {"pubdate", "published", "updated", "date"} or not element.text:
            continue
        raw = element.text.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError):
                continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def feed_items(source: str, content: bytes, since: datetime) -> list[dict]:
    root = ET.fromstring(content)
    items = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
    parsed = []
    for node in items:
        values: dict[str, str] = {}
        for child in node:
            key = local_name(child.tag)
            if key == "link":
                values[key] = child.attrib.get("href") or child.text or ""
            elif key in {"title", "description", "summary", "content:encoded", "encoded"}:
                values[key] = " ".join(child.itertext())
        title = clean(values.get("title"), 300)
        url = values.get("link", "").strip()
        published = feed_date(node)
        if not title or not url.startswith("https://") or not published or not (since <= published <= datetime.now(timezone.utc) + timedelta(minutes=5)):
            continue
        description = clean(values.get("description") or values.get("summary") or values.get("encoded"), 900)
        parsed.append({"id": "", "source": source, "title": title, "description": description, "url": url,
                       "published": published.isoformat()})
    return parsed


def fetch_news(now: datetime) -> list[dict]:
    since = now.astimezone(timezone.utc) - NEWS_WINDOW
    candidates: dict[str, dict] = {}
    succeeded = 0
    session = requests.Session()
    session.headers.update(HEADERS)
    for source, url in FEEDS:
        try:
            response = session.get(url, timeout=(5, 12))
            response.raise_for_status()
            succeeded += 1
            for item in feed_items(source, response.content, since):
                candidates.setdefault(item["url"], item)
        except Exception as exc:
            print(f"[brief] feed unavailable: {source} ({type(exc).__name__})", file=sys.stderr)
    if not succeeded:
        raise RuntimeError("All configured news feeds are unavailable; refusing to send an empty-news brief")
    ranked = sorted(candidates.values(), key=lambda item: item["published"], reverse=True)[:MAX_NEWS]
    for index, item in enumerate(ranked, 1):
        item["id"] = f"n{index}"
    return ranked


def fetch_calendar(now: datetime) -> list[dict]:
    response = requests.get(CALENDAR_URL, headers=HEADERS, timeout=(5, 15))
    response.raise_for_status()
    payload = response.json()
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise ValueError("Forex Factory calendar response has no events")
    today = now.astimezone(BANGKOK).date()
    selected = []
    for event in events:
        if not isinstance(event, dict):
            continue
        try:
            when = datetime.fromisoformat(str(event.get("date", "")).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            when = when.astimezone(BANGKOK)
        except ValueError:
            continue
        if when.date() != today:
            continue
        if event.get("impact") not in {"High", "Medium"}:
            continue
        selected.append({
            "time": when.strftime("%H:%M"), "currency": clean(str(event.get("country", "—")), 8),
            "title": clean(str(event.get("title", "")), 100), "impact": event.get("impact", ""),
            "forecast": clean(str(event.get("forecast", "—")), 24) or "—",
            "previous": clean(str(event.get("previous", "—")), 24) or "—",
        })
    return sorted(selected, key=lambda event: event["time"])


def summarize(news: list[dict], now: datetime) -> list[dict]:
    """Select relevant RSS items and use source-provided descriptions only; no LLM or API key."""
    relevant_terms = (
        "econom", "inflation", "cpi", "pce", "employment", "payroll", "unemployment", "gdp", "growth",
        "interest rate", "rates", "treasury", "bond", "yield", "dollar", "euro", "currency", "forex",
        "gold", "oil", "trade", "tariff", "central bank", "fed", "ecb", "monetary", "recession", "market",
    )
    ranked = []
    for item in news:
        title = clean(item.get("title", ""), 300)
        description = clean(item.get("description", ""), 900)
        source = item.get("source", "")
        body = f"{title} {description}".lower()
        if not any(term in body for term in relevant_terms) and source not in {"Fed", "ECB"}:
            continue
        tags = []
        tag_rules = (
            (("gold rises", "gold gains", "gold climbs", "gold rallies", "gold advances", "gold jumps"),
             ("gold falls", "gold drops", "gold slides", "gold declines", "gold sinks", "gold tumbles"),
             "#หนุนทองคำ", "#กดดันทองคำ"),
            (("dollar rises", "dollar gains", "dollar strengthens", "dollar climbs", "dollar firms", "greenback gains"),
             ("dollar falls", "dollar drops", "dollar weakens", "dollar slides", "dollar declines", "greenback falls"),
             "#บาทอ่อน", "#บาทแข็ง"),
            (("baht strengthens", "baht gains", "baht appreciates", "baht firms"),
             ("baht weakens", "baht falls", "baht depreciates", "baht loses"),
             "#บาทแข็ง", "#บาทอ่อน"),
        )
        for positive, negative, positive_tag, negative_tag in tag_rules:
            has_positive = any(term in body for term in positive)
            has_negative = any(term in body for term in negative)
            if has_positive != has_negative:
                tags.append(positive_tag if has_positive else negative_tag)
        ranked.append({
            **item,
            "title": title,
            "summary": description or "RSS ไม่มีคำอธิบายเพิ่มเติม โปรดเปิดลิงก์ต้นฉบับ",
            "tags": tags,
        })
    ranked.sort(key=lambda item: item.get("published", ""), reverse=True)
    return ranked[:6]


def translate_and_analyze(items: list[dict], events: list[dict]) -> dict:
    """Create the Thai news brief, calendar descriptions, and asset outlook with Gemini."""
    if not items and not events:
        return {"news": [], "events": [], "outlook": []}
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY secret is not configured")

    source_items = [{
        "id": item["id"], "source": item["source"], "title": item["title"],
        "description": item["description"], "detected_tags": item.get("tags", []),
    } for item in items]
    source_events = [{"id": f"e{index}", **event} for index, event in enumerate(events)]
    prompt = (
        "คุณเป็นบรรณาธิการข่าวเศรษฐกิจสำหรับสรุป Telegram ภาษาไทย "
        "เขียนรายงานตามรูปแบบ BREAKING NEWS & ECONOMIC UPDATE "
        "แปลข่าวและปฏิทินเป็นภาษาไทยทั้งหมด กระชับ ชัดเจน ใช้สำนวนข่าวการเงิน "
        "ยึดเฉพาะข้อมูลใน RSS และปฏิทิน ห้ามแต่งตัวเลข เหตุการณ์ คำพูด ราคา "
        "แนวรับ/แนวต้าน หรือถ้อยแถลง หากข้อมูลไม่พอให้ระบุว่าไม่มีข้อมูลพอยืนยัน "
        "ห้ามคาดเดาหรือฟันธงผลตลาด\n"
        "คืน JSON ตาม schema นี้เท่านั้น: {\"news\":[{\"id\":string,"
        "\"title_th\":string,\"summary_th\":string,\"tags\":[string]}],"
        "\"events\":[{\"id\":string,\"title_th\":string,\"effect_th\":string,"
        "\"tags\":[string]}],\"outlook\":[{\"asset\":\"USD\"|\"Gold\"|\"THB\","
        "\"summary_th\":string,\"tags\":[string]}]}\n"
        "คืนข่าวทุกชิ้นโดยรักษา id ให้ตรง; พาดหัวไทยชัดเจน; summary_th ข่าวละ 2-3 ประโยค "
        "ไม่เกิน 400 ตัวอักษร. คืนรายการปฏิทินทุกชิ้นตาม id เดิม; effect_th สั้นๆ "
        "อธิบายว่าตัวเลขนั้นอาจสำคัญต่ออะไร ห้ามระบุทิศทางผลลัพธ์ล่วงหน้า. "
        "outlook ต้องมีครบ USD, Gold, THB สรุปภาพรวมจากข่าวชุดนี้เท่านั้น "
        "ไม่สร้างระดับราคา. ให้ THB outlook วิเคราะห์ผลสุทธิของข่าวต่อเงินบาทอย่างชัดเจน "
        "เริ่ม summary_th ของ THB ด้วย 'เงินบาทระยะสั้นมีแนวโน้มอ่อนค่า' หรือ "
        "'เงินบาทระยะสั้นมีแนวโน้มแข็งค่า' ตามน้ำหนักหลักฐานในข่าว "
        "โดยระยะสั้นหมายถึงวันนี้ถึง 24 ชั่วโมงข้างหน้า ไม่ใช่การรับประกันอนาคต. "
        "ตามด้วยเหตุผลหลัก 1-2 ข้อ และเงื่อนไขจากปฏิทินวันนี้ที่อาจเปลี่ยนมุมมอง "
        "เขียนแบบมีเงื่อนไข ห้ามเดาผลประกาศหรือแต่งค่าคาดการณ์. "
        "ถ้ามีหลักฐานทิศทาง ให้ใส่แท็ก #บาทอ่อน หรือ #บาทแข็ง เพียงหนึ่งแท็ก "
        "พร้อมอธิบายเหตุผลจากข่าว; ถ้าหลักฐานขัดแย้งหรือไม่พอ ห้ามเดาและให้ tags เป็น [] "
        "พร้อมระบุใน summary_th ว่ายังไม่มีข้อมูลชี้ทิศทางเงินบาท. "
        "ข่าวแต่ละชิ้นให้ใส่แท็กเงินบาทเมื่อมีหลักฐานเฉพาะในข่าวเท่านั้น. "
        "ทุก tags เลือกได้เฉพาะจาก "
        "#หนุนทองคำ #กดดันทองคำ #หนุนดอลลาร์ #กดดันดอลลาร์ #หนุนบาท #กดดันบาท #บาทอ่อน #บาทแข็ง "
        "และให้ [] เมื่อหลักฐานไม่ชัดเจน. แท็กสะท้อนผลกระทบเบื้องต้น ไม่ใช่คำแนะนำลงทุน.\n"
        f"ข่าว RSS: {json.dumps(source_items, ensure_ascii=False)}\n"
        f"Forex Factory วันนี้: {json.dumps(source_events, ensure_ascii=False)}"
    )
    response = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent",
        headers={"x-goog-api-key": api_key, **HEADERS},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}},
        timeout=(10, 45),
    )
    if not response.ok:
        # Do not include response bodies: API errors may echo sensitive request details.
        raise RuntimeError(f"Gemini API returned HTTP {response.status_code}")
    try:
        content = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        generated = json.loads(content)
        generated_news = generated["news"]
        generated_events = generated["events"]
        generated_outlook = generated["outlook"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("Gemini returned an invalid Thai brief response") from exc

    by_id = {entry.get("id"): entry for entry in generated_news if isinstance(entry, dict)}
    thai_text = lambda value: isinstance(value, str) and any("\u0e00" <= char <= "\u0e7f" for char in value)
    thai_news = []
    for item in items:
        translated = by_id.get(item["id"])
        if not translated:
            raise RuntimeError("Gemini omitted a news item; refusing to send an incomplete brief")
        title = clean(str(translated.get("title_th", "")), 240)
        summary = clean(str(translated.get("summary_th", "")), 600)
        tags = translated.get("tags", [])
        if not title or not summary or not thai_text(title) or not thai_text(summary) or not isinstance(tags, list):
            raise RuntimeError("Gemini returned an incomplete Thai news item")
        normalized_tags = [
            TAG_ALIASES.get(tag, tag) for tag in tags if isinstance(tag, str)
        ]
        normalized_tags = [tag for tag in normalized_tags if tag in ALLOWED_TAGS]
        for tag in item.get("tags", []):
            tag = TAG_ALIASES.get(tag, tag)
            if tag in ALLOWED_TAGS and tag not in normalized_tags:
                normalized_tags.append(tag)
        thai_news.append({**item, "title": title, "summary": summary,
                          "tags": normalized_tags})

    translated_events = {entry.get("id"): entry for entry in generated_events if isinstance(entry, dict)}
    thai_events = []
    for index, event in enumerate(events):
        translated = translated_events.get(f"e{index}")
        if not translated or not thai_text(translated.get("title_th")) or not thai_text(translated.get("effect_th")):
            raise RuntimeError("Gemini returned an incomplete Thai calendar item")
        tags = translated.get("tags", [])
        if not isinstance(tags, list):
            raise RuntimeError("Gemini returned invalid calendar impact tags")
        thai_events.append({**event, "title_th": clean(translated["title_th"], 100),
                            "effect_th": clean(translated["effect_th"], 100),
                            "tags": [TAG_ALIASES.get(tag, tag) for tag in tags
                                     if isinstance(tag, str) and TAG_ALIASES.get(tag, tag) in ALLOWED_TAGS]})

    outlook_by_asset = {entry.get("asset"): entry for entry in generated_outlook if isinstance(entry, dict)}
    detected_baht_directions = {
        tag
        for entry in [*thai_news, *thai_events]
        for tag in entry["tags"]
        if tag in {"#บาทอ่อน", "#บาทแข็ง"}
    }
    thai_outlook = []
    for asset in ("USD", "Gold", "THB"):
        outlook = outlook_by_asset.get(asset)
        if not outlook or not thai_text(outlook.get("summary_th")) or not isinstance(outlook.get("tags"), list):
            raise RuntimeError("Gemini returned an incomplete Thai asset outlook")
        tags = [TAG_ALIASES.get(tag, tag) for tag in outlook["tags"] if isinstance(tag, str)]
        tags = [tag for tag in tags if tag in ALLOWED_TAGS]
        if asset == "THB" and not (set(tags) & {"#บาทอ่อน", "#บาทแข็ง"}):
            if len(detected_baht_directions) == 1:
                tags.extend(detected_baht_directions)
        if asset == "THB" and len(set(tags) & {"#บาทอ่อน", "#บาทแข็ง"}) > 1:
            raise RuntimeError("Gemini returned conflicting baht direction tags")
        thai_outlook.append({"asset": asset, "summary": clean(outlook["summary_th"], 300),
                             "tags": tags})
    return {"news": thai_news, "events": thai_events, "outlook": thai_outlook}


def calendar_table(events: list[dict], total_count: int | None = None) -> str:
    if not events:
        if total_count:
            return f"ปฏิทินมี {total_count} รายการ; ย่อรายการเพราะข้อจำกัดความยาวข้อความ"
        return "วันนี้ไม่มีรายการประกาศระดับกลาง/สูงในปฏิทิน Forex Factory (เวลาไทย)"
    rows = ["เวลา | CCY | รายการ | สำคัญ | คาดการณ์ | ก่อนหน้า | ผลต่อตลาด"]
    for event in events[:14]:
        impact = "🔴สูง" if event["impact"] == "High" else "🟠กลาง"
        effect = event.get("effect_th", "")
        tags = " ".join(event.get("tags", []))
        impact_text = f"{effect} {tags}".strip()
        rows.append(f"{event['time']} | {event['currency']} | {event.get('title_th', event['title'])} | {impact} | {event['forecast']} | {event['previous']} | {impact_text}")
    total_count = total_count if total_count is not None else len(events)
    if total_count > len(events):
        rows.append(f"… แสดง {len(events)} จาก {total_count} รายการ")
    return "\n".join(rows)


def render(items: list[dict], events: list[dict], outlook: list[dict], now: datetime) -> str:
    thai_day = ("วันจันทร์", "วันอังคาร", "วันพุธ", "วันพฤหัสบดี", "วันศุกร์", "วันเสาร์", "วันอาทิตย์")[now.astimezone(BANGKOK).weekday()]
    local_date = now.astimezone(BANGKOK)
    thai_month = ("มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
                  "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม")[local_date.month - 1]
    date_text = f"{local_date.day} {thai_month} {local_date.year + 543}"
    lines = ["<b>BREAKING NEWS &amp; ECONOMIC UPDATE</b> ♨️", "", f"ประจำเช้า{thai_day}ที่ {date_text}", "",
             "<b>🚨 สรุปข่าวเด่นเศรษฐกิจ &amp; ผลตัวเลขสำคัญในรอบวันที่ผ่านมา (Forex Factory / CNBC / Bloomberg)</b>"]
    visible = list(items[:6])
    if not visible:
        lines.append("ไม่พบข่าวที่ยืนยันได้จากแหล่งข่าวในช่วง 24 ชั่วโมงที่ผ่านมา")
    def assemble(selected: list[dict], agenda: list[dict]) -> str:
        output = list(lines)
        if items and not selected:
            output.append("ข่าวที่คัดมาแสดงไม่ครบเนื่องจากข้อจำกัดความยาวข้อความ")
        for index, item in enumerate(selected, 1):
            source = escape(item["source"])
            title = escape(item["title"])
            summary = escape(item["summary"])
            url = escape(item["url"], quote=True)
            output.extend([f"\n<b>{index}️⃣ {title}</b>", summary,
                           " ".join(escape(tag) for tag in item["tags"]) or
                           "⚪ ข่าวนี้ยังไม่มีข้อมูลชี้ทิศทางเงินบาท",
                           f"แหล่งข่าว: <a href=\"{url}\">{source}</a>"])
        output.extend(["", f"<b>🗓️ ตารางประกาศตัวเลขเศรษฐกิจสำคัญวันนี้ ({local_date.day} {thai_month} {local_date.year + 543})</b>",
                       "<pre>" + escape(calendar_table(agenda, len(events))) + "</pre>",
                       "", "<b>💡 ภาพรวมผลกระทบต่อสินทรัพย์</b>"])
        asset_names = {"USD": "ดอลลาร์สหรัฐ (USD)", "Gold": "ทองคำ (Gold Spot)", "THB": "เงินบาท (USD/THB)"}
        outlook_by_asset = {entry["asset"]: entry for entry in outlook}
        for asset, display_name in asset_names.items():
            entry = outlook_by_asset.get(asset)
            if entry:
                tags = " ".join(escape(tag) for tag in entry["tags"])
                if asset == "THB" and not ({"#บาทอ่อน", "#บาทแข็ง"} & set(entry["tags"])):
                    tags = "⚪ ยังไม่มีข้อมูลชี้ทิศทางเงินบาท"
                if asset == "THB":
                    direction_tags = set(entry["tags"]) & {"#บาทอ่อน", "#บาทแข็ง"}
                    direction = ("เงินบาทมีแนวโน้มอ่อนค่า (USD/THB ขึ้น)" if direction_tags == {"#บาทอ่อน"}
                                 else "เงินบาทมีแนวโน้มแข็งค่า (USD/THB ลง)" if direction_tags == {"#บาทแข็ง"}
                                 else "ทิศทางเงินบาทยังไม่ชัดเจน")
                    output.extend(["", "<b>🇹🇭 แนวโน้มเงินบาท (USD/THB) วันนี้–24 ชั่วโมงข้างหน้า</b>",
                                   f"<b>{direction}</b>", escape(entry["summary"]), tags])
                else:
                    output.append(f"• <b>{display_name}:</b> {escape(entry['summary'])}" + (f" {tags}" if tags else ""))
            elif asset == "THB":
                output.append(f"• <b>{display_name}:</b> ⚪ ยังไม่มีข้อมูลชี้ทิศทางเงินบาท")
        output.extend([
                       "", "หมายเหตุ: สรุปและแปลเป็นไทยด้วย Gemini โดยอ้างอิงเฉพาะหัวข้อ/คำอธิบาย RSS; แท็กเป็นการประเมินผลกระทบเบื้องต้น",
                       "วิเคราะห์ตามข้อมูลที่เผยแพร่ ณ เวลาจัดทำ ไม่ใช่คำแนะนำลงทุน"])
        return "\n".join(output)

    text = assemble(visible, events)
    while len(text) > MAX_MESSAGE and len(visible) > 1:
        visible.pop()
        text = assemble(visible, events)
    agenda = list(events)
    while len(text) > MAX_MESSAGE and agenda:
        agenda.pop()
        text = assemble(visible, agenda)
    if len(text) > MAX_MESSAGE:
        raise ValueError(f"Telegram message too long: {len(text)} characters")
    return text


def send(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        raise RuntimeError("Telegram credentials are not configured")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=(10, 30),
    )
    response.raise_for_status()
    if response.json().get("ok") is not True:
        raise RuntimeError("Telegram did not confirm message delivery")


def main() -> int:
    now = datetime.now(timezone.utc)
    try:
        news = fetch_news(now)
        events = fetch_calendar(now)
        selected = summarize(news, now)
        analysis = translate_and_analyze(selected, events)
        text = render(analysis["news"], analysis["events"], analysis["outlook"], now)
        if os.environ.get("DAILY_BRIEF_DRY_RUN", "false").lower() == "true":
            print(BeautifulSoup(text, "html.parser").get_text())
            return 0
        send(text)
        print(f"[brief] Telegram confirmed at {now.astimezone(BANGKOK).isoformat()}")
        return 0
    except Exception as exc:
        print(f"[brief] failed: {type(exc).__name__}: {str(exc)[:180]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
