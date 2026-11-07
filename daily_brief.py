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
             "#หนุนทอง", "#กดดันทองคำ"),
            (("dollar rises", "dollar gains", "dollar strengthens", "dollar climbs", "dollar firms", "greenback gains"),
             ("dollar falls", "dollar drops", "dollar weakens", "dollar slides", "dollar declines", "greenback falls"),
             "#หนุนดอลลาร์", "#กดดันดอลลาร์"),
            (("baht strengthens", "baht gains", "baht appreciates", "baht firms"),
             ("baht weakens", "baht falls", "baht depreciates", "baht loses"),
             "#หนุนบาท", "#กดดันบาท"),
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


def translate_and_analyze(items: list[dict]) -> list[dict]:
    """Create concise, source-grounded Thai headlines, summaries, and impact tags with Gemini."""
    if not items:
        return []
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY secret is not configured")

    source_items = [{
        "id": item["id"], "source": item["source"], "title": item["title"],
        "description": item["description"],
    } for item in items]
    prompt = (
        "คุณเป็นบรรณาธิการข่าวเศรษฐกิจสำหรับสรุป Telegram ภาษาไทย "
        "เรียบเรียงข่าวต่อไปนี้เป็นภาษาไทยทั้งหมด กระชับ ชัดเจน เป็นข้อๆ "
        "ยึดเฉพาะข้อมูลในชื่อข่าวและคำอธิบาย ห้ามแต่งตัวเลข เหตุการณ์ หรือคำพูด "
        "หากข้อมูลไม่พอให้สรุปอย่างระมัดระวัง ห้ามคาดเดา/ฟันธงผลตลาด\n"
        "คืน JSON ตาม schema นี้เท่านั้น: {\"items\":[{\"id\":string,"
        "\"title_th\":string,\"summary_th\":string,\"tags\":[string]}]}\n"
        "รักษา id ให้ตรงกับต้นฉบับทุกชิ้น; title_th เป็นพาดหัวไทย; summary_th "
        "เป็น 1-2 ประโยคไม่เกิน 240 ตัวอักษร; tags เลือกได้เฉพาะจาก "
        "#หนุนทอง #กดดันทองคำ #หนุนดอลลาร์ #กดดันดอลลาร์ #หนุนบาท #กดดันบาท "
        "และให้ [] เมื่อทิศทางไม่ได้ระบุหรือหลักฐานไม่ชัดเจน. "
        "แท็กสะท้อนผลกระทบที่มีเหตุผลจากข้อมูล ไม่ใช่คำแนะนำลงทุน.\n"
        f"ข่าว: {json.dumps(source_items, ensure_ascii=False)}"
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
        generated_items = generated["items"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("Gemini returned an invalid Thai brief response") from exc

    by_id = {entry.get("id"): entry for entry in generated_items if isinstance(entry, dict)}
    allowed_tags = {"#หนุนทอง", "#กดดันทองคำ", "#หนุนดอลลาร์", "#กดดันดอลลาร์", "#หนุนบาท", "#กดดันบาท"}
    result = []
    for item in items:
        translated = by_id.get(item["id"])
        if not translated:
            raise RuntimeError("Gemini omitted a news item; refusing to send an incomplete brief")
        title = clean(str(translated.get("title_th", "")), 240)
        summary = clean(str(translated.get("summary_th", "")), 600)
        tags = translated.get("tags", [])
        is_thai = lambda value: any("\u0e00" <= char <= "\u0e7f" for char in value)
        if not title or not summary or not is_thai(title) or not is_thai(summary) or not isinstance(tags, list):
            raise RuntimeError("Gemini returned an incomplete Thai news item")
        result.append({**item, "title": title, "summary": summary,
                       "tags": [tag for tag in tags if isinstance(tag, str) and tag in allowed_tags]})
    return result


def calendar_table(events: list[dict], total_count: int | None = None) -> str:
    if not events:
        if total_count:
            return f"ปฏิทินมี {total_count} รายการ; ย่อรายการเพราะข้อจำกัดความยาวข้อความ"
        return "วันนี้ไม่มีรายการประกาศระดับกลาง/สูงในปฏิทิน Forex Factory (เวลาไทย)"
    rows = ["เวลา  CCY  รายการ | ผลกระทบ | คาดการณ์ | ก่อนหน้า"]
    for event in events[:14]:
        impact = "🔴สูง" if event["impact"] == "High" else "🟠กลาง"
        rows.append(f"{event['time']}  {event['currency']}  {event['title']} | {impact} | {event['forecast']} | {event['previous']}")
    total_count = total_count if total_count is not None else len(events)
    if total_count > len(events):
        rows.append(f"… แสดง {len(events)} จาก {total_count} รายการ")
    return "\n".join(rows)


def render(items: list[dict], events: list[dict], now: datetime) -> str:
    thai_day = ("วันจันทร์", "วันอังคาร", "วันพุธ", "วันพฤหัสบดี", "วันศุกร์", "วันเสาร์", "วันอาทิตย์")[now.astimezone(BANGKOK).weekday()]
    date_text = now.astimezone(BANGKOK).strftime("%d/%m/%Y")
    lines = ["<b>BREAKING NEWS &amp; ECONOMIC UPDATE</b> ♨️", "", f"ประจำเช้า{thai_day}ที่ {date_text}", "",
             "<b>🚨 ข่าวเศรษฐกิจโลก / การเงิน / Fed-ECB จาก RSS (ย้อนหลัง 24 ชั่วโมง)</b>"]
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
                           " ".join(escape(tag) for tag in item["tags"]) or "#รอติดตามทิศทางตลาด",
                           f"แหล่งข่าว: <a href=\"{url}\">{source}</a>"])
        output.extend(["", "<b>🗓️ ตารางประกาศสำคัญวันนี้ (Forex Factory, เวลาไทย)</b>",
                       "<pre>" + escape(calendar_table(agenda, len(events))) + "</pre>",
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
        thai_news = translate_and_analyze(selected)
        text = render(thai_news, events, now)
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
