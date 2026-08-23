import os
import time
import json
import csv
import asyncio
import requests
import MetaTrader5 as mt5
import pandas as pd
import ta
import psutil
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel 

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SYMBOL = os.getenv("MT5_SYMBOL", "XAUUSD")
TIMEFRAME_STR = os.getenv("TIMEFRAME", "M1")

CONFIG_FILE = "config.json"
TRADE_LOG_FILE = "trade_log.csv"

CONFIG = {
    "emaFastLen": 50, "emaSlowLen": 100, "emaEntryLen": 8,
    "adxLen": 14, "adxThresh": 25.0, "minWickPct": 5.0,
    "atrLen": 14, "atrMultiplier": 1.5, "rrRatio": 2.0, "riskPercent": 1.0,
    "magicNumber": 777777,
    "enableBE": True, "beTriggerATR": 2.0, "beProfitPoints": 20.0,
    "enableTrailing": True, "trailStartATR": 3.0, "trailDistanceATR": 1.0,
    "enableDCA": True, "dcaStepATR": 4.0, "dcaMultiplier": 1.5, "maxDCA": 3,
    "dcaTargetProfit": 3.0,
    "maxBasketRisk": 5.0,
    "hardCutRisk": 6.0,
    "maxDailyDrawdownRisk": 10.0,
    "enableNewsFilter": True,
    "newsBeforeMin": 30,
    "newsAfterMin": 30,
    "newsCurrencies": ["USD"],
    "enableMTFFilter": True,
    "maxSpreadPoints": 50.0,
    "enableSessionFilter": False,
    "sessionStartHour": 13,
    "sessionEndHour": 23
}

def load_config():
    global CONFIG
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                saved_config = json.load(f)
                CONFIG.update(saved_config)
        except Exception as e: print(f"Error loading config: {e}")

def save_config():
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(CONFIG, f, indent=4)
    except Exception as e: print(f"Error saving config: {e}")

load_config()

class ConfigModel(BaseModel):
    emaFastLen: int; emaSlowLen: int; emaEntryLen: int
    adxLen: int; adxThresh: float; minWickPct: float
    atrLen: int; atrMultiplier: float; rrRatio: float; riskPercent: float
    magicNumber: int
    enableBE: bool; beTriggerATR: float; beProfitPoints: float
    enableTrailing: bool; trailStartATR: float; trailDistanceATR: float
    enableDCA: bool; dcaStepATR: float; dcaMultiplier: float; maxDCA: int
    dcaTargetProfit: float 
    maxBasketRisk: float; hardCutRisk: float; maxDailyDrawdownRisk: float
    enableNewsFilter: bool; newsBeforeMin: int; newsAfterMin: int
    enableMTFFilter: bool; maxSpreadPoints: float
    enableSessionFilter: bool; sessionStartHour: int; sessionEndHour: int

app = FastAPI()
templates = Jinja2Templates(directory="templates")

bot_state = {
    "is_running": False, "auto_trade": False, "latest_price": 0,
    "signal": "WAIT", "last_update": "", "indicators": {},
    "tp": 0.0, "sl": 0.0, "lot": 0.0,
    "news_status": {"is_blocked": False, "reason": "No major news near", "next_news": "-"},
    "market_filter_status": {"is_filtered": False, "reason": "All conditions clear"}
}

known_tickets = set()
cached_news = []

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"})

def log_trade(ticket, action_str, symbol, volume, price, reason, profit=0.0):
    file_exists = os.path.isfile(TRADE_LOG_FILE)
    try:
        with open(TRADE_LOG_FILE, mode='a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["Timestamp", "Ticket", "Action", "Symbol", "Volume", "Price", "Reason", "EMA_Fast", "EMA_Slow", "ADX", "ATR", "Profit"])

            inds = bot_state.get("indicators", {})
            ema50 = inds.get("EMA 50", 0)
            ema100 = inds.get("EMA 100", 0)
            adx = inds.get("ADX", 0)
            atr = inds.get("ATR", 0)

            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ticket, action_str, symbol, volume, price, reason,
                ema50, ema100, adx, atr, round(profit, 2)
            ])
    except Exception as e:
        print(f"Error saving trade log: {e}")

