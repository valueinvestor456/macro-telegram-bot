# Macro Telegram Bot

## Thai Stock Updates — สรุปใหม่สำหรับติดตามการลงทุน

- ส่งวันละสองรอบ **09:00 และ 14:00 Asia/Bangkok** ผ่าน `stock-digest.yml` (GitHub อาจเริ่มงานช้ากว่า cron)
- คัดไม่เกิน 5 รายการ: ประกาศบริษัท SET ที่มีสาระ, การได้มา/จำหน่ายหุ้นแบบ 246-2,
  และซื้อขายหุ้นสามัญของผู้บริหาร/บุคคลเกี่ยวข้องแบบ 59 ที่มีมูลค่าตั้งแต่ 1 ล้านบาทต่อรายการ
- อ่านรายการข่าวจากหน้า [ข่าว SET](https://www.set.or.th/th/market/news-and-alert/news) โดยตรงผ่าน API ที่หน้าเว็บใช้
  อ่านครบทุกหน้าภายในช่วงย้อนหลัง 7 วัน เพื่อครอบคลุมข่าวหลังตลาดปิด/วันหยุด แล้วกันซ้ำระหว่างรอบเช้าและบ่าย
- คัดจากหัวข้อข่าว: เปลี่ยน CEO/CFO/ประธานกรรมการ, เปลี่ยนธุรกิจ,
  ซื้อขายกิจการ, ผู้ถือหุ้นใหญ่, ทุน, สัญญา และความเสี่ยงกิจการ
- ส่งเฉพาะ **ชื่อหุ้น + หัวข้อ + ลิงก์** ไม่มีบทสรุปหรือบทวิเคราะห์ ไม่ดาวน์โหลด/อ่าน PDF
  ใช้กฎคัดกรองในโปรแกรม ไม่เรียก LLM จึงไม่มีค่า token สำหรับการคัดและส่งแต่ละรอบ
- ตัดประกาศเสนอวาระ/เสนอชื่อกรรมการ วันหยุด รายงานซื้อหุ้นคืนรายวัน และรายงานใช้เงินตามรอบ
  การอนุมัติโครงการซื้อหุ้นคืนใหม่ยังเข้าข่าย
- แบบ 59 ใช้ SEC โดยตรงเพื่อยืนยันคนทำรายการ จำนวน ราคา และวันทำรายการ; ให้ความสำคัญกับการซื้อก่อนการขาย
- หัวข้อแบบ 59 ระบุวันทำรายการ; ไม่นำแบบ 59 ไปจัดเป็น IPO
- ตัดรายงานยกเลิก รายการที่ราคา/จำนวนไม่ครบ และข่าวที่ส่งแล้ว; ข่าวหุ้นเดียวกันแต่คนละรายการ
  ยังคงแยกกันและแสดงไม่เกิน 2 รายการต่อหุ้น ไม่มีการรวมยอดที่อาจนับซ้ำระหว่างผู้รายงาน/คู่สมรส
- หากไม่มีรายการใหม่ตามเกณฑ์ จะไม่ส่งข้อความว่าง; ข้อผิดพลาดของแหล่งข้อมูลบันทึกใน workflow log
  หากไม่มีข้อมูลเลยและมีแหล่งข้อมูลเสีย งานจะล้มเหลวเพื่อให้ตรวจพบได้

ตั้ง GitHub Actions repository variables (ไม่จำเป็นต้องตั้งเพื่อเริ่มใช้):

| Variable | Default | ความหมาย |
| --- | --- | --- |
| `STOCK_WATCHLIST` | ว่าง | หุ้นที่ผู้ใช้เลือก เช่น `CPALL,BDMS`; เรียงก่อนและยกเว้นเกณฑ์มูลค่าแบบ 59 ไม่ได้แปลว่าเป็นหุ้นในพอร์ต |
| `STOCK_MIN_INSIDER_VALUE` | `1000000` | มูลค่าขั้นต่ำต่อรายการแบบ 59 = จำนวน × ราคา; เป็นเกณฑ์คัดข่าว ไม่ใช่ระดับนัยสำคัญทางกฎหมาย |

```powershell
python -X utf8 -m unittest discover -s tests -v
python main.py --stocks --dry-run  # ตรวจแหล่งจริง ไม่ส่ง/ไม่เปลี่ยนประวัติ
python main.py --stocks           # ส่งจริงไปยัง chat ที่ตั้งไว้
```

ประวัติ `stock_digest_state.json` บันทึกแบบ atomic หลัง Telegram ยืนยันสำเร็จเท่านั้น เก็บ 60 วัน
บน GitHub ใช้ Actions cache และ concurrency เพื่อให้รอบส่งไม่ทับกัน; dry-run ไม่เขียน cache
cache เป็น best-effort: ถ้าถูกลบ/หมดอายุ หรือเครื่องล้มหลังส่งแต่ก่อนบันทึก อาจส่งซ้ำได้
เมื่อรันบน GitHub polling loop จะไม่ส่งสรุปหุ้นอีก เพื่อให้มี workflow เดียวรับผิดชอบ
การเปิดใช้โค้ดใหม่ต้อง restart polling run เก่าที่โหลดโค้ดเดิมอยู่ด้วย

ขอบเขตข้อมูล: รายการประจำวันที่ SEC เปิดหน้าให้ดู (ย้อนหลังวันทำรายการไม่เกิน 31 วัน),
ข่าว SET ย้อนหลัง 7 วัน สูงสุด 20 หน้า × 100 รายการ หากรายการไม่ครบหรือ API เปลี่ยนจะแจ้งข้อผิดพลาด
หัวข้อทั่วไป เช่น “แจ้งมติคณะกรรมการ” ที่ไม่ระบุประเด็นสำคัญอาจไม่ผ่านเกณฑ์ เพราะไม่อ่านเอกสารแนบ
ลิงก์แบบ 246-2 อาจเปิดเป็น PDF เมื่อผู้ใช้กด แต่โปรแกรมไม่ได้ดาวน์โหลดเอกสารนั้น
ข้อมูล SEC ต้องมีวันเผยแพร่ไม่เกิน 7 วัน เพื่อรองรับวันหยุด
สคริปต์ไม่อ่านพอร์ตหรือสมมติฐานลงทุนจาก wiki โดยอัตโนมัติ

สคริปต์ Python ดึงข้อมูลมหภาค (DXY, Gold, US10Y, TH10Y, USD/THB, BDI) แล้วส่งสรุปเข้า
Telegram อัตโนมัติทุกวันเวลา 09:45, 10:00, 11:00, 14:00, 15:00, 16:00, 16:15, 19:30, 20:30 (เวลาไทย)

**Bot Status**: ✅ Active — scheduled sends via GitHub Actions

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
   หาก token เคยอยู่ในไฟล์ที่ track ด้วย git ให้ถือว่ารั่วแล้ว แม้ลบออกจากไฟล์ปัจจุบัน
   ก็ยังอยู่ในประวัติ commit: ให้ revoke ผ่าน @BotFather แล้วเปลี่ยนทั้ง `.env` และ
   GitHub Actions secret `TELEGRAM_BOT_TOKEN` ก่อนเปิด poller ใหม่

## ใช้งาน

```bash
python main.py --once    # ทดสอบ: ดึงข้อมูล + ส่งทันที 1 ครั้ง
python main.py           # รันค้างไว้ ส่งอัตโนมัติตามเวลา (ปิดหน้าต่าง = หยุด)
```

ถ้ายังไม่ได้ใส่ token สคริปต์จะพิมพ์ข้อความลงจอแทนการส่งจริง — ใช้เช็ค format ได้เลย

**สำคัญ:** `python main.py` ใช้ Telegram `getUpdates` สำหรับรับคำสั่ง และเปิดพร้อมกันได้
เพียงหนึ่ง instance ต่อ bot token เท่านั้น ถ้าใช้ GitHub Actions `Telegram Bot Polling Loop`
อยู่แล้ว ห้ามเปิดบอทซ้ำบน PC/VPS หรือ Railway/Fly ด้วย token เดียวกัน (และกลับกัน:
ถ้าเลือก PC/VPS ให้ปิด polling workflow บน GitHub Actions) การเปิดซ้ำทำให้ Telegram
ตอบ `409 Conflict` และคำสั่งอย่าง `/usd` จะไม่ถูกรับ

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
