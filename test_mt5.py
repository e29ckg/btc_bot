import MetaTrader5 as mt5
from dotenv import load_dotenv
import os

load_dotenv()
SYMBOL = os.getenv("MT5_SYMBOL", "BTCUSD")

if not mt5.initialize():
    print("❌ เชื่อมต่อ MT5 ไม่สำเร็จ (โปรแกรมเปิดอยู่ไหม?)")
    print("Error:", mt5.last_error())
    quit()

print("✅ เชื่อมต่อ MT5 สำเร็จ!")

# ลองเช็คว่ามี Symbol นี้ในโบรกเกอร์หรือไม่
symbol_info = mt5.symbol_info(SYMBOL)
if symbol_info is None:
    print(f"❌ ไม่พบเหรียญ '{SYMBOL}' ใน MT5 ของคุณ กรุณาเช็คชื่อเหรียญให้ถูกต้อง")
    mt5.shutdown()
    quit()

if not symbol_info.visible:
    print(f"⚠️ เหรียญ '{SYMBOL}' ไม่ได้แสดงใน Market Watch กำลังพยายามเพิ่ม...")
    if not mt5.symbol_select(SYMBOL, True):
        print("❌ ไม่สามารถเพิ่มเหรียญลงใน Market Watch ได้")
        mt5.shutdown()
        quit()

rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M15, 0, 5)
if rates is None:
    print(f"❌ ดึงข้อมูลราคาไม่ได้ Error:", mt5.last_error())
else:
    print(f"✅ ดึงราคาสำเร็จ! ราคาปิดล่าสุด: {rates[-1]['close']}")

mt5.shutdown()