async def mt5_connection_monitor():
    is_connected = True
    while True:
        terminal = mt5.terminal_info()
        if terminal is None or not terminal.connected:
            if is_connected:
                send_telegram("⚠️ *MT5 DISCONNECTED*\nขาดการเชื่อมต่อกับเซิร์ฟเวอร์โบรกเกอร์! ระบบกำลังพยายามเชื่อมต่อใหม่...")
                is_connected = False
            if mt5.initialize():
                terminal_retry = mt5.terminal_info()
                if terminal_retry and terminal_retry.connected:
                    send_telegram("✅ *MT5 RECONNECTED*\nระบบสามารถเชื่อมต่อกับเซิร์ฟเวอร์ได้สำเร็จและพร้อมทำงานต่อแล้ว!")
                    is_connected = True
        else:
            if not is_connected:
                send_telegram("✅ *MT5 RECONNECTED*\nการเชื่อมต่อกลับมาเป็นปกติแล้ว!")
                is_connected = True
        await asyncio.sleep(10)

def fetch_economic_calendar():
    global cached_news
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200: cached_news = res.json()
    except Exception as e: print(f"Error fetching economic calendar: {e}")

def check_news_impact():
    if not CONFIG.get("enableNewsFilter", True) or not cached_news:
        bot_state["news_status"] = {"is_blocked": False, "reason": "News filter disabled", "next_news": "-"}
        return False
    now_utc = datetime.now(timezone.utc)
    before_delta = timedelta(minutes=CONFIG.get("newsBeforeMin", 30))
    after_delta = timedelta(minutes=CONFIG.get("newsAfterMin", 30))
    currencies = CONFIG.get("newsCurrencies", ["USD"])
    blocked, block_reason, next_news_str = False, "", "-"
    min_upcoming_diff = None

    for item in cached_news:
        if item.get("impact") != "High" or item.get("country") not in currencies: continue
        date_str = item.get("date")
        if not date_str: continue
        try:
            news_time = datetime.fromisoformat(date_str)
            if news_time.tzinfo is None: news_time = news_time.replace(tzinfo=timezone.utc)
        except Exception: continue

        if news_time > now_utc:
            diff = (news_time - now_utc).total_seconds()
            if min_upcoming_diff is None or diff < min_upcoming_diff:
                min_upcoming_diff = diff
                local_news_time = news_time.astimezone()
                next_news_str = f"{item.get('title')} ({item.get('country')}) @ {local_news_time.strftime('%H:%M')}"

        if (news_time - before_delta) <= now_utc <= (news_time + after_delta):
            blocked = True
            local_news_time = news_time.astimezone()
            block_reason = f"High Impact: {item.get('title')} ({item.get('country')}) @ {local_news_time.strftime('%H:%M')}"

    bot_state["news_status"] = {"is_blocked": blocked, "reason": block_reason if blocked else "Normal Trading Window", "next_news": next_news_str}
    return blocked

async def news_fetch_loop():
    while True:
        fetch_economic_calendar()
        await asyncio.sleep(3600)

def get_mt5_timeframe(tf_str):
    mapping = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1}
    return mapping.get(tf_str, mt5.TIMEFRAME_M15)

def check_market_filters(proposed_signal: str):
    tick = mt5.symbol_info_tick(SYMBOL)
    sym_info = mt5.symbol_info(SYMBOL)
    if tick and sym_info and sym_info.point > 0:
        spread_points = (tick.ask - tick.bid) / sym_info.point
        if spread_points > CONFIG.get("maxSpreadPoints", 50.0):
            return False, f"High Spread: {spread_points:.1f} pts (Max {CONFIG['maxSpreadPoints']})"

    if CONFIG.get("enableSessionFilter", False):
        current_hour = datetime.now().hour
        start_h = CONFIG.get("sessionStartHour", 13)
        end_h = CONFIG.get("sessionEndHour", 23)
        if not (start_h <= current_hour < end_h):
            return False, f"Outside Session Window ({start_h}:00 - {end_h}:00)"

    if CONFIG.get("enableMTFFilter", True) and proposed_signal in ["BUY", "SELL"]:
        h1_rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, 120)
        if h1_rates is not None and len(h1_rates) >= 100:
            df_h1 = pd.DataFrame(h1_rates)
            ema50_h1 = ta.trend.ema_indicator(df_h1['close'], window=CONFIG['emaFastLen']).iloc[-1]
            ema100_h1 = ta.trend.ema_indicator(df_h1['close'], window=CONFIG['emaSlowLen']).iloc[-1]
            if proposed_signal == "BUY" and ema50_h1 <= ema100_h1: return False, "H1 Trend is Bearish"
            elif proposed_signal == "SELL" and ema50_h1 >= ema100_h1: return False, "H1 Trend is Bullish"

    return True, "Passed All Filters"

