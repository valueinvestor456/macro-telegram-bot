"""Macro summary -> Telegram bot, scheduled 4x daily (Thailand time).

Fetches DXY / GOLD / US10Y / TH10Y / USDTHB / BDI, formats a summary message,
and sends it via the Telegram Bot API at 08:00, 10:00, 14:00, 16:00 Asia/Bangkok.

Per-asset failures degrade to "N/A" — one blocked source never kills the message.

Usage:
    python main.py --once    # fetch + send immediately (for testing)
    python main.py           # run the scheduler loop forever
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import schedule
import yfinance as yf
from bs4 import BeautifulSoup

# Windows consoles often default to a legacy codepage (cp874 on Thai systems)
# that can't print emoji — force UTF-8 so --once test output doesn't crash.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _load_dotenv(path: Path = Path(__file__).with_name(".env")) -> None:
    """Minimal KEY=VALUE loader — avoids adding python-dotenv as a dependency.
    Never overrides a variable already set in the real environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

# ---------------------------------------------------------------------------
# CONFIG — set env vars TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, or paste here
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# If CHAT_ID not set, auto-detect from first message
AUTO_DETECT_CHAT_ID_PATH = Path(__file__).with_name("telegram_chat_id.json")

SEND_TIMES = ["09:45", "10:00", "11:00", "14:00", "15:00", "16:00", "16:15", "19:30", "20:30"]
TIMEZONE = "Asia/Bangkok"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}


# Auto-detect chat ID from first message if not configured
def get_auto_chat_id() -> str:
    """Load auto-detected chat ID from file if available."""
    if AUTO_DETECT_CHAT_ID_PATH.exists():
        try:
            data = json.loads(AUTO_DETECT_CHAT_ID_PATH.read_text(encoding="utf-8"))
            return str(data.get("chat_id", ""))
        except Exception:
            pass
    return ""


def save_auto_chat_id(chat_id: int) -> None:
    """Save auto-detected chat ID to file."""
    try:
        AUTO_DETECT_CHAT_ID_PATH.write_text(json.dumps({"chat_id": chat_id}), encoding="utf-8")
    except Exception as e:
        print(f"[telegram] failed to save auto-detect chat_id: {e}", file=sys.stderr)


# Use configured CHAT_ID or fall back to auto-detected one
_CONFIGURED_CHAT_ID = TELEGRAM_CHAT_ID
_AUTO_CHAT_ID = get_auto_chat_id()


# ---------------------------------------------------------------------------
# 1) DATA FETCHING
# ---------------------------------------------------------------------------
def fetch_yfinance(ticker: str) -> tuple[float, float]:
    """Return (last_price, pct_change_vs_prev_close). Raises on failure."""
    hist = yf.Ticker(ticker).history(period="5d")
    if len(hist) < 2:
        raise ValueError(f"{ticker}: got {len(hist)} rows, need >= 2")
    last = float(hist["Close"].iloc[-1])
    prev = float(hist["Close"].iloc[-2])
    return last, (last / prev - 1.0) * 100.0


def scrape_tradingeconomics(url: str) -> tuple[float, float | None]:
    """Scrape latest value (+ %change when present) from a TradingEconomics page.

    TE renders the headline quote server-side in elements with id="p" (last)
    and id="pch" (percent change) — verified against the live pages. Needs a
    browser User-Agent or TE returns a bot-block page.
    """
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    last_el = soup.find(id="p")
    if last_el is None or not last_el.get_text(strip=True):
        raise ValueError(f"could not find id='p' value on {url}")
    last = float(last_el.get_text(strip=True).replace(",", ""))

    pct = None
    pch_el = soup.find(id="pch")
    if pch_el is not None:
        txt = pch_el.get_text(strip=True).replace("%", "").replace(",", "")
        try:
            pct = float(txt)
        except ValueError:
            pct = None
    return last, pct


def scrape_bond10y_forecasts(countries: dict[str, str] | None = None) -> dict:
    """Scrape TE's 10Y bond forecast table (tradingeconomics.com/forecast/government-bond-10y).

    `countries` maps output key -> TE URL slug, e.g. {"US": "united-states"}.
    Matching is done on the row link's href slug, NOT the display text (the US
    row is labeled just "US" on the page). Returns
    {key: {"last": float, "quarters": [(label, value), ...]}}.
    Row layout verified against the live page: country link, <td id="p"> last
    yield, a signal cell, then one centered <td> per forecast quarter; quarter
    labels (e.g. Q3/26) come from the table's <thead>.
    """
    countries = countries or {"US": "united-states", "TH": "thailand"}
    url = "https://tradingeconomics.com/forecast/government-bond-10y"
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    import re
    slug_to_key = {slug: key for key, slug in countries.items()}
    out = {}
    for table in soup.find_all("table"):
        q_labels = [th.get_text(strip=True) for th in table.find_all("th")
                    if re.fullmatch(r"Q\d/\d\d", th.get_text(strip=True))]
        for row in table.find_all("tr"):
            link = row.find("a", href=re.compile(r"/government-bond-yield$"))
            if link is None:
                continue
            m = re.match(r"^/([^/]+)/government-bond-yield$", link["href"])
            if m is None or m.group(1) not in slug_to_key:
                continue
            country = slug_to_key[m.group(1)]
            last_td = row.find("td", id="p")
            if last_td is None:
                continue
            forecasts = []
            for td in row.find_all("td"):
                if td.get("id") == "p":
                    continue
                txt = td.get_text(strip=True).replace(",", "")
                if re.fullmatch(r"-?\d+(\.\d+)?", txt):
                    forecasts.append(float(txt))
            out[country] = {
                "last": float(last_td.get_text(strip=True).replace(",", "")),
                "quarters": list(zip(q_labels, forecasts)) if q_labels else
                            [(f"Q+{i+1}", v) for i, v in enumerate(forecasts)],
            }
    missing = [key for key in countries if key not in out]
    if missing:
        raise ValueError(f"countries not found in forecast table: {missing}")
    return out


