# 🚀 MASTER 7 Bot - Advanced MT5 Algorithmic Trading System

**MASTER 7 Bot** คือระบบเทรดอัตโนมัติ (Algorithmic Trading) ระดับ Institutional Grade ที่ถูกออกแบบมาเพื่อทำงานร่วมกับ MetaTrader 5 (MT5) โดยมีความเชี่ยวชาญพิเศษในการเทรดทองคำ (XAUUSD) บนไทม์เฟรมระยะสั้น (M1, M15) ระบบถูกพัฒนาด้วย Python และมาพร้อมกับ Web Dashboard (FastAPI) สำหรับควบคุมการทำงานแบบ Real-time, ระบบจัดการความเสี่ยงขั้นสูง, และการแจ้งเตือนผ่าน Telegram

---

## 🛠️ สถาปัตยกรรมระบบ (Tech Stack)

* **Core Engine:** Python 3.10+, MetaTrader 5 API (`MetaTrader5`), Pandas, TA-Lib
* **Web Framework:** FastAPI, Uvicorn, Jinja2 Templates
* **Frontend:** HTML5, TailwindCSS (Responsive Design รองรับ Mobile & Desktop)
* **Process Manager:** PM2 (สำหรับการรันแบบ Background Service 24/7)
* **Web Server & SSL:** Caddy Server (Reverse Proxy ผูกโดเมน `master.e29ckg.org`)
* **Notifications:** Telegram Bot API
* **Data Persistence:** JSON Config & CSV Trade Logging

---

## 🌟 ฟีเจอร์เด่น (Core Features)

### 1. 🎯 Precision Entry Logic (ระบบวิเคราะห์จุดเข้า)

* **Trend Following + Pullback:** ใช้การตัดกันของเส้น EMA (Fast/Slow) เพื่อดูเทรนด์ และใช้ EMA Entry สำหรับหาจุดย่อตัว (Pullback)
* **Momentum Filter:** กรองความแรงของเทรนด์ด้วยค่า ADX (Average Directional Index) เพื่อหลีกเลี่ยงสภาวะตลาด Sideways
* **Price Action Rejection:** วิเคราะห์ % ไส้เทียน (Wick Percentage) เพื่อยืนยันแรงซื้อ/ขายที่แนวรับแนวต้าน

### 2. 🛡️ Advanced Risk Management (ระบบป้องกันพอร์ตระดับสถาบัน)

* **Max Daily Drawdown (Stop Bot):** ระบบคำนวณ P/L รายวันแบบ Real-time หากขาดทุนรวม (Closed + Floating) ทะลุกำหนด ระบบจะปิดทุกออเดอร์และหยุดการทำงาน (Offline) อัตโนมัติในวันนั้น
* **Hard Cut Loss (Basket Protection):** หากการแก้ไม้ (DCA) ผิดทางและทำให้ตะกร้าติดลบเกิน % ที่ตั้งไว้ ระบบจะสละอวัยวะ (Cut Loss) ทันทีเพื่อรักษาพอร์ตหลัก
* **Dynamic Lot Sizing:** คำนวณ Lot Size อัตโนมัติตามระยะ Stop Loss (ATR) และ % ความเสี่ยงที่รับได้ต่อไม้ (Risk per trade)
* **MT5 Auto-Reconnect:** มี Background Task คอยตรวจสอบการเชื่อมต่อกับโบรกเกอร์ หากหลุดจะทำการ Reconnect อัตโนมัติ

### 3. 💰 Smart Profit Management (ระบบจัดการกำไรขั้นสูง)

* **Break-Even with Extra Points:** เลื่อน Stop Loss มาบังหน้าทุนอัตโนมัติเมื่อกำไรถึงเป้า พร้อมระบุ "กำไรติดปลายนวม (Points)" เพื่อครอบคลุมค่าคอมมิชชันและสเปรด
* **ATR-Based Trailing Stop:** ขยับ Stop Loss ตามก้นราคาเพื่อรันเทรนด์ (Let profit run) อย่างยืดหยุ่นตามความผันผวน (ATR)
* **Smart DCA (Martingale):** ระบบแก้ไม้อัตโนมัติเมื่อผิดทาง โดยระยะแก้ไม้ (Step) จะขยายกว้างขึ้นหรือแคบลงตามความผันผวน (ATR) พร้อมกำหนดเป้าหมายกำไรตอนรวบปิดตะกร้า (Target Profit) ได้

### 4. 📰 Institutional-Grade Market Filters (ตัวกรองสภาพตลาด)