def calculate_lot_size(symbol: str, sl_points: float, risk_percent: float):
    acc_info = mt5.account_info()
    if not acc_info: return 0.0
    risk_amount = acc_info.balance * (risk_percent / 100)
    sym_info = mt5.symbol_info(symbol)
    if not sym_info or sl_points <= 0: return 0.0
    tick_value = sym_info.trade_tick_value 
    tick_size = sym_info.trade_tick_size
    if tick_size == 0 or sym_info.point == 0: return 0.0
    point_value = tick_value / (tick_size / sym_info.point)
    if point_value <= 0: return 0.0
    raw_lot = risk_amount / (sl_points * point_value)
    lot = round(raw_lot / sym_info.volume_step) * sym_info.volume_step
    return max(sym_info.volume_min, min(lot, sym_info.volume_max))

def calculate_safe_dca_lot(symbol, group, curr_atr):
    acc_info = mt5.account_info()
    if not acc_info: return 0.0
    sym_info = mt5.symbol_info(symbol)
    if not sym_info: return 0.0
    tick_value = sym_info.trade_tick_value
    tick_size = sym_info.trade_tick_size
    if tick_size == 0 or sym_info.point == 0: return 0.0
    point_value = tick_value / (tick_size / sym_info.point)
    if point_value <= 0: return 0.0

    current_loss = sum(p.profit + p.swap for p in group)
    max_loss_amount = acc_info.balance * (CONFIG.get("maxBasketRisk", 5.0) / 100)
    remaining_budget = max_loss_amount - abs(current_loss)
    if remaining_budget <= 0: return 0.0 

    sl_points = (curr_atr * CONFIG["dcaStepATR"]) / sym_info.point
    safe_lot = remaining_budget / (sl_points * point_value) if sl_points > 0 else 0.0
    last_lot = sorted(group, key=lambda x: x.time)[-1].volume
    normal_dca_lot = last_lot * CONFIG["dcaMultiplier"]
    final_lot = min(normal_dca_lot, safe_lot)
    final_lot = round(final_lot / sym_info.volume_step) * sym_info.volume_step
    return max(sym_info.volume_min, min(final_lot, sym_info.volume_max))

def execute_trade(symbol, action, lot, price, sl, tp, comment):
    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lot,
        "type": action, "price": price, "sl": sl, "tp": tp, "deviation": 20,
        "magic": CONFIG["magicNumber"], "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
    }
    max_retries = 3
    error_msg = "Unknown Error"
    for attempt in range(max_retries):
        res = mt5.order_send(request)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            action_str = "BUY" if action == mt5.ORDER_TYPE_BUY else "SELL"
            log_trade(res.order, f"OPEN {action_str}", symbol, lot, price, comment)
            send_telegram(f"🤖 *AUTO-TRADE EXECUTED*\nออเดอร์: {comment}\nTicket: `{res.order}`\nPrice: {request['price']}\nLot: {lot}")
            return True
        else:
            error_msg = res.comment if res else 'Unknown Error'
            if attempt < max_retries - 1:
                time.sleep(1)
                tick = mt5.symbol_info_tick(symbol)
                if tick:
                    request["price"] = tick.ask if action == mt5.ORDER_TYPE_BUY else tick.bid
    send_telegram(f"❌ *AUTO-TRADE FAILED*\nพยายามยิงคำสั่ง {max_retries} ครั้งแต่ล้มเหลว\nเหตุผล: {error_msg}")
    return False

def move_sl_to_breakeven(ticket, open_price, tp, symbol, type):
    sym_info = mt5.symbol_info(symbol)
    point = sym_info.point
    extra_pts = CONFIG.get("beProfitPoints", 20.0) 
    
    be_price = open_price + (extra_pts * point) if type == mt5.ORDER_TYPE_BUY else open_price - (extra_pts * point)
    be_price = round(be_price, sym_info.digits) 
    
    req = {
        "action": mt5.TRADE_ACTION_SLTP, "symbol": symbol, "position": ticket,
        "sl": be_price, "tp": tp, "magic": CONFIG["magicNumber"]
    }
    if mt5.order_send(req).retcode == mt5.TRADE_RETCODE_DONE:
        send_telegram(f"🛡️ *BREAK-EVEN ACTIVATED*\nออเดอร์ `{ticket}` เลื่อน SL บังหน้าทุน + ล็อคกำไร {extra_pts} จุดเรียบร้อย!")

