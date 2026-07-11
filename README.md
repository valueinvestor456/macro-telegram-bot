# Macro Telegram Bot

สคริปต์ Python ดึงข้อมูลมหภาค (DXY, Gold, US10Y, TH10Y, USD/THB, BDI) แล้วส่งสรุปเข้า
Telegram อัตโนมัติทุกวันเวลา 08:00, 10:00, 14:00, 16:00 (เวลาไทย)

## ติดตั้ง

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## ตั้งค่า Telegram Bot (ครั้งเดียว)

1. **สร้างบอท**: เปิด Telegram คุยกับ `@BotFather` → พิมพ์ `/newbot` → ตั้งชื่อ →
   จะได้ **Bot Token** หน้าตาแบบ `1234567890:AAF...xyz`
2. **หา Chat ID**: กดเริ่มแชท (Start) กับบอทที่เพิ่งสร้าง แล้วส่งข้อความอะไรก็ได้ 1 ข้อความ
   จากนั้นเปิด URL นี้ในเบราว์เซอร์ (แทน `<TOKEN>` ด้วย token จริง):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   → หาเลข `"chat":{"id": 123456789}` — นั่นคือ **Chat ID**
3. **ใส่ค่าให้สคริปต์**: สร้างไฟล์ `.env` ในโฟลเดอร์นี้ (มีตัวอย่างอยู่แล้ว) —
   สคริปต์อ่านไฟล์นี้เองอัตโนมัติ ไม่ต้องตั้ง environment variable ด้วยมือ:
   ```
   TELEGRAM_BOT_TOKEN=1234567890:AAF...xyz
   TELEGRAM_CHAT_ID=123456789
   ```
   ⚠️ `.env` อยู่ใน `.gitignore` แล้ว — **ห้าม commit ไฟล์นี้ขึ้น git หรือแชร์ token
   ให้ใคร** ใครมี token ก็ส่งข้อความผ่านบอทเราได้เลย ถ้า token หลุดให้คุยกับ
   @BotFather พิมพ์ `/revoke` เพื่อออก token ใหม่

## ใช้งาน

```bash
python main.py --once    # ทดสอบ: ดึงข้อมูล + ส่งทันที 1 ครั้ง
python main.py           # รันค้างไว้ ส่งอัตโนมัติตามเวลา (ปิดหน้าต่าง = หยุด)
```

ถ้ายังไม่ได้ใส่ token สคริปต์จะพิมพ์ข้อความลงจอแทนการส่งจริง — ใช้เช็ค format ได้เลย

## แหล่งข้อมูล & ข้อจำกัด

- DXY (`DX-Y.NYB`), Gold futures (`GC=F`), US10Y (`^TNX`), USD/THB (`THB=X`) — จาก
  Yahoo Finance ผ่าน `yfinance` (ไม่ใช่ investpy — ตัวนั้นตายแล้วตั้งแต่ Investing.com
  บล็อก API เมื่อหลายปีก่อน)
- TH10Y และ BDI — scrape จาก TradingEconomics (element `id="p"`/`id="pch"` ตรวจกับ
  หน้าเว็บจริงแล้ว ณ วันที่เขียน) — **โครงหน้าเว็บเปลี่ยนได้ทุกเมื่อ** ถ้าตัวไหนพัง
  ข้อความจะแสดง `N/A ⚠️` แทน ไม่ crash และตัวอื่นยังส่งปกติ
- ตาราง Bond 10Y Forecast (US/TH/Spread รายไตรมาส) — scrape จาก
  tradingeconomics.com/forecast/government-bond-10y โดย match แถวประเทศจาก href slug
  (แถว US บนหน้าใช้ชื่อ "US" ไม่ใช่ "United States") ถ้าดึงไม่ได้จะตัดตารางออกจาก
  ข้อความเฉยๆ ส่วนอื่นส่งปกติ — ค่า forecast เป็นประมาณการของ TradingEconomics
  ไม่ใช่ข้อเท็จจริง
- เวลา schedule ใช้ timezone `Asia/Bangkok` โดยตรง เครื่องไม่จำเป็นต้องตั้งเวลาไทย
- สคริปต์ต้องรันค้างไว้ (เครื่องเปิด + ไม่ sleep) — ถ้าต้องการให้ส่งแม้เครื่องปิด
  ค่อยย้ายไปรันบน cloud (เช่น GitHub Actions cron / VPS) ภายหลัง