FETCHERS = {
    # name: (callable, kwargs, unit, decimals)
    "DXY":    (fetch_yfinance, {"ticker": "DX-Y.NYB"}, "", 2),
    "GOLD":   (fetch_yfinance, {"ticker": "GC=F"}, " $", 1),
    "USOIL":  (fetch_yfinance, {"ticker": "CL=F"}, " $", 2),
    "US10Y":  (fetch_yfinance, {"ticker": "^TNX"}, "%", 3),
    "USDTHB": (fetch_yfinance, {"ticker": "THB=X"}, "", 3),
    "TH10Y":  (scrape_tradingeconomics,
               {"url": "https://tradingeconomics.com/thailand/government-bond-yield"}, "%", 3),
    "BDI":    (scrape_tradingeconomics,
               {"url": "https://tradingeconomics.com/commodity/baltic"}, "", 0),
}


def fetch_calendar() -> list | None:
    """Forex Factory weekly calendar, relayed same-origin as usd/calendar.json
    by the deepsleep456.com project (refreshed every 6h, see its
    ff-calendar.yml workflow) -- read here to surface the next high-impact
    event as a catalyst line instead of duplicating that scrape."""
    try:
        resp = requests.get("https://deepsleep456.com/usd/calendar.json", timeout=15)
        resp.raise_for_status()
        return resp.json().get("events") or []
    except Exception as e:
        print(f"  [FAIL -> omit catalyst] calendar.json: {type(e).__name__}: {e}", file=sys.stderr)
        return None


# Short, generic directional read for USD data surprises -- strong USD data
# historically pushes DXY up, which (same logic as compute_thb_score's DXY
# factor) weakens THB; weak data does the opposite. Good enough as a
# one-line heads-up, not a precise per-event call.
CATALYST_DIRECTION_HINT = "👉 แข็งแกร่ง/Hawkish → บาทอ่อน (USD/THB ขึ้น) | อ่อนแอ/Dovish → บาทแข็ง (USD/THB ลง)"


def format_catalyst_line(events: list | None) -> str | None:
    """Nearest upcoming High-impact USD event (the biggest USD/THB movers --
    FOMC, CPI, NFP, GDP, etc.), with a countdown and a generic hawkish/dovish
    -> baht direction hint appended per the user's request. Returns None if
    there's no such event in the feed (short lookahead window) or the feed
    is unreachable."""
    if not events:
        return None
    now = datetime.now(timezone.utc)
    upcoming = []
    for e in events:
        if e.get("impact") != "High" or e.get("country") != "USD":
            continue
        try:
            when = datetime.fromisoformat(e["date"])
        except Exception:
            continue
        if when > now:
            upcoming.append((when, e["title"]))
    if not upcoming:
        return None
    when, title = min(upcoming, key=lambda x: x[0])
    delta = when - now
    hours = delta.total_seconds() / 3600
    countdown = f"{delta.days}d {hours % 24:.0f}h" if delta.days else f"{hours:.0f}h"
    when_th = when.astimezone(ZoneInfo("Asia/Bangkok")).strftime("%d %b %H:%M")
    return f"🗞 Catalyst: {title} ในอีก {countdown} ({when_th} TH)\n   {CATALYST_DIRECTION_HINT}"