def move_trailing_stop(ticket, new_sl, tp, symbol):
    req = {
        "action": mt5.TRADE_ACTION_SLTP, "symbol": symbol, "position": ticket,
        "sl": new_sl, "tp": tp, "magic": CONFIG["magicNumber"]
    }
    res = mt5.order_send(req)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        send_telegram(f"📈 *TRAILING STOP MOVED*\nออเดอร์ `{ticket}` ล็อคกำไรเพิ่มที่ SL: {round(new_sl, 4)}")

def close_position(ticket, symbol, p_type, volume, profit=0.0, comment="System Close"):
    max_retries = 3
    for attempt in range(max_retries):
        tick = mt5.symbol_info_tick(symbol)
        if not tick: 
            time.sleep(1)
            continue
        action_type = mt5.ORDER_TYPE_SELL if p_type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if p_type == mt5.ORDER_TYPE_BUY else tick.ask
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": volume,
            "type": action_type, "position": ticket, "price": price, "deviation": 20,
            "magic": CONFIG["magicNumber"], "comment": "Close Order",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            action_str = "CLOSE SELL" if p_type == mt5.ORDER_TYPE_BUY else "CLOSE BUY"
            log_trade(ticket, action_str, symbol, volume, price, comment, profit)
            return True
        time.sleep(1)
    return False

def trade_manager(curr_atr):
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions: return
    bot_pos = [p for p in positions if p.magic == CONFIG["magicNumber"] or p.magic == 0]
    if not bot_pos: return

    acc_info = mt5.account_info()
    balance = acc_info.balance if acc_info else 0.0
    is_news_blocked = check_news_impact()
    sym_info = mt5.symbol_info(SYMBOL)

    for o_type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]:
        group = [p for p in bot_pos if p.type == o_type]
        if not group: continue
        
        total_profit = sum(p.profit + p.swap for p in group)
        hard_cut_amount = balance * (CONFIG.get("hardCutRisk", 6.0) / 100)
        dca_target_profit = CONFIG.get("dcaTargetProfit", 3.0)

        if len(group) > 1 and total_profit >= dca_target_profit:
            closed_count = 0
            for p in group:
                if close_position(p.ticket, p.symbol, p.type, p.volume, p.profit + p.swap, "Basket Profit Close"): closed_count += 1
            if closed_count > 0:
                side_name = "BUY" if o_type == mt5.ORDER_TYPE_BUY else "SELL"
                send_telegram(f"✨ *DCA RECOVERY SUCCESS*\nรวบปิดออเดอร์ฝั่ง {side_name} จำนวน {closed_count} ไม้\n💰 กำไรรวม: {round(total_profit, 2)}")
            continue 
            
        elif total_profit < 0.0 and abs(total_profit) >= hard_cut_amount and hard_cut_amount > 0:
            closed_count = 0
            for p in group:
                if close_position(p.ticket, p.symbol, p.type, p.volume, p.profit + p.swap, "Hard Cut Loss"): closed_count += 1
            if closed_count > 0:
                side_name = "BUY" if o_type == mt5.ORDER_TYPE_BUY else "SELL"
                send_telegram(f"☠️ *HARD CUT LOSS ACTIVATED*\nตะกร้าฝั่ง {side_name} ติดลบเกินขีดจำกัด\nระบบตัดขาดทุนจำนวน {closed_count} ไม้ เพื่อรักษาพอร์ต\n💸 ขาดทุนรวม: {round(total_profit, 2)}")
            continue

        curr_price = mt5.symbol_info_tick(SYMBOL).bid if o_type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(SYMBOL).ask
        
        if CONFIG.get("enableBE", True) and sym_info:
            for p in group:
                profit_dist = (curr_price - p.price_open) if o_type == mt5.ORDER_TYPE_BUY else (p.price_open - curr_price)
                if profit_dist >= (curr_atr * CONFIG["beTriggerATR"]):
                    point = sym_info.point
                    extra_pts = CONFIG.get("beProfitPoints", 20.0)
                    be_price = p.price_open + (extra_pts * point) if o_type == mt5.ORDER_TYPE_BUY else p.price_open - (extra_pts * point)
                    be_price = round(be_price, sym_info.digits)
                    
                    if o_type == mt5.ORDER_TYPE_BUY and p.sl < be_price:
                        move_sl_to_breakeven(p.ticket, p.price_open, p.tp, SYMBOL, o_type)
                    elif o_type == mt5.ORDER_TYPE_SELL and (p.sl > be_price or p.sl == 0):
                        move_sl_to_breakeven(p.ticket, p.price_open, p.tp, SYMBOL, o_type)

        if CONFIG.get("enableTrailing", True) and sym_info:
            digits = sym_info.digits
            for p in group:
                profit_dist = (curr_price - p.price_open) if o_type == mt5.ORDER_TYPE_BUY else (p.price_open - curr_price)
                if profit_dist >= (curr_atr * CONFIG.get("trailStartATR", 1.5)):
                    if o_type == mt5.ORDER_TYPE_BUY:
                        new_sl = round(curr_price - (curr_atr * CONFIG.get("trailDistanceATR", 1.0)), digits)
                        if new_sl > p.sl + (curr_atr * 0.1) and new_sl < curr_price:
                            move_trailing_stop(p.ticket, new_sl, p.tp, SYMBOL)
                    else:
                        new_sl = round(curr_price + (curr_atr * CONFIG.get("trailDistanceATR", 1.0)), digits)
                        if (p.sl == 0 or new_sl < p.sl - (curr_atr * 0.1)) and new_sl > curr_price:
                            move_trailing_stop(p.ticket, new_sl, p.tp, SYMBOL)

        if CONFIG["enableDCA"] and len(group) <= CONFIG["maxDCA"]:
            if is_news_blocked: continue
            last_p = sorted(group, key=lambda x: x.time)[-1]
            loss_dist = (last_p.price_open - curr_price) if o_type == mt5.ORDER_TYPE_BUY else (curr_price - last_p.price_open)
            if loss_dist >= (curr_atr * CONFIG["dcaStepATR"]):
                new_lot = calculate_safe_dca_lot(SYMBOL, group, curr_atr)
                if new_lot > 0: execute_trade(SYMBOL, o_type, new_lot, curr_price, 0.0, 0.0, f"DCA Step {len(group)}")
                else: send_telegram(f"⚠️ *DCA BLOCKED*\nระบบปฏิเสธการเปิดไม้แก้เกม เนื่องจากความเสี่ยงตะกร้าเกิน {CONFIG.get('maxBasketRisk', 5.0)}% แล้ว")

