# 📈 MASTER 7 - MT5 Trading Bot & Dashboard

ระบบบอทแจ้งเตือนสัญญาณการเทรดผ่าน Telegram และ Web Dashboard แบบ Real-time โดยดึงข้อมูลโดยตรงจากโปรแกรม MetaTrader 5 (MT5) พัฒนาด้วย Python (FastAPI)

## ✨ ฟีเจอร์หลัก (Features)
- **Web Dashboard:** แสดงสถานะเซิร์ฟเวอร์ (CPU, RAM, Disk), ข้อมูลพอร์ตโฟลิโอ (Balance, Equity, Margin, P/L), สถานะราคาแบบ Real-time, โครงสร้างเทรนด์ (Trend Matrix) และรายการออเดอร์ที่เปิดอยู่ (Active Positions)
- **Trading Logic:** คำนวณจุดเข้าซื้อขาย (Buy/Sell) จากอินดิเคเตอร์ EMA (50, 100, 8), ADX และ Price Action (Wick Rejection)
- **Telegram Alert:** ส่งแจ้งเตือนสัญญาณ Buy/Sell เข้า Telegram อัตโนมัติ

---

## 📋 สิ่งที่ต้องเตรียมก่อนติดตั้ง (Prerequisites)
1. **ระบบปฏิบัติการ Windows** (ไลบรารี `MetaTrader5` ของ Python รองรับเฉพาะ Windows เท่านั้น)
2. **Python 3.9 - 3.11** (แนะนำให้ใช้ Python 3.11 เพื่อความเข้ากันได้ของไลบรารี)
3. โปรแกรม **MetaTrader 5 (MT5)** ที่ติดตั้งและล็อกอินเข้าบัญชีเทรดเรียบร้อยแล้ว
4. **Telegram Bot Token** (สร้างได้จาก [@BotFather](https://t.me/BotFather) บน Telegram)
5. **Telegram Chat ID** (ไอดีห้องแชทที่จะให้บอทส่งข้อความไปหา)

---

## 🚀 ขั้นตอนการติดตั้ง (Installation)

### 1. โครงสร้างโฟลเดอร์
สร้างโฟลเดอร์สำหรับโปรเจกต์ (เช่น `btc_bot`) และจัดโครงสร้างไฟล์ดังนี้:
```text
btc_bot/
├── .env
├── requirements.txt
├── main.py
└── templates/
    └── index.html

```

### 2. สร้าง Virtual Environment

เปิด Terminal หรือ Command Prompt ในโฟลเดอร์โปรเจกต์ แล้วรันคำสั่ง:

```bash
# สร้าง venv
python -m venv venv

# เปิดใช้งาน venv (สำหรับ Windows)
venv\Scripts\activate

```

### 3. ติดตั้งไลบรารี (Dependencies)

ตรวจสอบให้แน่ใจว่าในไฟล์ `requirements.txt` มีข้อมูลดังนี้:

```text
fastapi
uvicorn
MetaTrader5
pandas
ta
psutil
python-dotenv
requests
jinja2

```

รันคำสั่งติดตั้ง:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt

```

### 4. การตั้งค่า Environment Variables

เปิดไฟล์ `.env` และแก้ไขข้อมูลให้ตรงกับของคุณ:

```env
TELEGRAM_BOT_TOKEN=ใส่_Token_ของบอทที่นี่
TELEGRAM_CHAT_ID=ใส่_Chat_ID_ที่นี่
MT5_SYMBOL=BTCUSD   # ชื่อคู่เงิน/คริปโต ต้องพิมพ์ให้ตรงกับใน MT5 เป๊ะๆ (เช่น BTCUSDm, XAUUSDc)
TIMEFRAME=M15       # Timeframe ที่ใช้รันบอท (M1, M5, M15, M30, H1, H4, D1)

```

---

## ▶️ วิธีการรันระบบ (Running the App)

1. **เปิดโปรแกรม MetaTrader 5 (MT5) ทิ้งไว้** และตรวจสอบว่าเชื่อมต่ออินเทอร์เน็ต/ล็อกอินบัญชีแล้ว
2. ไปที่ MT5 > หน้าต่าง Market Watch (ซ้ายมือ) > เช็คว่ามีคู่เหรียญที่ตั้งไว้ใน `MT5_SYMBOL` แสดงอยู่
3. เปิด Terminal (ที่เปิดใช้งาน venv แล้ว) รันคำสั่ง:
```bash
uvicorn main:app --reload

```


4. เปิด Web Browser ไปที่: **http://127.0.0.1:8000**
5. กดปุ่ม **"START BOT"** บนหน้าเว็บเพื่อเริ่มให้บอททำงาน

---

## 🛠️ ปัญหาที่พบบ่อย (Troubleshooting)

**1. ราคาบนเว็บเป็น 0.00 หรือ ไม่แสดงข้อมูลอินดิเคเตอร์**

* **สาเหตุ:** Python เชื่อมต่อ MT5 ไม่ได้ หรือหาชื่อคู่เทรดไม่เจอ
* **วิธีแก้:** เช็คไฟล์ `.env` ว่าตัวแปร `MT5_SYMBOL` พิมพ์อักษรตัวเล็ก-ใหญ่ตรงกับใน MT5 เป๊ะๆ หรือไม่ (เช่น โบรกเกอร์ Exness มักจะต่อท้ายด้วย m หรือ c เช่น `BTCUSDm`) และต้องแน่ใจว่าคู่เหรียญนั้นเปิดแสดงอยู่ในหน้าต่าง Market Watch ของโปรแกรม MT5

**2. Error: TypeError: unhashable type: 'dict' ตอนเข้าหน้าเว็บ**

* **สาเหตุ:** ใช้ FastAPI / Starlette เวอร์ชันใหม่
* **วิธีแก้:** ตรวจสอบโค้ดใน `main.py` ตรงบรรทัด `@app.get("/")` ต้องเขียนเป็น:
`return templates.TemplateResponse(request=request, name="index.html", context={"state": bot_state})`

**3. ติดตั้งไลบรารีไม่ได้ (หา pandas-ta ไม่เจอ)**

* โปรเจกต์นี้ได้เปลี่ยนไปใช้ไลบรารี `ta` แทน `pandas-ta` แล้ว เพื่อแก้ปัญหาเวอร์ชัน Python ขัดแย้งกัน ตรวจสอบว่าในโค้ด `main.py` ใช้ `import ta` ไม่ใช่ `import pandas_ta`