def fetch_market_data_json() -> dict | None:
    """PMI levels + Thai/US policy rates aren't scraped by this project --
    pull the already-published usd/market-data.json from the deepsleep456.com
    dashboard (refreshed hourly there) instead of duplicating that scraping
    here. Returns None on any failure; callers degrade gracefully."""
    try:
        resp = requests.get("https://deepsleep456.com/usd/market-data.json", timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  [FAIL -> omit score/CIP] market-data.json: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def fetch_all() -> dict:
    """Fetch every asset; failures become None (rendered as N/A) instead of crashing."""
    out = {}
    for name, (fn, kwargs, unit, dp) in FETCHERS.items():
        try:
            last, pct = fn(**kwargs)
            out[name] = {"last": last, "pct": pct, "unit": unit, "dp": dp}
            print(f"  [ok] {name}: {last:.{dp}f} ({'n/a' if pct is None else f'{pct:+.2f}%'})")
        except Exception as e:
            out[name] = None
            print(f"  [FAIL -> N/A] {name}: {type(e).__name__}: {e}", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# 1b) THB STRENGTH SCORE + CIP FUTURES FAIR VALUE
# (same formulas/weights/defaults as usd/index.html's recalc()/recalcAnchors()
#  on the deepsleep456.com/usd dashboard -- kept in sync manually, EXCEPT the
#  Oil factor below, which is main.py-only for now: Thailand is a net oil
#  importer, so a pricier import bill weakens THB -- same negative-correlation
#  shape as DXY, just not yet ported to the website widget.)
# ---------------------------------------------------------------------------
def _clamp3(x: float) -> float:
    return max(-3.0, min(3.0, x))


def compute_thb_score(d: dict, market: dict | None) -> dict | None:
    dxy, gold, us10y, th10y, bdi = d.get("DXY"), d.get("GOLD"), d.get("US10Y"), d.get("TH10Y"), d.get("BDI")
    if not (dxy and gold and us10y and th10y):
        return None

    v_dxy, v_xau, v_bdi, v_oil = 0.35, 1.0, 2.0, 1.5
    spr_neutral, spr_scale = 1.50, 0.75
    w = {"DXY": 0.60, "XAU": 0.17, "SPR": 0.22, "BDI": 0.10, "PMI": 0.15, "OIL": 0.10}

    s_dxy = _clamp3(-dxy["pct"] / v_dxy) if dxy.get("pct") is not None else 0.0
    s_xau = _clamp3(gold["pct"] / v_xau) if gold.get("pct") is not None else 0.0
    spread = us10y["last"] - th10y["last"]
    s_spr = _clamp3(-(spread - spr_neutral) / spr_scale)
    s_bdi = _clamp3(bdi["pct"] / v_bdi) if (bdi and bdi.get("pct") is not None) else 0.0

    oil = d.get("USOIL")
    s_oil = _clamp3(-oil["pct"] / v_oil) if (oil and oil.get("pct") is not None) else 0.0

    s_pmi = 0.0
    pmi = {k: (market or {}).get(f"pmi_{k}") for k in ("cn", "th", "in", "us")}
    if all(pmi[k] and pmi[k].get("last") is not None for k in pmi):
        cn, th, inn, us = (pmi[k]["last"] for k in ("cn", "th", "in", "us"))
        pmi_div = 0.40 * (cn - 50) / 5 + 0.35 * (th - 50) / 5 + 0.15 * (inn - 50) / 5 - 0.10 * (us - 50) / 5
        s_pmi = _clamp3(pmi_div)

    contrib = {
        "DXY": w["DXY"] * s_dxy,
        "Gold": w["XAU"] * s_xau,
        "Spread": w["SPR"] * s_spr,
        "BDI": w["BDI"] * s_bdi,
        "Oil": w["OIL"] * s_oil,
        "PMI": w["PMI"] * s_pmi,
    }
    total = sum(contrib.values())
    score = 100 * math.tanh(total)
    if score >= 30:
        verdict = "🟢 THB แข็งแรง — USD/THB bias ลง"
    elif score <= -30:
        verdict = "🔴 THB อ่อนแรง — USD/THB bias ขึ้น"
    else:
        verdict = "⚪ เป็นกลาง — สัญญาณไม่ชัด"
    # short_verdict: sign-only (no neutral bucket) for the compact USD/THB line
    short_verdict = "บาทแข็งค่า" if score >= 0 else "บาทอ่อนค่า"
    return {"score": score, "verdict": verdict, "short_verdict": short_verdict, "contrib": contrib}


# TFEX USD futures list quarterly (Mar/Jun/Sep/Dec) -- same approximation as
# usd/index.html's updateSettlementFromSeries(): last calendar day of the
# contract month minus 2 days (no Thai holiday calendar), so treat "days" as
# approximate, not exact.
TFEX_QUARTER_MONTHS = [3, 6, 9, 12]
MONTH_CODE_BY_IDX = {0: "F", 1: "G", 2: "H", 3: "J", 4: "K", 5: "M",
                      6: "N", 7: "Q", 8: "U", 9: "V", 10: "X", 11: "Z"}

# Front-month rolls to the next quarterly contract once within ~2 months of
# the current one's settlement -- liquidity shifts to the next quarter well
# before expiry in practice, so treating the current quarter as "front" all
# the way to its settlement date overstates how long it stays the active one.
TFEX_ROLL_BUFFER_DAYS = 60


def next_tfex_settlement(today: date | None = None) -> tuple[date, int, int]:
    today = today or date.today()
    y = today.year
    while True:
        for m in TFEX_QUARTER_MONTHS:
            if y == today.year and m < today.month:
                continue
            last_day = calendar.monthrange(y, m)[1]
            settlement = date(y, m, last_day) - timedelta(days=2)
            if settlement - timedelta(days=TFEX_ROLL_BUFFER_DAYS) >= today:
                return settlement, y, m
        y += 1


def format_trend_line(market: dict | None) -> str:
    """EMA(20)/EMA(50) trend filter, as a USD1! (TFEX USD futures) proxy -- no
    free API for the actual futures series, but it tracks USD/THB spot
    closely via CIP arbitrage. Computed server-side (THB=X via yfinance) in
    the dashboard project's scripts/fetch_market_data.py and read here from
    the same published usd/market-data.json rather than re-scraping it.
    Direction is the EMA20-vs-EMA50 crossover -- the two numbers alone tell
    the story, no separate signal needed."""
    trend = (market or {}).get("usdthb_trend")
    if not trend:
        return "Trend (EMA20/50): N/A ⚠️"
    up = trend["direction"] == "up"
    arrow = "🟢 Uptrend" if up else "🔴 Downtrend"
    sign = ">" if up else "<"
    return f"{arrow} (EMA20 {trend['ema20']:.3f} {sign} EMA50 {trend['ema50']:.3f})"


def compute_cip_fair(d: dict, market: dict | None) -> dict | None:
    usdthb = d.get("USDTHB")
    if not usdthb or not market:
        return None
    i_th = (market.get("th_rate") or {}).get("last")
    i_us = (market.get("us_rate") or {}).get("last")
    if i_th is None or i_us is None:
        return None
    settlement, y, m = next_tfex_settlement()
    days = max((settlement - date.today()).days, 1)
    t = days / 365.0
    cip_fair = usdthb["last"] * (1 + i_th / 100 * t) / (1 + i_us / 100 * t)
    series = f"USD{MONTH_CODE_BY_IDX[m - 1]}{y % 100:02d}"
    return {"fair": cip_fair, "series": series}


def compute_cip_fair_fixed(d: dict, market: dict | None, month: int) -> dict | None:
    """CIP fair value for a fixed TFEX USD contract (e.g., Sep=9, Dec=12)."""
    usdthb = d.get("USDTHB")
    if not usdthb or not market:
        return None
    i_th = (market.get("th_rate") or {}).get("last")
    i_us = (market.get("us_rate") or {}).get("last")
    if i_th is None or i_us is None:
        return None

    today = date.today()
    y = today.year
    if month < today.month:
        y += 1

    last_day = calendar.monthrange(y, month)[1]
    settlement = date(y, month, last_day) - timedelta(days=2)

    days = max((settlement - today).days, 1)
    t = days / 365.0
    cip_fair = usdthb["last"] * (1 + i_th / 100 * t) / (1 + i_us / 100 * t)
    series = f"USD{MONTH_CODE_BY_IDX[month - 1]}{y % 100:02d}"
    return {"fair": cip_fair, "series": series}


MANUAL_FUT_STALE_HOURS = 48


def fetch_tfex_usd_futures() -> dict | None:
    """Real, live-traded TFEX USD futures price (front-month, auto-rolling)
    via TradingView's public scanner API -- no auth, no desktop app/CDP
    needed, so it works from GitHub Actions runners even when the PC is off.
    (TFEX's own market-data page has no free API of its own: it's a Nuxt SSR
    app whose quote is fetched client-side from an internal endpoint behind
    Imperva bot protection -- not worth scraping. This sidesteps that by
    using TradingView's redistribution of the same feed instead.)"""
    try:
        resp = requests.post(
            "https://scanner.tradingview.com/global/scan",
            json={"symbols": {"tickers": ["TFEX:USD1!"]}, "columns": ["close", "change"]},
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json().get("data") or []
        if not rows:
            return None
        close, pct = rows[0]["d"]
        return {"price": close, "pct": pct}
    except Exception as e:
        print(f"  [FAIL -> fall back to manual futures price] scanner.tradingview.com: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def fetch_tfex_usd_futures_dated(series: str) -> dict | None:
    """Fetch live price for a specific TFEX USD futures contract (dated).
    series: e.g., "USDZ2026" for Dec 2026 contract."""
    try:
        ticker = f"TFEX:{series}"
        resp = requests.post(
            "https://scanner.tradingview.com/global/scan",
            json={"symbols": {"tickers": [ticker]}, "columns": ["close", "change"]},
            timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json().get("data") or []
        if not rows:
            return None
        close, pct = rows[0]["d"]
        return {"price": close, "pct": pct}
    except Exception as e:
        print(f"  [FAIL -> omit {series} live price] scanner.tradingview.com: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def fetch_manual_fut_price() -> dict | None:
    """Fallback only -- see fetch_tfex_usd_futures() for the live source now
    used first. Kept as a backstop for whenever TradingView's scanner API is
    unreachable: whenever you run "/fut <price>" on the DR bot it persists
    that submission to usd/manual-fut-price.json, published the same way as
    market-data.json -- read it here so the Macro Summary can still show the
    last price you actually reported, with its age."""
    try:
        resp = requests.get("https://deepsleep456.com/usd/manual-fut-price.json", timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"  [FAIL -> omit manual futures price] manual-fut-price.json: {type(e).__name__}: {e}", file=sys.stderr)
        return None


FUT_BASIS_THRESHOLD = 0.03  # THB, same RICH/CHEAP/FAIR band as telegram_dr_bot.py's cmd_fut


def format_fut_line(cip: dict | None, live: dict | None) -> str:
    """Compact line matching the other asset lines' style (label: value
    arrow %change) for the real, live-traded TFEX USD futures price. Falls
    back to the last manually-reported price (with its age) only if the
    live TradingView feed is unreachable."""
    series = cip["series"] if cip else "USD futures"
    if live:
        arrow = f"{'🟢▲' if live['pct'] >= 0 else '🔴▼'} {live['pct']:+.2f}%"
        return f"{series} จริง: {live['price']:.4f} {arrow}"

    manual = fetch_manual_fut_price()
    if not manual:
        return f"{series} จริง: N/A ⚠️"
    try:
        asof = datetime.fromisoformat(manual["asof"].replace("Z", "+00:00"))
    except Exception:
        asof = None
    age_hours = (datetime.now(timezone.utc) - asof).total_seconds() / 3600 if asof else None
    if age_hours is not None and age_hours > MANUAL_FUT_STALE_HOURS:
        return f"{manual['series']} จริง: ข้อมูลเก่า ({age_hours/24:.1f} วัน) — ส่ง /fut [ราคา] ใหม่"
    return f"{manual['series']} จริง: {manual['price']:.4f}"


def compute_trade_signal(score: dict | None, market: dict | None, cip: dict | None, live: dict | None) -> str | None:
    """Combines the three directional inputs already in the message -- THB
    strength Score, USD/THB EMA20/50 trend, and futures basis vs CIP fair
    value -- into one long/short read for USD/THB (equivalently: TFEX USD
    futures), instead of leaving the reader to reconcile them by eye.

    Each input votes LONG or SHORT USD/THB (abstains if neutral/unavailable):
      - Score >= +15 -> THB strengthening -> SHORT; <= -15 -> LONG
      - EMA20/50: uptrend -> LONG; downtrend -> SHORT
      - Basis: futures RICH (>fair by threshold) -> SHORT (expect reversion
        down); CHEAP -> LONG
    3/3 or 2/3 agreement -> directional call; otherwise -> NEUTRAL/WAIT."""
    votes = []
    reasons = []

    if score and abs(score["score"]) >= 15:
        d_ = "SHORT" if score["score"] > 0 else "LONG"
        votes.append(d_)
        reasons.append(f"Score {d_}")

    trend = (market or {}).get("usdthb_trend")
    if trend:
        d_ = "LONG" if trend["direction"] == "up" else "SHORT"
        votes.append(d_)
        reasons.append(f"Trend {d_}")

    if cip and live:
        basis = live["price"] - cip["fair"]
        if abs(basis) > FUT_BASIS_THRESHOLD:
            d_ = "SHORT" if basis > 0 else "LONG"
            votes.append(d_)
            reasons.append(f"Basis {'RICH' if basis > 0 else 'CHEAP'}->{d_}")

    longs, shorts = votes.count("LONG"), votes.count("SHORT")
    total = len(votes)
    if total < 2:
        return None  # one lone vote isn't enough to call a direction

    if shorts == 0:
        return f"📌 สัญญาณ: 🟢 LONG USD/THB (เห็นตรงกัน {total}/{total}: {', '.join(reasons)})"
    if longs == 0:
        return f"📌 สัญญาณ: 🔴 SHORT USD/THB (เห็นตรงกัน {total}/{total}: {', '.join(reasons)})"
    if longs != shorts:
        lean = "LONG" if longs > shorts else "SHORT"
        return f"📌 สัญญาณ: ⚪ เอียง {lean} ({longs}L/{shorts}S: {', '.join(reasons)})"
    return f"📌 สัญญาณ: ⚪ สัญญาณขัดแย้ง — รอความชัดเจน ({', '.join(reasons)})"


# ---------------------------------------------------------------------------
# 2) MESSAGE FORMATTING + TELEGRAM
# ---------------------------------------------------------------------------
def _line(name: str, data: dict | None, label: str) -> str:
    if data is None:
        return f"{label}: N/A ⚠️"
    val = f"{data['last']:,.{data['dp']}f}{data['unit']}"
    if data["pct"] is None:
        return f"{label}: {val}"
    arrow = "🟢▲" if data["pct"] >= 0 else "🔴▼"
    return f"{label}: {val}  {arrow} {data['pct']:+.2f}%"


def format_message(d: dict, market: dict | None) -> str:
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    score = compute_thb_score(d, market)
    score_line = f"🎯 Score {score['score']:+.0f} {score['short_verdict']}" if score else "🎯 Score N/A ⚠️"

    lines = [
        f"📊 Macro Summary — {now} (TH)",
        "",
        _line("USDTHB", d.get("USDTHB"), "💱 USD/THB"),
        score_line,
    ]
    if score:
        breakdown = "  ".join(f"{k} {v:+.2f}" for k, v in score["contrib"].items())
        lines.append(f"   ⤷ {breakdown}")

    # USDU26 (Sep 2026)
    cip_u = compute_cip_fair_fixed(d, market, 9)
    live_fut_u = fetch_tfex_usd_futures_dated("USDU26")
    if live_fut_u and cip_u:
        arrow = f"{'🟢▲' if live_fut_u['pct'] >= 0 else '🔴▼'} {live_fut_u['pct']:+.2f}%"
        lines.append(f"USDU26 จริง: {live_fut_u['price']:.4f} / มูลค่ายุติธรรม: {cip_u['fair']:.4f} {arrow}")
        basis = live_fut_u["price"] - cip_u["fair"]
        verdict = "🔴 RICH" if basis > FUT_BASIS_THRESHOLD else ("🟢 CHEAP" if basis < -FUT_BASIS_THRESHOLD else "⚪ FAIR")
        lines.append(f"Basis: {basis:+.4f} THB {verdict}")
    elif live_fut_u and not cip_u:
        arrow = f"{'🟢▲' if live_fut_u['pct'] >= 0 else '🔴▼'} {live_fut_u['pct']:+.2f}%"
        lines.append(f"USDU26 จริง: {live_fut_u['price']:.4f} / มูลค่ายุติธรรม: N/A ⚠️ {arrow}")
    elif cip_u and not live_fut_u:
        lines.append(f"USDU26 จริง: N/A ⚠️ / มูลค่ายุติธรรม: {cip_u['fair']:.4f}")
    else:
        lines.append("USDU26 จริง: N/A ⚠️ / มูลค่ายุติธรรม: N/A ⚠️")

    # USDZ26 (Dec 2026)
    cip_z = compute_cip_fair_fixed(d, market, 12)
    live_fut_z = fetch_tfex_usd_futures_dated("USDZ26")
    if live_fut_z and cip_z:
        arrow = f"{'🟢▲' if live_fut_z['pct'] >= 0 else '🔴▼'} {live_fut_z['pct']:+.2f}%"
        lines.append(f"USDZ26 จริง: {live_fut_z['price']:.4f} / มูลค่ายุติธรรม: {cip_z['fair']:.4f} {arrow}")
        basis = live_fut_z["price"] - cip_z["fair"]
        verdict = "🔴 RICH" if basis > FUT_BASIS_THRESHOLD else ("🟢 CHEAP" if basis < -FUT_BASIS_THRESHOLD else "⚪ FAIR")
        lines.append(f"Basis: {basis:+.4f} THB {verdict}")
    elif live_fut_z and not cip_z:
        arrow = f"{'🟢▲' if live_fut_z['pct'] >= 0 else '🔴▼'} {live_fut_z['pct']:+.2f}%"
        lines.append(f"USDZ26 จริง: {live_fut_z['price']:.4f} / มูลค่ายุติธรรม: N/A ⚠️ {arrow}")
    elif cip_z and not live_fut_z:
        lines.append(f"USDZ26 จริง: N/A ⚠️ / มูลค่ายุติธรรม: {cip_z['fair']:.4f}")
    else:
        lines.append("USDZ26 จริง: N/A ⚠️ / มูลค่ายุติธรรม: N/A ⚠️")

    lines.append(format_trend_line(market))
    lines += [
        _line("DXY", d.get("DXY"), "💵 DXY"),
        _line("GOLD", d.get("GOLD"), "🥇 Gold"),
        _line("US10Y", d.get("US10Y"), "🇺🇸 US10Y"),
        _line("TH10Y", d.get("TH10Y"), "🇹🇭 TH10Y"),
        _line("BDI", d.get("BDI"), "🚢 BDI"),
        _line("USOIL", d.get("USOIL"), "🛢️ US Oil"),
    ]
    us, th = d.get("US10Y"), d.get("TH10Y")
    if us and th:
        spread = us["last"] - th["last"]
        lines.append(f"↔️ Spread US−TH: {spread:+.2f}% {'🔴 (outflow risk)' if spread > 2.0 else ''}")

    signal = compute_trade_signal(score, market, cip_u, live_fut_u)
    if signal:
        lines.append(signal)

    catalyst = format_catalyst_line(fetch_calendar())
    if catalyst:
        lines.append(catalyst)

    lines.append("ดูสด: deepsleep456.com/usd")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 1c) BOND 10Y FORECAST -- alert-only (not in every Macro Summary anymore,
# since TE's forecast barely moves run to run). Baseline is the last snapshot
# that actually triggered an alert, persisted to bond10y_forecast_state.json
# and git-committed back to the repo so the next scheduled run (fresh
# checkout) can compare against it -- same pattern as the DR-scan project's
# telegram_dr_bot.py persisting its getUpdates offset.
# ---------------------------------------------------------------------------
FORECAST_STATE_PATH = Path(__file__).with_name("bond10y_forecast_state.json")
FORECAST_ALERT_THRESHOLD = 0.05  # percentage points, any single quarter


def _render_forecast_table(fc: dict) -> str:
    us, th = fc["US"], fc["TH"]
    n = min(len(us["quarters"]), len(th["quarters"]), 4)
    labels = [q[0] for q in us["quarters"][:n]]

    def row(name, last, vals):
        return name.ljust(7) + f"{last:5.2f}" + "".join(f"{v:7.2f}" for v in vals)

    head = "10Y".ljust(7) + " Last" + "".join(f"{q:>7}" for q in labels)
    us_vals = [q[1] for q in us["quarters"][:n]]
    th_vals = [q[1] for q in th["quarters"][:n]]
    sp_vals = [u - t for u, t in zip(us_vals, th_vals)]
    return "\n".join([
        head,
        row("US", us["last"], us_vals),
        row("TH", th["last"], th_vals),
        row("Spread", us["last"] - th["last"], sp_vals),
    ])


def _forecast_max_diff(current: dict, baseline: dict) -> float:
    """Max abs diff (percentage points) across quarters present in both
    snapshots -- quarters roll off/on over time as TE's window advances, so
    only overlapping quarter labels (e.g. "Q3/26") are compared."""
    max_diff = 0.0
    for country, cur in current.items():
        base = baseline.get(country)
        if not base:
            continue
        base_map = dict(base["quarters"])
        for label, val in cur["quarters"]:
            if label in base_map:
                max_diff = max(max_diff, abs(val - base_map[label]))
    return max_diff


def _git_commit_forecast_state():
    """`git diff --quiet` (unstaged) never flags a brand-new untracked file as
    changed, so the very first baseline would silently never get committed --
    stage first, then check the *staged* diff against HEAD instead."""
    repo_dir = Path(__file__).resolve().parent
    try:
        subprocess.run(["git", "add", str(FORECAST_STATE_PATH)], cwd=repo_dir, check=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet", "--", str(FORECAST_STATE_PATH)], cwd=repo_dir)
        if diff.returncode == 0:
            return  # unchanged
        subprocess.run(["git", "commit", "-m", "Bond 10Y forecast: update alert baseline [skip ci]"],
                        cwd=repo_dir, check=True)
        subprocess.run(["git", "push"], cwd=repo_dir, check=True)
    except Exception as e:
        print("git_commit_forecast_state error:", e, file=sys.stderr)


def check_forecast_alert() -> str | None:
    """Returns a Telegram-ready alert string if the 10Y forecast moved >=
    FORECAST_ALERT_THRESHOLD pp on any quarter since the last alert baseline,
    else None. First-ever run (no baseline yet) just records one silently."""
    try:
        fc = scrape_bond10y_forecasts({"US": "united-states", "TH": "thailand"})
    except Exception as e:
        print(f"  [FAIL -> skip forecast alert check] {type(e).__name__}: {e}", file=sys.stderr)
        return None

    current = {country: {"last": v["last"], "quarters": v["quarters"]} for country, v in fc.items()}

    baseline = None
    if FORECAST_STATE_PATH.exists():
        try:
            baseline = json.loads(FORECAST_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            baseline = None

    if baseline is not None and _forecast_max_diff(current, baseline) < FORECAST_ALERT_THRESHOLD:
        return None

    FORECAST_STATE_PATH.write_text(json.dumps(current), encoding="utf-8")
    _git_commit_forecast_state()
    if baseline is None:
        return None  # nothing to compare against yet -- just established

    table = _render_forecast_table(fc)
    return f"⚠️ Bond 10Y Forecast เปลี่ยน ≥{FORECAST_ALERT_THRESHOLD:.2f}pp (TradingEconomics)\n<pre>{table}</pre>"


def send_telegram(text: str) -> bool:
    global _AUTO_CHAT_ID
    chat_id = _CONFIGURED_CHAT_ID or _AUTO_CHAT_ID
    if not chat_id or "PASTE_YOUR" in TELEGRAM_BOT_TOKEN:
        print("[telegram] ERROR: token/chat_id not configured", file=sys.stderr)
        print(f"[telegram] TELEGRAM_BOT_TOKEN exists: {bool(TELEGRAM_BOT_TOKEN and 'PASTE_YOUR' not in TELEGRAM_BOT_TOKEN)}")
        print(f"[telegram] TELEGRAM_CHAT_ID configured: {bool(_CONFIGURED_CHAT_ID)}")
        print(f"[telegram] auto-detected chat_id: {bool(_AUTO_CHAT_ID)}")
        print("[telegram] printing message instead:\n")
        print(text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        payload = {"chat_id": str(chat_id), "text": text}
        # only use HTML mode if message contains actual HTML tags (for <pre> forecast table)
        if "<pre>" in text:
            payload["parse_mode"] = "HTML"
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        print(f"[telegram] sent OK to chat_id={chat_id}")
        return True
    except Exception as e:
        print(f"[telegram] send FAILED to chat_id={chat_id}: {e}", file=sys.stderr)
        return False


def job():
    print(f"\n=== fetching @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    data = fetch_all()
    market = fetch_market_data_json()
    send_telegram(format_message(data, market))


def format_usdz26_message(d: dict, market: dict | None) -> str:
    """USDZ26-focused macro summary (Dec 2026 futures)."""
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    score = compute_thb_score(d, market)
    score_line = f"🎯 Score {score['score']:+.0f} {score['short_verdict']}" if score else "🎯 Score N/A ⚠️"

    lines = [
        f"📊 Macro Summary (USDZ26) — {now} (TH)",
        "",
        _line("USDTHB", d.get("USDTHB"), "💱 USD/THB"),
        score_line,
    ]
    if score:
        breakdown = "  ".join(f"{k} {v:+.2f}" for k, v in score["contrib"].items())
        lines.append(f"   ⤷ {breakdown}")

    # USDZ26 (Dec 2026) - focus
    cip_z = compute_cip_fair_fixed(d, market, 12)
    live_fut_z = fetch_tfex_usd_futures_dated("USDZ26")
    if live_fut_z and cip_z:
        arrow = f"{'🟢▲' if live_fut_z['pct'] >= 0 else '🔴▼'} {live_fut_z['pct']:+.2f}%"
        lines.append(f"USDZ26 จริง: {live_fut_z['price']:.4f} {arrow}")
        lines.append(f"Futures ยุติธรรม: {cip_z['fair']:.4f}")
        basis = live_fut_z["price"] - cip_z["fair"]
        verdict = "🔴 RICH" if basis > FUT_BASIS_THRESHOLD else ("🟢 CHEAP" if basis < -FUT_BASIS_THRESHOLD else "⚪ FAIR")
        lines.append(f"Basis: {basis:+.4f} THB {verdict}")
    elif live_fut_z or cip_z:
        price_str = f"{live_fut_z['price']:.4f}" if live_fut_z else "N/A"
        fair_str = f"{cip_z['fair']:.4f}" if cip_z else "N/A"
        lines.append(f"USDZ26 จริง: {price_str}")
        lines.append(f"Futures ยุติธรรม: {fair_str}")
    else:
        lines.append("USDZ26 จริง: N/A ⚠️")
        lines.append("Futures ยุติธรรม: N/A ⚠️")

    lines.append(format_trend_line(market))
    lines += [
        _line("DXY", d.get("DXY"), "💵 DXY"),
        _line("GOLD", d.get("GOLD"), "🥇 Gold"),
        _line("US10Y", d.get("US10Y"), "🇺🇸 US10Y"),
        _line("TH10Y", d.get("TH10Y"), "🇹🇭 TH10Y"),
        _line("BDI", d.get("BDI"), "🚢 BDI"),
        _line("USOIL", d.get("USOIL"), "🛢️ US Oil"),
    ]
    us, th = d.get("US10Y"), d.get("TH10Y")
    if us and th:
        spread = us["last"] - th["last"]
        lines.append(f"↔️ Spread US−TH: {spread:+.2f}% {'🔴 (outflow risk)' if spread > 2.0 else ''}")

    signal = compute_trade_signal(score, market, cip_z, live_fut_z)
    if signal:
        lines.append(signal)

    catalyst = format_catalyst_line(fetch_calendar())
    if catalyst:
        lines.append(catalyst)

    lines.append("ดูสด: deepsleep456.com/usd")
    return "\n".join(lines)


def job_usdz26():
    print(f"\n=== fetching USDZ26 @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    data = fetch_all()
    market = fetch_market_data_json()
    send_telegram(format_usdz26_message(data, market))


def fetch_stock_research() -> list | None:
    """Fetch Thai stock research updates from stock.gapfocus.com."""
    try:
        url = "https://stock.gapfocus.com/research"
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Extract research items (adjust selectors based on actual page structure)
        items = []
        for article in soup.find_all("div", class_=["research-item", "article", "post"]):
            title_elem = article.find(["h2", "h3", "a"])
            if title_elem:
                title = title_elem.get_text(strip=True)
                link = article.find("a")
                href = link.get("href", "") if link else ""
                if title and len(items) < 5:
                    items.append({"title": title, "link": href})

        return items if items else None
    except Exception as e:
        print(f"  [FAIL -> omit stock research] stock.gapfocus.com: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def fetch_ipo_stocks() -> list | None:
    """Fetch IPO & recent stock offerings from market.sec.or.th."""
    try:
        url = "https://market.sec.or.th/public/idisc/th/r59"
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Extract IPO/stock offering info (adjust selectors based on actual page structure)
        items = []
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) >= 3:
                ticker = cells[0].get_text(strip=True)
                name = cells[1].get_text(strip=True)
                price = cells[2].get_text(strip=True)
                if ticker and len(items) < 5:
                    items.append({"ticker": ticker, "name": name, "price": price})

        return items if items else None
    except Exception as e:
        print(f"  [FAIL -> omit IPO stocks] market.sec.or.th: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def format_stock_message() -> str:
    """Format Thai stock research & IPO updates."""
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [
        f"📈 Thai Stock Updates — {now} (TH)",
        "",
        "🔍 Research Highlights:",
    ]

    research = fetch_stock_research()
    if research:
        for i, item in enumerate(research, 1):
            link_text = f" — {item['link']}" if item.get('link') else ""
            lines.append(f"{i}. {item['title']}{link_text}")
    else:
        lines.append("• No updates available")

    lines.append("")
    lines.append("💰 IPO & Market Offerings (SEC):")

    ipo = fetch_ipo_stocks()
    if ipo:
        for i, stock in enumerate(ipo, 1):
            lines.append(f"{i}. **{stock['ticker']}** — {stock['name']} @ {stock['price']}")
    else:
        lines.append("• No IPO updates available")

    lines.append("")
    lines.append("📊 Sources: stock.gapfocus.com | market.sec.or.th")
    return "\n".join(lines)


def job_stocks():
    print(f"\n=== fetching stocks @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    message = format_stock_message()
    send_telegram(message)


# ---------------------------------------------------------------------------
# 3) COMMAND HANDLERS (POLLING)
# ---------------------------------------------------------------------------
UPDATE_OFFSET_PATH = Path(__file__).with_name("telegram_update_offset.json")


def get_last_update_offset() -> int:
    """Load last processed update_id to avoid re-processing messages."""
    if UPDATE_OFFSET_PATH.exists():
        try:
            data = json.loads(UPDATE_OFFSET_PATH.read_text(encoding="utf-8"))
            return data.get("offset", 0)
        except Exception:
            pass
    return 0


def save_last_update_offset(offset: int) -> None:
    """Persist the last update_id so we only fetch new messages."""
    try:
        UPDATE_OFFSET_PATH.write_text(json.dumps({"offset": offset}), encoding="utf-8")
    except Exception as e:
        print(f"[telegram] failed to save offset: {e}", file=sys.stderr)


def format_usd_futures() -> str:
    """Format real-time USDU26 and USDZ26 prices + fair values + score."""
    try:
        print("[usd cmd] fetching market data...", file=sys.stdout, flush=True)
        # Fetch data
        data = fetch_all()
        market = fetch_market_data_json()

        print(f"[usd cmd] fetched {len([v for v in data.values() if v])} assets, market={'ok' if market else 'failed'}", file=sys.stdout, flush=True)

        # Calculate score
        score = compute_thb_score(data, market)
        score_line = f"Score {score['score']:+.0f} {score['short_verdict']}" if score else "Score N/A"

        lines = [f"📊 USD Futures — {datetime.now().strftime('%d/%m/%Y %H:%M')} (TH) {score_line}"]
        
        # USDU26 (Sep 2026) - typically SHORT bias
        cip_u = compute_cip_fair_fixed(data, market, 9)
        live_fut_u = fetch_tfex_usd_futures_dated("USDU26")
        
        if live_fut_u and cip_u:
            arrow = f"{'🟢▲' if live_fut_u['pct'] >= 0 else '🔴▼'}{live_fut_u['pct']:+.2f}%"
            lines.append(f"USDU26 (Real/Fair): {live_fut_u['price']:.4f} / {cip_u['fair']:.4f} {arrow} Short")
        elif live_fut_u or cip_u:
            price_str = f"{live_fut_u['price']:.4f}" if live_fut_u else "N/A"
            fair_str = f"{cip_u['fair']:.4f}" if cip_u else "N/A"
            lines.append(f"USDU26 (Real/Fair): {price_str} / {fair_str} Short")
        else:
            lines.append("USDU26 (Real/Fair): N/A / N/A")
        
        # USDZ26 (Dec 2026) - typically LONG bias
        cip_z = compute_cip_fair_fixed(data, market, 12)
        live_fut_z = fetch_tfex_usd_futures_dated("USDZ26")
        
        if live_fut_z and cip_z:
            arrow = f"{'🟢▲' if live_fut_z['pct'] >= 0 else '🔴▼'}{live_fut_z['pct']:+.2f}%"
            lines.append(f"USDZ26 (Real/Fair): {live_fut_z['price']:.4f} / {cip_z['fair']:.4f} {arrow} Long")
        elif live_fut_z or cip_z:
            price_str = f"{live_fut_z['price']:.4f}" if live_fut_z else "N/A"
            fair_str = f"{cip_z['fair']:.4f}" if cip_z else "N/A"
            lines.append(f"USDZ26 (Real/Fair): {price_str} / {fair_str} Long")
        else:
            lines.append("USDZ26 (Real/Fair): N/A / N/A")
        
        print("[usd cmd] formatted successfully, returning message", file=sys.stdout, flush=True)
        return "\n".join(lines)
    except Exception as e:
        print(f"[usd cmd] ERROR: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return f"❌ Error fetching USD futures: {type(e).__name__}: {str(e)[:150]}"


def get_telegram_updates() -> list:
    """Poll Telegram for new messages using getUpdates."""
    if "PASTE_YOUR" in TELEGRAM_BOT_TOKEN:
        return []
    
    offset = get_last_update_offset()
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, params={"offset": offset, "timeout": 30}, timeout=35)
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as e:
        print(f"[telegram] getUpdates failed: {type(e).__name__}: {e}", file=sys.stderr)
        return []


def handle_command(chat_id: str, command: str) -> None:
    """Handle incoming Telegram commands."""
    try:
        print(f"[telegram] handling command: {command} from {chat_id}", file=sys.stdout, flush=True)
        if command == "/usd":
            print(f"[telegram] fetching USD futures data...", file=sys.stdout, flush=True)
            text = format_usd_futures()
            print(f"[telegram] formatted message, sending reply...", file=sys.stdout, flush=True)
            send_telegram_reply(chat_id, text)
        elif command in ["/start", "/help"]:
            help_text = "📌 Available commands:\n/usd — Check USDU26 & USDZ26 real-time prices & fair values\n/help — Show this message"
            send_telegram_reply(chat_id, help_text)
        else:
            print(f"[telegram] unknown command: {command} from {chat_id}", file=sys.stdout, flush=True)
    except Exception as e:
        print(f"[telegram] handle_command error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        send_telegram_reply(chat_id, f"Error: {type(e).__name__}: {str(e)[:100]}")


def send_telegram_reply(chat_id: str, text: str) -> bool:
    """Send a reply to a specific chat_id."""
    print(f"[reply] attempting to send to {chat_id}...", file=sys.stdout, flush=True)
    if "PASTE_YOUR" in TELEGRAM_BOT_TOKEN:
        print(f"[reply] token not configured, printing instead:\n{text[:100]}...", file=sys.stdout, flush=True)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        print(f"[reply] POST to {url}", file=sys.stdout, flush=True)
        payload = {"chat_id": chat_id, "text": text}
        if "<pre>" in text:
            payload["parse_mode"] = "HTML"
        resp = requests.post(url, json=payload, timeout=30)
        print(f"[reply] status code: {resp.status_code}", file=sys.stdout, flush=True)
        resp.raise_for_status()
        print(f"[reply] ✓ sent to {chat_id} successfully", file=sys.stdout, flush=True)
        return True
    except Exception as e:
        print(f"[reply] ✗ FAILED: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return False


def poll_commands() -> None:
    """Poll and process incoming Telegram messages/commands."""
    global _AUTO_CHAT_ID
    try:
        updates = get_telegram_updates()
        if not updates:
            print(f"[poll] no updates (offset: {get_last_update_offset()})", flush=True)
            return

        print(f"[poll] got {len(updates)} update(s)", flush=True)
        for update in updates:
            offset = update.get("update_id", 0)
            message = update.get("message", {})
            text = message.get("text", "").strip()
            chat_id = message.get("chat", {}).get("id")
            
            # Auto-detect chat ID from first message if not configured
            if chat_id and not _CONFIGURED_CHAT_ID and not _AUTO_CHAT_ID:
                print(f"[telegram] auto-detected chat_id: {chat_id}")
                save_auto_chat_id(chat_id)
                _AUTO_CHAT_ID = str(chat_id)
            
            if text and chat_id:
                print(f"[msg] received: '{text}' from chat_id: {chat_id}", flush=True)
                if text.startswith("/"):
                    print(f"[msg] command detected, handling...", flush=True)
                    handle_command(str(chat_id), text)
                else:
                    print(f"[msg] not a command, ignoring", flush=True)
            else:
                print(f"[telegram] no text or chat_id: text='{text}', chat_id={chat_id}")
            
            # Always save the offset, even if we didn't process a command
            if offset:
                save_last_update_offset(offset + 1)
    except Exception as e:
        print(f"[telegram] poll_commands error: {type(e).__name__}: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 4) SCHEDULER
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one fetch+send now, then exit")
    args = parser.parse_args()

    if args.once:
        job()
        return

    for t in SEND_TIMES:
        schedule.every().day.at(t, TIMEZONE).do(job)
        schedule.every().day.at(t, TIMEZONE).do(job_usdz26)
        schedule.every().day.at(t, TIMEZONE).do(job_stocks)
    print(f"scheduled daily at {', '.join(SEND_TIMES)} ({TIMEZONE})")
    print(f"  — USDU26 macro summary")
    print(f"  — USDZ26 macro summary")
    print(f"  — Thai stock updates (research & IPO)")
    print(f"  — polling for /usd commands")
    print(f"  — Ctrl+C to stop")

    print("[bot] starting polling loop — waiting for /usd commands...", flush=True)
    while True:
        schedule.run_pending()
        poll_commands()
        time.sleep(5)  # poll every 5 seconds for commands


if __name__ == "__main__":
    main()