def analyze_data():
    if not mt5.initialize(): return None
    rates = mt5.copy_rates_from_pos(SYMBOL, get_mt5_timeframe(TIMEFRAME_STR), 0, 150)
    if rates is None or len(rates) == 0: return None

    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    
    df['emaFast'] = ta.trend.ema_indicator(df['close'], window=CONFIG['emaFastLen'])
    df['emaSlow'] = ta.trend.ema_indicator(df['close'], window=CONFIG['emaSlowLen'])
    df['emaEntry'] = ta.trend.ema_indicator(df['close'], window=CONFIG['emaEntryLen'])
    df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=CONFIG['adxLen']).adx()
    df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=CONFIG['atrLen']).average_true_range()

    df['candleRange'] = df['high'] - df['low']
    df['upperWick'] = df['high'] - df[['open', 'close']].max(axis=1)
    df['lowerWick'] = df[['open', 'close']].min(axis=1) - df['low']
    df['upperWickPct'] = (df['upperWick'] / df['candleRange']) * 100
    df['lowerWickPct'] = (df['lowerWick'] / df['candleRange']) * 100

    curr = df.iloc[-2]
    prev = df.iloc[-3]

    # 🔒 บังคับผลลัพธ์ให้เป็น True/False แบบ 100% (Bulletproof)
    isUptrend = bool(curr['emaFast'] > curr['emaSlow'])
    isDowntrend = bool(curr['emaFast'] < curr['emaSlow'])
    isTrending = bool(curr['adx'] > CONFIG.get('adxThresh', 20.0))
    
    buyTrigger = bool((curr['close'] > curr['emaEntry']) and (prev['close'] <= prev['emaEntry']))
    sellTrigger = bool((curr['close'] < curr['emaEntry']) and (prev['close'] >= prev['emaEntry']))

    buySignal = bool(isUptrend and isTrending and buyTrigger and (curr['lowerWickPct'] >= CONFIG.get('minWickPct', 5.0)))
    sellSignal = bool(isDowntrend and isTrending and sellTrigger and (curr['upperWickPct'] >= CONFIG.get('minWickPct', 5.0)))

    raw_signal = "BUY" if buySignal else "SELL" if sellSignal else "WAIT"
    is_filter_passed, filter_reason = check_market_filters(raw_signal)
    bot_state["market_filter_status"] = {"is_filtered": not is_filter_passed, "reason": filter_reason}

    final_signal = raw_signal if is_filter_passed else "WAIT"
    
    tp_price, sl_price, lot_size = 0.0, 0.0, 0.0
    if final_signal != "WAIT":
        sym_info = mt5.symbol_info(SYMBOL)
        if sym_info:
            sl_dist = curr['atr'] * CONFIG['atrMultiplier']
            sl_points = sl_dist / sym_info.point if sym_info.point > 0 else 0
            if final_signal == "BUY":
                sl_price, tp_price = curr['close'] - sl_dist, curr['close'] + (sl_dist * CONFIG['rrRatio'])
            elif final_signal == "SELL":
                sl_price, tp_price = curr['close'] + sl_dist, curr['close'] - (sl_dist * CONFIG['rrRatio'])
            lot_size = calculate_lot_size(SYMBOL, sl_points, CONFIG['riskPercent'])

    bot_state.update({
        "latest_price": df.iloc[-1]['close'], "signal": final_signal, 
        "last_update": df.iloc[-1]['time'].strftime("%Y-%m-%d %H:%M:%S"),
        "tp": tp_price, "sl": sl_price, "lot": lot_size,
        "indicators": {
            "EMA 50": round(curr['emaFast'], 2), "EMA 100": round(curr['emaSlow'], 2),
            "EMA 8": round(curr['emaEntry'], 2), "ADX": round(curr['adx'], 2), "ATR": round(curr['atr'], 4),
            "Uptrend (M15)": "Yes" if isUptrend else "No", "Trending": "Yes" if isTrending else "No",
            "Market Filter": "PASS" if is_filter_passed else "BLOCKED"
        }
    })
    
    if final_signal != "WAIT":
        bot_state["indicators"]["🎯 Target (TP)"] = round(tp_price, 4)
        bot_state["indicators"]["🛑 Stop Loss (SL)"] = round(sl_price, 4)
        bot_state["indicators"]["📦 Rec. Lot Size"] = lot_size

    if bot_state["auto_trade"]:
        trade_manager(curr['atr'])

    return final_signal

