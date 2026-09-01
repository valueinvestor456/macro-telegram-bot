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

## การใช้ GitHub Actions (การส่งแบบกำหนดการ บน Cloud ฟรี)

หากต้องการให้บอทส่งสรุปอัตโนมัติแม้ PC ปิด สามารถใช้ GitHub Actions ฟรี:

1. **Push ขึ้น GitHub**:
   ```bash
   cd d:\VSCODE\macro-telegram-bot
   git add .
   git commit -m "Add GitHub Actions scheduled send"
   git push
   ```

2. **เพิ่ม Secrets ใน GitHub**:
   - ไปที่ repo → Settings → Secrets and variables → Actions
   - Click "New repository secret"
   - เพิ่ม 2 secrets:
     - `TELEGRAM_BOT_TOKEN` = `1234567890:AAF...xyz` (token จาก @BotFather)
     - `TELEGRAM_CHAT_ID` = `123456789` (chat ID จาก getUpdates)

3. **ตรวจสอบ Workflow**:
   - ไปที่ repo → Actions
   - เลือก "Scheduled Macro Summary Send"
   - Workflow จะรันอัตโนมัติตามเวลา (09:45, 10:00, 11:00, 14:00, 15:00, 16:00, 16:15, 19:30, 20:30 เวลาไทย)
   - หรือคลิก "Run workflow" → "Run workflow" เพื่อทดสอบทันที

**หมายเหตุ**:
- GitHub Actions ฟรี — ไม่มีค่าใช้จ่าย
- Workflow รันเฉพาะการส่งแบบกำหนดการ (ไม่มี `/usd` reply แบบ real-time)
- สำหรับ `/usd` command replies ต้องให้บอทรันบน PC หรือ VPS 24/7

## 🔧 Troubleshooting: บอทไม่ส่งข้อความ

ถ้าบอทหยุดส่งข้อความนาน ให้ตรวจสอบตามนี้:

### 1. **ตรวจสอบ Workflow Status ใน GitHub**
   - ไปที่ repo → Actions
   - ดูส่วน "Scheduled Macro Summary Send" 
   - ถ้า workflow เป็นสีเหลืองหรือแดง = มีปัญหา (คลิกดูรายละเอียด)
   - ถ้าหายไป = **GitHub Actions อาจถูกปิด** หรือ **Secrets หายไป**

### 2. **ตรวจสอบ Secrets ใน GitHub**
   - ไปที่ Settings → Secrets and variables → Actions
   - ต้องมี 2 secrets:
     - ✅ `TELEGRAM_BOT_TOKEN` (ต้องไม่ว่าง)
     - ✅ `TELEGRAM_CHAT_ID` (ต้องไม่ว่าง)
   - ถ้าหายไป = **เพิ่มใหม่** ตามขั้นตอน "GitHub Actions" ด้านบน

### 3. **ทดสอบด้วย Manual Trigger**
   - ไปที่ repo → Actions → "Scheduled Macro Summary Send"
   - คลิก "Run workflow" → "Run workflow"
   - รอให้ workflow จบ (5-10 วินาที)
   - ตรวจสอบ:
     - ✅ Workflow เสร็จสีเขียว = OK ค่าใช้ได้
     - ❌ ล้มเหลว = ดูรายละเอียด error ที่ step "Send Macro Summary"

### 4. **ถ้าทั้งหมดดูดี แต่ยังไม่ส่ง**
   - Workflow อาจถูกปิดโดย GitHub (ไม่มี commit เกิน 60 วัน)
   - ✅ **วิธีแก้**: Push commit ใหม่ (แม้แต่การเปลี่ยนแปลงเล็กน้อย) จะเปิด workflow ใหม่
   - ตัวอย่าง:
     ```bash
     echo "# Bot active - $(date)" >> README.md
     git add README.md && git commit -m "Keep workflow active" && git push
     ```

### 5. **ตรวจสอบเวลา Cron ถูกต้อง**
   - Workflow ตั้งเวลา UTC และแปลงเป็นเวลาไทย (UTC+7)
   - ถ้าดูเหมือนเวลาผิด ให้ตรวจสอบ `.github/workflows/scheduled-send.yml`