* **Multi-Timeframe (MTF) Alignment:** บังคับให้บอทเปิดไม้เฉพาะทิศทางเดียวกับเทรนด์ใหญ่ (เช่น H1) เพื่อลดสัญญาณหลอกบน M1
* **Max Spread Filter:** ตรวจสอบค่า Spread แบบ Real-time หากโบรกเกอร์ถ่างสเปรดเกินกำหนด บอทจะงดเข้าออเดอร์
* **Trading Session Window:** กำหนดชั่วโมงเปิด-ปิดรับออเดอร์ เพื่อหลีกเลี่ยงช่วงเวลาตลาดไร้สภาพคล่อง
* **High-Impact News Shield:** ดึงข้อมูลปฏิทินเศรษฐกิจ (ForexFactory) อัตโนมัติ และสั่งหยุดเทรด/หยุดแก้ไม้ ก่อนและหลังข่าวกล่องแดง (USD) ออก

### 5. 💻 Web Dashboard & Logging (ระบบมอนิเตอร์และควบคุม)

* **Real-time Dashboard:** ดูสถานะ CPU/RAM/Disk, ยอด Balance/Equity, กำไรรายวัน (Daily P/L), และไม้ที่กำลังถืออยู่ได้จากทุกที่
* **Dynamic Configuration:** ปรับเปลี่ยนค่าพารามิเตอร์ทุกตัวได้ผ่านหน้าเว็บ (UI รองรับมือถือ 100%) โดยการตั้งค่าจะถูกบันทึกลงไฟล์ `config.json` ถาวร
* **Trade Logs Viewer:** ระบบบันทึกประวัติการเทรดลง `trade_log.csv` อัตโนมัติ พร้อมหน้าต่างเรียกดู Log บนเว็บ (ระบุเวลา, Ticket, เหตุผลที่เข้า/ออก, ค่า Indicators, และ Net Profit)
* **One-Click Panic Button:** ปุ่ม "CLOSE ALL" สำหรับสั่งปิดทุกออเดอร์ในพอร์ตทันทีผ่านหน้าเว็บ

---

## ⚙️ การติดตั้งและการรันระบบ (Deployment Guide)

### สิ่งที่ต้องมี (Prerequisites)

* Windows Server หรือ Windows VPS
* MetaTrader 5 (Log in บัญชีเทรดทิ้งไว้และเปิด Allow Algo Trading)
* Python 3.10+
* Node.js (สำหรับใช้งาน PM2)
* Caddy Server (สำหรับจัดการ Domain และ SSL)

### ขั้นตอนการรันระบบ

1. **สร้าง Virtual Environment และติดตั้งไลบรารี:**
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

```


2. **ตั้งค่า Environment Variables:**
สร้างไฟล์ `.env` และใส่ข้อมูล:
```env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
MT5_SYMBOL=XAUUSD
TIMEFRAME=M1
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=use-a-long-random-password-here

```

> ระบบจะปฏิเสธการเปิด Dashboard หากไม่ได้กำหนด `DASHBOARD_PASSWORD` เพื่อป้องกันบุคคลภายนอกควบคุมบัญชี MT5


3. **รันเซิร์ฟเวอร์ด้วย PM2 (Background Process):**
คลิกที่ไฟล์ `start.bat` ระบบจะรัน Uvicorn บนพอร์ต `8000` ทันที
4. **ตั้งค่า Domain ด้วย Caddy:**
ตรวจสอบใน `Caddyfile`:
```caddyfile
master.e29ckg.org {
    reverse_proxy 127.0.0.1:8000
}

```


สั่ง Reload Caddy: `caddy reload`

### Backtest XAUUSDc M1 (3 เดือน / ทุน $1,000)

เปิด MT5 และล็อกอินบัญชีของโบรกเกอร์ที่มีสัญลักษณ์ `XAUUSDc` จากนั้นรัน:

```powershell
python backtest_xauusdc.py --symbol XAUUSDc --start 2026-06-17 --end 2026-09-17 --balance 1000 --risk-percent 0.25
```

ผลลัพธ์จะถูกบันทึกใน `backtest_results/xauusdc_m1_summary.json` และ
`backtest_results/xauusdc_m1_trades.csv` หากบัญชีมีค่าคอมมิชชัน ให้เพิ่ม
`--commission-per-lot` ตามค่ารอบเปิด-ปิดต่อ 1 lot ของโบรกเกอร์

---

## 📈 วิธีการปรับจูน (Fine-Tuning สำหรับ XAUUSD M1)

เนื่องจากทองคำ 1 นาทีมีความผันผวนสูงมาก แนะนำให้เริ่มต้นด้วยค่าเหล่านี้:

* **ADX Threshold:** `25.0` - `30.0` (กรองสัญญาณหลอก)
* **BE Trigger (ATR):** `2.0` - `3.0` (กันกราฟสะบัดกินทุน)
* **BE Profit (Points):** `20.0` - `50.0` (เผื่อค่าคอมมิชชันและสเปรด)
* **DCA Step (ATR):** `4.0` - `6.0` (ถ่างระยะแก้ไม้ให้ปลอดภัย)
* **DCA Target Profit ($):** `3.0` (ตั้งเผื่อ Net Profit หักค่าธรรมเนียม)

---

*Developed by E29CKG | Architecture by Master 7 System*