async def drawdown_monitor():
    while True:
        dd_limit = CONFIG.get("maxDailyDrawdownRisk", 0.0)
        if bot_state["is_running"] and dd_limit > 0:
            acc_info = mt5.account_info()
            if acc_info:
                midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                deals = mt5.history_deals_get(midnight, datetime.now() + timedelta(days=1))
                daily_pl = sum(d.profit for d in deals if d.type in (0, 1)) if deals else 0.0
                
                positions = mt5.positions_get(symbol=SYMBOL)
                floating_pl = sum(p.profit + p.swap for p in positions) if positions else 0.0
                
                total_daily_loss = daily_pl + floating_pl
                max_loss_limit = acc_info.balance * (dd_limit / 100)
                
                if total_daily_loss < 0 and abs(total_daily_loss) >= max_loss_limit:
                    bot_state["is_running"] = False
                    closed_count = 0
                    if positions:
                        for p in positions:
                            if close_position(p.ticket, p.symbol, p.type, p.volume, p.profit + p.swap, "Max Daily Drawdown Close"): 
                                closed_count += 1
                    msg = (f"🛑 *MAX DAILY DRAWDOWN REACHED*\nพอร์ตขาดทุนรายวันเกินขีดจำกัด {dd_limit}%\n"
                           f"📉 ขาดทุนรวมวันนี้: {round(total_daily_loss, 2)}\n"
                           f"🛡️ ระบบปิดออเดอร์ {closed_count} ไม้ และ **STOP BOT** อัตโนมัติ!")
                    send_telegram(msg)
        await asyncio.sleep(5)

