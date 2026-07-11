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
import os
import sys
import time
from datetime import datetime
from pathlib import Path

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
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "PASTE_YOUR_CHAT_ID_HERE")

SEND_TIMES = ["08:00", "10:00", "14:00", "16:00"]
TIMEZONE = "Asia/Bangkok"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}


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
    "US10Y":  (fetch_yfinance, {"ticker": "^TNX"}, "%", 3),
    "USDTHB": (fetch_yfinance, {"ticker": "THB=X"}, "", 3),
    "TH10Y":  (scrape_tradingeconomics,
               {"url": "https://tradingeconomics.com/thailand/government-bond-yield"}, "%", 3),
    "BDI":    (scrape_tradingeconomics,
               {"url": "https://tradingeconomics.com/commodity/baltic"}, "", 0),
}


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


def format_message(d: dict) -> str:
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    lines = [
        f"📊 Macro Summary — {now} (TH)",
        "",
        _line("USDTHB", d.get("USDTHB"), "💱 USD/THB"),
        _line("DXY", d.get("DXY"), "💵 DXY"),
        _line("GOLD", d.get("GOLD"), "🥇 Gold"),
        _line("US10Y", d.get("US10Y"), "🇺🇸 US10Y"),
        _line("TH10Y", d.get("TH10Y"), "🇹🇭 TH10Y"),
        _line("BDI", d.get("BDI"), "🚢 BDI"),
    ]
    us, th = d.get("US10Y"), d.get("TH10Y")
    if us and th:
        spread = us["last"] - th["last"]
        lines.append(f"↔️ Spread US−TH: {spread:+.2f}% {'🔴 กว้าง (outflow risk)' if spread > 2.0 else ''}")
    fc_table = format_forecast_table()
    if fc_table:
        lines += ["", fc_table]
    lines += ["", "ดูสด: deepsleep456.com/usd"]
    return "\n".join(lines)


def format_forecast_table() -> str | None:
    """Fixed-width 10Y forecast table (US / TH / spread) for Telegram <pre> block."""
    try:
        fc = scrape_bond10y_forecasts({"US": "united-states", "TH": "thailand"})
    except Exception as e:
        print(f"  [FAIL -> omit] forecast table: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    us, th = fc["US"], fc["TH"]
    n = min(len(us["quarters"]), len(th["quarters"]), 4)
    labels = [q[0] for q in us["quarters"][:n]]

    def row(name, last, vals):
        return name.ljust(7) + f"{last:5.2f}" + "".join(f"{v:7.2f}" for v in vals)

    head = "10Y".ljust(7) + " Last" + "".join(f"{q:>7}" for q in labels)
    us_vals = [q[1] for q in us["quarters"][:n]]
    th_vals = [q[1] for q in th["quarters"][:n]]
    sp_vals = [u - t for u, t in zip(us_vals, th_vals)]
    table = "\n".join([
        head,
        row("US", us["last"], us_vals),
        row("TH", th["last"], th_vals),
        row("Spread", us["last"] - th["last"], sp_vals),
    ])
    return "📉 Bond 10Y Forecast (TradingEconomics)\n<pre>" + table + "</pre>"


def send_telegram(text: str) -> bool:
    if "PASTE_YOUR" in TELEGRAM_BOT_TOKEN or "PASTE_YOUR" in TELEGRAM_CHAT_ID:
        print("[telegram] token/chat_id not configured — printing message instead:\n")
        print(text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        # parse_mode HTML so the <pre> forecast table renders monospace/aligned
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=30)
        resp.raise_for_status()
        print("[telegram] sent OK")
        return True
    except Exception as e:
        print(f"[telegram] send FAILED: {e}", file=sys.stderr)
        return False


def job():
    print(f"\n=== fetching @ {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    data = fetch_all()
    send_telegram(format_message(data))


# ---------------------------------------------------------------------------
# 3) SCHEDULER
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
    print(f"scheduled daily at {', '.join(SEND_TIMES)} ({TIMEZONE}) — Ctrl+C to stop")

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
