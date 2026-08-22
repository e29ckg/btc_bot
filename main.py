import os
import time
import asyncio
import requests
import MetaTrader5 as mt5
import pandas as pd
import ta
import psutil
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.templating import Jinja2Templates

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SYMBOL = os.getenv("MT5_SYMBOL", "BTCUSD")
TIMEFRAME_STR = os.getenv("TIMEFRAME", "M15")

CONFIG = {
    "emaFastLen": 50, "emaSlowLen": 100, "emaEntryLen": 8,
    "adxLen": 14, "adxThresh": 20.0, "minWickPct": 5.0
}

app = FastAPI()
templates = Jinja2Templates(directory="templates")

bot_state = {
    "is_running": False,
    "latest_price": 0,
    "signal": "WAIT",
    "last_update": "",
    "indicators": {}
}

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"})

def get_mt5_timeframe(tf_str):
    mapping = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1}
    return mapping.get(tf_str, mt5.TIMEFRAME_M15)

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

    df['candleRange'] = df['high'] - df['low']
    df['upperWick'] = df['high'] - df[['open', 'close']].max(axis=1)
    df['lowerWick'] = df[['open', 'close']].min(axis=1) - df['low']
    df['upperWickPct'] = (df['upperWick'] / df['candleRange']) * 100
    df['lowerWickPct'] = (df['lowerWick'] / df['candleRange']) * 100

    curr = df.iloc[-2]
    prev = df.iloc[-3]

    isUptrend = curr['emaFast'] > curr['emaSlow']
    isDowntrend = curr['emaFast'] < curr['emaSlow']
    isTrending = curr['adx'] > CONFIG['adxThresh']

    buyTrigger = (curr['close'] > curr['emaEntry']) and (prev['close'] <= prev['emaEntry'])
    sellTrigger = (curr['close'] < curr['emaEntry']) and (prev['close'] >= prev['emaEntry'])

    buySignal = isUptrend and isTrending and buyTrigger and (curr['lowerWickPct'] >= CONFIG['minWickPct'])
    sellSignal = isDowntrend and isTrending and sellTrigger and (curr['upperWickPct'] >= CONFIG['minWickPct'])

    signal = "BUY" if buySignal else "SELL" if sellSignal else "WAIT"

    bot_state["latest_price"] = df.iloc[-1]['close']
    bot_state["signal"] = signal
    bot_state["last_update"] = df.iloc[-1]['time'].strftime("%Y-%m-%d %H:%M:%S")
    bot_state["indicators"] = {
        "EMA 50": round(curr['emaFast'], 2),
        "EMA 100": round(curr['emaSlow'], 2),
        "EMA 8": round(curr['emaEntry'], 2),
        "ADX": round(curr['adx'], 2),
        "Uptrend": "Yes" if isUptrend else "No",
        "Trending (ADX>20)": "Yes" if isTrending else "No"
    }
    
    return signal

async def bot_loop():
    while bot_state["is_running"]:
        signal = analyze_data()
        if signal in ["BUY", "SELL"]:
            send_telegram(f"🚨 *{SYMBOL} SIGNAL: {signal}* 🚨\nPrice: {bot_state['latest_price']}\nADX: {bot_state['indicators']['ADX']}")
            await asyncio.sleep(60)
        else:
            await asyncio.sleep(10)

def get_dashboard_data():
    mt5.initialize()
    
    # 1. Server Status
    sys_data = {
        "cpu": psutil.cpu_percent(interval=None),
        "ram": psutil.virtual_memory().percent,
        "disk": psutil.disk_usage('/').percent
    }
    
    # 2. Account Data
    acc_info = mt5.account_info()
    acc_data = {
        "balance": 0.0, "equity": 0.0, "floating_pl": 0.0,
        "leverage": 0, "used_margin": 0.0, "free_margin": 0.0, "margin_level": 0.0, "daily_pl": 0.0
    }
    
    if acc_info:
        acc_data.update({
            "balance": acc_info.balance, "equity": acc_info.equity, "floating_pl": acc_info.profit,
            "leverage": acc_info.leverage, "used_margin": acc_info.margin, "free_margin": acc_info.margin_free,
            "margin_level": acc_info.margin_level
        })
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        deals = mt5.history_deals_get(midnight, datetime.now() + timedelta(days=1))
        if deals: acc_data["daily_pl"] = sum(d.profit for d in deals if d.type in (0, 1))

    # 3. Market Tracker
    market_data = {"symbol": SYMBOL, "bid": 0.0, "ask": 0.0, "structure": {}, "trends": {}}
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick:
        market_data["bid"] = tick.bid
        market_data["ask"] = tick.ask
        
    # Structure (H1 - last 50 candles)
    h1_rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, 50)
    if h1_rates is not None and len(h1_rates) > 0:
        df_h1 = pd.DataFrame(h1_rates)
        df_h1['time'] = pd.to_datetime(df_h1['time'], unit='s')
        hi_idx = df_h1['high'].idxmax()
        lo_idx = df_h1['low'].idxmin()
        market_data["structure"] = {
            "high": df_h1.loc[hi_idx, 'high'], "high_time": df_h1.loc[hi_idx, 'time'].strftime("%H:%M %d/%m"),
            "low": df_h1.loc[lo_idx, 'low'], "low_time": df_h1.loc[lo_idx, 'time'].strftime("%H:%M %d/%m")
        }

    # Trend Matrix (SMA 14)
    tfs = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1}
    for tf_name, tf_val in tfs.items():
        r = mt5.copy_rates_from_pos(SYMBOL, tf_val, 0, 15)
        if r is not None and len(r) >= 14:
            c = pd.Series([x['close'] for x in r])
            sma = ta.trend.sma_indicator(c, window=14).iloc[-1]
            market_data["trends"][tf_name] = "UP" if c.iloc[-1] > sma else "DOWN"

    # 4. Active Positions
    pos_data = []
    positions = mt5.positions_get()
    if positions:
        for p in positions:
            sym_info = mt5.symbol_info(p.symbol)
            point = sym_info.point if sym_info else 0.0001
            diff = p.price_current - p.price_open if p.type == 0 else p.price_open - p.price_current
            pips = diff / point if point > 0 else 0
            pos_data.append({
                "ticket": p.ticket, "symbol": p.symbol, "type": "BUY" if p.type == 0 else "SELL",
                "volume": p.volume, "pips": round(pips, 1), "profit": round(p.profit, 2)
            })

    return {"sys": sys_data, "acc": acc_data, "bot": bot_state, "market": market_data, "positions": pos_data}

@app.get("/")
def read_root(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={"state": bot_state})

@app.get("/api/start")
async def start_bot(background_tasks: BackgroundTasks):
    if not bot_state["is_running"]:
        bot_state["is_running"] = True
        background_tasks.add_task(bot_loop)
    return {"status": "started"}

@app.get("/api/stop")
def stop_bot():
    bot_state["is_running"] = False
    return {"status": "stopped"}

@app.get("/api/status")
def get_status():
    analyze_data()
    return get_dashboard_data()