async def bot_loop():
    while bot_state["is_running"]:
        signal = analyze_data()
        is_news_blocked = check_news_impact()
        
        if signal in ["BUY", "SELL"]:
            if is_news_blocked:
                send_telegram(f"📰 *SIGNAL PAUSED (HIGH-IMPACT NEWS)*\nมีสัญญาณ {signal} แต่ระบบงดเข้าออเดอร์เนื่องจากติดข่าว:\n_{bot_state['news_status']['reason']}_")
                await asyncio.sleep(60)
                continue
                
            msg = (f"🚨 *{SYMBOL} SIGNAL: {signal}* 🚨\n\n"
                   f"💰 *Entry Price:* {bot_state['latest_price']}\n"
                   f"📦 *Lot Size:* {bot_state['lot']} (Risk {CONFIG['riskPercent']}%)\n"
                   f"🎯 *Take Profit:* {round(bot_state['tp'], 4)}\n"
                   f"🛑 *Stop Loss:* {round(bot_state['sl'], 4)}\n\n"
                   f"📊 ATR: {bot_state['indicators']['ATR']} | ADX: {bot_state['indicators']['ADX']}\n"
                   f"🛡️ Filter Status: {bot_state['market_filter_status']['reason']}")
            send_telegram(msg)
            
            if bot_state["auto_trade"]:
                positions = mt5.positions_get(symbol=SYMBOL)
                bot_positions = [p for p in positions if p.magic == CONFIG["magicNumber"]] if positions else []
                if len(bot_positions) == 0:
                    action = mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL
                    curr_price = mt5.symbol_info_tick(SYMBOL).ask if signal == "BUY" else mt5.symbol_info_tick(SYMBOL).bid
                    execute_trade(SYMBOL, action, bot_state["lot"], curr_price, bot_state["sl"], bot_state["tp"], "Master7 First Entry")
                else:
                    send_telegram("⚠️ *SIGNAL IGNORED*\nบอทยังมีออเดอร์ค้างอยู่ ข้ามการเปิดไม้ใหม่")
            await asyncio.sleep(60)
        else:
            await asyncio.sleep(2)

async def monitor_orders():
    global known_tickets
    while True:
        positions = mt5.positions_get()
        if positions is None:
            await asyncio.sleep(3)
            continue
            
        current_tickets = {pos.ticket for pos in positions}
        new_orders = current_tickets - known_tickets
        closed_orders = known_tickets - current_tickets
        
        if new_orders or closed_orders:
            combined_message = "🔔 *MT5 Order Update*\n\n"
            if new_orders:
                combined_message += "🟢 *NEW ORDERS OPENED:*\n"
                for ticket in new_orders:
                    pos = [p for p in positions if p.ticket == ticket][0]
                    action = "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL"
                    combined_message += f"- Ticket: `{ticket}` | {pos.symbol} | {action} | Vol: {pos.volume} | Price: {pos.price_open}\n"
                combined_message += "\n"
                
            if closed_orders:
                combined_message += "🔴 *ORDERS CLOSED:*\n"
                for ticket in closed_orders:
                    combined_message += f"- Ticket: `{ticket}` has been closed.\n"
            
            send_telegram(combined_message)
        known_tickets = current_tickets
        await asyncio.sleep(3)

async def hourly_status_report():
    while True:
        await asyncio.sleep(3600)
        account = mt5.account_info()
        terminal = mt5.terminal_info()
        if account and terminal:
            midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            deals = mt5.history_deals_get(midnight, datetime.now() + timedelta(days=1))
            daily_pl = sum(d.profit for d in deals if d.type in (0, 1)) if deals else 0.0
            
            positions = mt5.positions_get(symbol=SYMBOL)
            open_orders = len(positions) if positions else 0
            floating_pl = sum(p.profit + p.swap for p in positions) if positions else 0.0

            msg = (f"🕒 *HOURLY SUMMARY REPORT*\n"
                   f"🗓 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                   f"💰 *Balance:* {account.balance:,.2f}\n"
                   f"📈 *Equity:* {account.equity:,.2f}\n"
                   f"🛡️ *Margin Level:* {account.margin_level:,.2f}%\n\n"
                   f"🎯 *Today's P/L:* {daily_pl:,.2f}\n"
                   f"🌊 *Floating P/L:* {floating_pl:,.2f}\n"
                   f"📦 *Active Orders:* {open_orders} ไม้\n")
            send_telegram(msg)

def get_dashboard_data():
    mt5.initialize()
    check_news_impact()
    sys_data = {"cpu": psutil.cpu_percent(interval=None), "ram": psutil.virtual_memory().percent, "disk": psutil.disk_usage('/').percent}
    acc_info = mt5.account_info()
    acc_data = {"balance": 0.0, "equity": 0.0, "floating_pl": 0.0, "leverage": 0, "used_margin": 0.0, "free_margin": 0.0, "margin_level": 0.0, "daily_pl": 0.0}
    if acc_info:
        acc_data.update({"balance": acc_info.balance, "equity": acc_info.equity, "floating_pl": acc_info.profit, "leverage": acc_info.leverage, "used_margin": acc_info.margin, "free_margin": acc_info.margin_free, "margin_level": acc_info.margin_level})
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        deals = mt5.history_deals_get(midnight, datetime.now() + timedelta(days=1))
        if deals: acc_data["daily_pl"] = sum(d.profit for d in deals if d.type in (0, 1))

    market_data = {"symbol": SYMBOL, "bid": 0.0, "ask": 0.0, "spread": 0.0}
    tick = mt5.symbol_info_tick(SYMBOL)
    sym_info = mt5.symbol_info(SYMBOL)
    if tick and sym_info and sym_info.point > 0:
        spread_calc = round((tick.ask - tick.bid) / sym_info.point, 1)
        market_data.update({"bid": tick.bid, "ask": tick.ask, "spread": spread_calc})

    pos_data = []
    positions = mt5.positions_get(symbol=SYMBOL)
    if positions:
        for p in positions:
            point = sym_info.point if sym_info else 0.0001
            diff = p.price_current - p.price_open if p.type == 0 else p.price_open - p.price_current
            pos_data.append({"ticket": p.ticket, "symbol": p.symbol, "type": "BUY" if p.type == 0 else "SELL", "volume": p.volume, "pips": round(diff / point if point > 0 else 0, 1), "profit": round(p.profit, 2), "bot": "Yes" if p.magic == CONFIG["magicNumber"] else "No"})

    return {"sys": sys_data, "acc": acc_data, "bot": bot_state, "market": market_data, "positions": pos_data}

@app.on_event("startup")
async def startup_event():
    if not mt5.initialize():
        print("MT5 initialization failed")
    else:
        positions = mt5.positions_get()
        if positions:
            global known_tickets
            known_tickets = {pos.ticket for pos in positions}

    fetch_economic_calendar() 
    asyncio.create_task(news_fetch_loop()) 
    asyncio.create_task(monitor_orders())
    asyncio.create_task(hourly_status_report())
    asyncio.create_task(drawdown_monitor())
    asyncio.create_task(mt5_connection_monitor())
    
    send_telegram("🚀 *MASTER 7 Bot Started & Ready*")

@app.on_event("shutdown")
async def shutdown_event():
    mt5.shutdown()

@app.get("/")
def read_root(request: Request): return templates.TemplateResponse(request=request, name="index.html", context={"state": bot_state})

@app.get("/api/config")
def get_config(): return CONFIG

@app.post("/api/config")
def update_config(new_config: ConfigModel):
    global CONFIG
    CONFIG.update(new_config.dict())
    save_config()
    send_telegram("⚙️ *SYSTEM CONFIG UPDATED*\nผู้ใช้งานได้เปลี่ยนการตั้งค่าบอทผ่าน Web Dashboard")
    return {"status": "success", "message": "Configuration updated successfully."}

@app.get("/api/start")
async def start_bot(background_tasks: BackgroundTasks):
    if not bot_state["is_running"]: bot_state["is_running"] = True; background_tasks.add_task(bot_loop)
    return {"status": "started"}

@app.get("/api/stop")
def stop_bot(): bot_state["is_running"] = False; return {"status": "stopped"}

@app.get("/api/toggle_auto")
def toggle_auto(): bot_state["auto_trade"] = not bot_state["auto_trade"]; return {"auto_trade": bot_state["auto_trade"]}

@app.get("/api/close_all")
def close_all_orders():
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions: return {"status": "no_orders"}
    closed_count = 0
    for p in positions:
        if close_position(p.ticket, p.symbol, p.type, p.volume, p.profit + p.swap, "Manual Web Close"):
            closed_count += 1
    if closed_count > 0:
        send_telegram(f"🧹 *MANUAL CLOSE ALL*\nปิดออเดอร์ทั้งหมดจำนวน {closed_count} ไม้ ผ่านหน้าเว็บสำเร็จ!")
    return {"status": "success", "closed": closed_count}

@app.get("/api/status")
def get_status():
    analyze_data()
    return get_dashboard_data()

# ==========================================
# 📈 API สำหรับดึงประวัติการเทรดมาแสดงผล
# ==========================================
@app.get("/api/logs")
def get_trade_logs():
    if not os.path.exists(TRADE_LOG_FILE):
        return {"status": "success", "logs": []}
    try:
        df = pd.read_csv(TRADE_LOG_FILE)
        # ดึง 50 รายการล่าสุด และกลับด้านให้รายการใหม่สุดอยู่บน
        last_logs = df.tail(50).fillna("").to_dict(orient="records")
        last_logs.reverse()
        return {"status": "success", "logs": last_logs}
    except Exception as e:
        return {"status": "error", "message": str(e)}