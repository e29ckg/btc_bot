import os
import time
import json
import csv
import asyncio
import base64
import math
import secrets
import requests
import MetaTrader5 as mt5
import pandas as pd
import ta
import psutil
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, model_validator

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SYMBOL = os.getenv("MT5_SYMBOL", "XAUUSD")
TIMEFRAME_STR = os.getenv("TIMEFRAME", "M1")
EXECUTION_MODE = os.getenv("EXECUTION_MODE", "paper").lower()
DASHBOARD_USERNAME = os.getenv("DASHBOARD_USERNAME", "admin")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")

CONFIG_FILE = "config.json"
TRADE_LOG_FILE = "trade_log.csv"
RUNTIME_STATE_FILE = "runtime_state.json"
PAPER_LOG_FILE = "paper_trade_log.csv"

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
    "maxWeeklyDrawdownRisk": 5.0,
    "maxConsecutiveLosses": 5,
    "enableNewsFilter": True,
    "newsBeforeMin": 30,
    "newsAfterMin": 30,
    "newsCurrencies": ["USD"],
    "enableMTFFilter": True,
    "maxSpreadPoints": 50.0,
    "maxSpreadRiskRatio": 0.1,
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
    emaFastLen: int = Field(ge=2, le=500)
    emaSlowLen: int = Field(ge=3, le=1000)
    emaEntryLen: int = Field(ge=2, le=200)
    adxLen: int = Field(ge=2, le=200)
    adxThresh: float = Field(ge=1, le=100)
    minWickPct: float = Field(ge=0, le=100)
    atrLen: int = Field(ge=2, le=200)
    atrMultiplier: float = Field(gt=0, le=20)
    rrRatio: float = Field(gt=0, le=20)
    riskPercent: float = Field(gt=0, le=2)
    magicNumber: int = Field(gt=0)
    enableBE: bool
    beTriggerATR: float = Field(gt=0, le=20)
    beProfitPoints: float = Field(ge=0, le=10000)
    enableTrailing: bool
    trailStartATR: float = Field(gt=0, le=20)
    trailDistanceATR: float = Field(gt=0, le=20)
    enableDCA: bool
    dcaStepATR: float = Field(ge=1, le=20)
    dcaMultiplier: float = Field(ge=1, le=2)
    maxDCA: int = Field(ge=0, le=5)
    dcaTargetProfit: float = Field(ge=0)
    maxBasketRisk: float = Field(gt=0, le=5)
    hardCutRisk: float = Field(gt=0, le=6)
    maxDailyDrawdownRisk: float = Field(gt=0, le=10)
    maxWeeklyDrawdownRisk: float = Field(default=5.0, gt=0, le=20)
    maxConsecutiveLosses: int = Field(default=5, ge=1, le=20)
    enableNewsFilter: bool
    newsBeforeMin: int = Field(ge=0, le=240)
    newsAfterMin: int = Field(ge=0, le=240)
    enableMTFFilter: bool
    maxSpreadPoints: float = Field(gt=0, le=500)
    maxSpreadRiskRatio: float = Field(default=0.1, gt=0, le=0.5)
    enableSessionFilter: bool
    sessionStartHour: int = Field(ge=0, le=23)
    sessionEndHour: int = Field(ge=0, le=23)
    strategyMode: str = Field(default="donchian", pattern="^(legacy|donchian)$")
    donchianEntryLen: int = Field(default=20, ge=5, le=200)
    donchianExitLen: int = Field(default=10, ge=2, le=100)
    trendEmaLen: int = Field(default=200, ge=20, le=500)
    atrRegimeLookback: int = Field(default=100, ge=20, le=500)
    atrRegimeMin: float = Field(default=0.75, gt=0, le=2)
    atrRegimeMax: float = Field(default=2.0, gt=0, le=5)

    @model_validator(mode="after")
    def validate_risk_relationships(self):
        if self.emaFastLen >= self.emaSlowLen:
            raise ValueError("emaFastLen must be less than emaSlowLen")
        if self.maxBasketRisk > self.hardCutRisk:
            raise ValueError("maxBasketRisk must not exceed hardCutRisk")
        if self.enableSessionFilter and self.sessionStartHour == self.sessionEndHour:
            raise ValueError("session window cannot be zero hours")
        if self.donchianExitLen >= self.donchianEntryLen:
            raise ValueError("donchianExitLen must be less than donchianEntryLen")
        if self.atrRegimeMin >= self.atrRegimeMax:
            raise ValueError("atrRegimeMin must be less than atrRegimeMax")
        return self

@asynccontextmanager
async def lifespan(app: FastAPI):
    global known_tickets, bot_task

    if not mt5.initialize():
        print(f"MT5 initialization failed: {mt5.last_error()}")
    else:
        positions = mt5.positions_get()
        if positions:
            known_tickets = {pos.ticket for pos in positions}

    await asyncio.to_thread(fetch_economic_calendar)
    service_tasks = [
        asyncio.create_task(news_fetch_loop()),
        asyncio.create_task(monitor_orders()),
        asyncio.create_task(hourly_status_report()),
        asyncio.create_task(drawdown_monitor()),
        asyncio.create_task(mt5_connection_monitor()),
    ]
    await asyncio.to_thread(send_telegram, "🚀 *MASTER 7 Bot Started & Ready*")

    try:
        yield
    finally:
        bot_state["is_running"] = False
        bot_state["auto_trade"] = False
        if bot_task and not bot_task.done():
            bot_task.cancel()
            service_tasks.append(bot_task)
        for task in service_tasks:
            task.cancel()
        await asyncio.gather(*service_tasks, return_exceptions=True)
        bot_task = None
        mt5.shutdown()

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

MUTATING_PATHS = {"/api/config", "/api/start", "/api/stop", "/api/toggle_auto", "/api/close_all"}

@app.middleware("http")
async def dashboard_security(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)
    if not DASHBOARD_PASSWORD:
        return JSONResponse(
            status_code=503,
            content={"detail": "Set DASHBOARD_PASSWORD in .env before exposing the dashboard."},
        )
    auth = request.headers.get("Authorization", "")
    try:
        scheme, encoded = auth.split(" ", 1)
        username, password = base64.b64decode(encoded).decode("utf-8").split(":", 1)
    except (ValueError, UnicodeDecodeError):
        scheme, username, password = "", "", ""
    valid = (
        scheme.lower() == "basic"
        and secrets.compare_digest(username, DASHBOARD_USERNAME)
        and secrets.compare_digest(password, DASHBOARD_PASSWORD)
    )
    if not valid:
        return PlainTextResponse(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="MASTER 7 Bot"'},
        )
    if request.method == "POST" and request.url.path in MUTATING_PATHS:
        if request.headers.get("X-Bot-Action") != "confirm":
            return JSONResponse(status_code=405, content={"detail": "Confirmed POST request required."})
    return await call_next(request)

bot_state = {
    "is_running": False, "auto_trade": False, "latest_price": 0,
    "signal": "WAIT", "last_update": "", "indicators": {},
    "tp": 0.0, "sl": 0.0, "sl_distance": 0.0, "lot": 0.0,
    "news_status": {"is_blocked": False, "reason": "No major news near", "next_news": "-"},
    "market_filter_status": {"is_filtered": False, "reason": "All conditions clear"},
    "daily_lockout_date": None,
    "weekly_lockout_week": None,
    "signal_candle": None,
}

paper_state = {
    "position": None,
    "realized_pl": 0.0,
    "floating_pl": 0.0,
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "consecutive_losses": 0,
    "week_start_balance": None,
    "week_key": None,
}

known_tickets = set()
cached_news = []
news_last_updated = None
bot_task = None
last_signal_candle = None
bot_started_at = None

def load_runtime_state():
    global last_signal_candle
    try:
        with open(RUNTIME_STATE_FILE, "r", encoding="utf-8") as state_file:
            saved = json.load(state_file)
        bot_state["daily_lockout_date"] = saved.get("daily_lockout_date")
        bot_state["weekly_lockout_week"] = saved.get("weekly_lockout_week")
        last_signal_candle = saved.get("last_signal_candle")
        saved_paper = saved.get("paper_state")
        if isinstance(saved_paper, dict):
            paper_state.update(saved_paper)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        bot_state["daily_lockout_date"] = None
        bot_state["weekly_lockout_week"] = None

def save_runtime_state():
    try:
        with open(RUNTIME_STATE_FILE, "w", encoding="utf-8") as state_file:
            json.dump({
                "daily_lockout_date": bot_state.get("daily_lockout_date"),
                "weekly_lockout_week": bot_state.get("weekly_lockout_week"),
                "last_signal_candle": last_signal_candle,
                "paper_state": paper_state,
            }, state_file, indent=2)
    except OSError as exc:
        print(f"Runtime state save failed: {exc}")

load_runtime_state()

def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"Telegram notification failed: {type(exc).__name__}")
        return False

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
                await asyncio.to_thread(send_telegram, "⚠️ *MT5 DISCONNECTED*\nขาดการเชื่อมต่อกับเซิร์ฟเวอร์โบรกเกอร์! ระบบกำลังพยายามเชื่อมต่อใหม่...")
                is_connected = False
            if mt5.initialize():
                terminal_retry = mt5.terminal_info()
                if terminal_retry and terminal_retry.connected:
                    await asyncio.to_thread(send_telegram, "✅ *MT5 RECONNECTED*\nระบบสามารถเชื่อมต่อกับเซิร์ฟเวอร์ได้สำเร็จและพร้อมทำงานต่อแล้ว!")
                    is_connected = True
        else:
            if not is_connected:
                await asyncio.to_thread(send_telegram, "✅ *MT5 RECONNECTED*\nการเชื่อมต่อกลับมาเป็นปกติแล้ว!")
                is_connected = True
        await asyncio.sleep(10)

def fetch_economic_calendar():
    global cached_news, news_last_updated
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            cached_news = res.json()
            news_last_updated = datetime.now(timezone.utc)
    except Exception as e: print(f"Error fetching economic calendar: {e}")

def check_news_impact():
    if not CONFIG.get("enableNewsFilter", True):
        bot_state["news_status"] = {"is_blocked": False, "reason": "News filter disabled", "next_news": "-"}
        return False
    cache_stale = not news_last_updated or datetime.now(timezone.utc) - news_last_updated > timedelta(hours=2)
    if not cached_news or cache_stale:
        bot_state["news_status"] = {"is_blocked": True, "reason": "News calendar unavailable or stale", "next_news": "-"}
        return True
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
        await asyncio.to_thread(fetch_economic_calendar)
        await asyncio.sleep(3600)

def get_mt5_timeframe(tf_str):
    mapping = {"M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4, "D1": mt5.TIMEFRAME_D1}
    return mapping.get(tf_str, mt5.TIMEFRAME_M15)

def timeframe_seconds(tf_str):
    return {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}.get(tf_str, 900)

def required_rate_count():
    """Return enough closed candles for every configured indicator."""
    longest_window = max(
        CONFIG["emaFastLen"],
        CONFIG["emaSlowLen"],
        CONFIG["emaEntryLen"],
        CONFIG["adxLen"] * 2,
        CONFIG["atrLen"],
        CONFIG.get("donchianEntryLen", 20),
        CONFIG.get("atrRegimeLookback", 100),
    )
    return longest_window + 10

def get_order_filling_mode(sym_info):
    """Translate SYMBOL_FILLING_MODE flags to an ORDER_FILLING_* value."""
    filling_flags = getattr(sym_info, "filling_mode", 0)
    # SYMBOL_FILLING_FOK and SYMBOL_FILLING_IOC are bit flags (1 and 2),
    # while ORDER_FILLING_FOK and ORDER_FILLING_IOC are enum values (0 and 1).
    if filling_flags & 2:
        return mt5.ORDER_FILLING_IOC
    if filling_flags & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN

def check_market_filters(proposed_signal: str):
    tick = mt5.symbol_info_tick(SYMBOL)
    sym_info = mt5.symbol_info(SYMBOL)
    if not tick or not sym_info or sym_info.point <= 0:
        return False, "Market data unavailable"
    spread_points = (tick.ask - tick.bid) / sym_info.point
    if spread_points > CONFIG.get("maxSpreadPoints", 50.0):
        return False, f"High Spread: {spread_points:.1f} pts (Max {CONFIG['maxSpreadPoints']})"

    if CONFIG.get("enableSessionFilter", False):
        current_hour = datetime.now().hour
        start_h = CONFIG.get("sessionStartHour", 13)
        end_h = CONFIG.get("sessionEndHour", 23)
        in_session = start_h <= current_hour < end_h if start_h < end_h else current_hour >= start_h or current_hour < end_h
        if not in_session:
            return False, f"Outside Session Window ({start_h}:00 - {end_h}:00)"

    if CONFIG.get("enableMTFFilter", True) and proposed_signal in ["BUY", "SELL"]:
        h1_window = CONFIG.get("trendEmaLen", 200) if CONFIG.get("strategyMode") == "donchian" else max(CONFIG['emaFastLen'], CONFIG['emaSlowLen'])
        h1_count = h1_window + 10
        h1_rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_H1, 0, h1_count)
        if h1_rates is None or len(h1_rates) < h1_count:
            return False, "H1 market data unavailable"
        df_h1 = pd.DataFrame(h1_rates)
        if CONFIG.get("strategyMode") == "donchian":
            h1_ema = ta.trend.ema_indicator(df_h1['close'], window=h1_window).iloc[-2]
            h1_close = df_h1['close'].iloc[-2]
            if proposed_signal == "BUY" and h1_close <= h1_ema: return False, "H1 price is below EMA trend filter"
            if proposed_signal == "SELL" and h1_close >= h1_ema: return False, "H1 price is above EMA trend filter"
        else:
            ema50_h1 = ta.trend.ema_indicator(df_h1['close'], window=CONFIG['emaFastLen']).iloc[-2]
            ema100_h1 = ta.trend.ema_indicator(df_h1['close'], window=CONFIG['emaSlowLen']).iloc[-2]
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
    if sym_info.volume_step <= 0: return 0.0
    lot = math.floor(raw_lot / sym_info.volume_step) * sym_info.volume_step
    if lot < sym_info.volume_min: return 0.0
    return round(min(lot, sym_info.volume_max), 8)

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
    if sym_info.volume_step <= 0: return 0.0
    final_lot = math.floor(final_lot / sym_info.volume_step) * sym_info.volume_step
    if final_lot < sym_info.volume_min: return 0.0
    return round(min(final_lot, sym_info.volume_max), 8)

def calculate_basket_emergency_sl(symbol, group, new_lot, current_price, order_type):
    acc_info = mt5.account_info()
    sym_info = mt5.symbol_info(symbol)
    if not acc_info or not sym_info or sym_info.point <= 0 or sym_info.trade_tick_size <= 0:
        return 0.0
    point_value = sym_info.trade_tick_value / (sym_info.trade_tick_size / sym_info.point)
    current_loss = max(0.0, -sum(p.profit + p.swap for p in group))
    max_loss = acc_info.balance * (CONFIG["maxBasketRisk"] / 100)
    remaining_loss = max_loss - current_loss
    total_volume = sum(p.volume for p in group) + new_lot
    if remaining_loss <= 0 or total_volume <= 0 or point_value <= 0:
        return 0.0
    distance_points = remaining_loss / (total_volume * point_value)
    minimum_points = max(sym_info.trade_stops_level, 1)
    if distance_points <= minimum_points:
        return 0.0
    distance = distance_points * sym_info.point
    stop = current_price - distance if order_type == mt5.ORDER_TYPE_BUY else current_price + distance
    return round(stop, sym_info.digits)

def protect_position_with_sl(position, emergency_sl):
    tighter_already = (
        position.sl > 0
        and (
            (position.type == mt5.ORDER_TYPE_BUY and position.sl >= emergency_sl)
            or (position.type == mt5.ORDER_TYPE_SELL and position.sl <= emergency_sl)
        )
    )
    if tighter_already:
        return True
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": position.symbol,
        "position": position.ticket,
        "sl": emergency_sl,
        "tp": position.tp,
        "magic": CONFIG["magicNumber"],
    }
    result = mt5.order_send(request)
    return bool(result and result.retcode == mt5.TRADE_RETCODE_DONE)

def log_paper_trade(action, position, price, profit=0.0, reason=""):
    file_exists = os.path.isfile(PAPER_LOG_FILE)
    with open(PAPER_LOG_FILE, "a", newline="", encoding="utf-8-sig") as log_file:
        writer = csv.writer(log_file)
        if not file_exists:
            writer.writerow(["Timestamp", "Ticket", "Action", "Symbol", "Volume", "Price", "Reason", "EMA_Fast", "EMA_Slow", "ADX", "ATR", "Profit"])
        writer.writerow([
            datetime.now().isoformat(timespec="seconds"), position["ticket"], action,
            position["symbol"], position["volume"], round(price, 5), reason,
            bot_state["indicators"].get("EMA Fast", ""), bot_state["indicators"].get("EMA Slow", ""),
            bot_state["indicators"].get("ADX", ""), bot_state["indicators"].get("ATR", ""), round(profit, 2),
        ])

def paper_profit(position, exit_price):
    sym_info = mt5.symbol_info(position["symbol"])
    if not sym_info or sym_info.trade_tick_size <= 0:
        return 0.0
    direction = 1 if position["type"] == "BUY" else -1
    ticks = (exit_price - position["entry"]) * direction / sym_info.trade_tick_size
    return ticks * sym_info.trade_tick_value * position["volume"]

def close_paper_position(exit_price, reason):
    position = paper_state.get("position")
    if not position:
        return False
    profit = paper_profit(position, exit_price)
    paper_state["realized_pl"] += profit
    paper_state["floating_pl"] = 0.0
    paper_state["trades"] += 1
    if profit > 0:
        paper_state["wins"] += 1
        paper_state["consecutive_losses"] = 0
    elif profit < 0:
        paper_state["losses"] += 1
        paper_state["consecutive_losses"] += 1
    else:
        paper_state["consecutive_losses"] = 0
    log_paper_trade(f"CLOSE {position['type']}", position, exit_price, profit, reason)
    paper_state["position"] = None
    save_runtime_state()
    send_telegram(f"🧪 *PAPER TRADE CLOSED*\n{position['type']} {position['symbol']}\nP/L: {profit:.2f}\nReason: {reason}")
    return True

def open_paper_trade(symbol, action, lot, price, sl, tp, comment):
    if paper_state.get("position"):
        return False
    account = mt5.account_info()
    current_equity = (account.balance if account else 0.0) + paper_state["realized_pl"]
    if paper_state.get("week_start_balance") is None:
        paper_state["week_start_balance"] = current_equity
    if paper_state["consecutive_losses"] >= CONFIG.get("maxConsecutiveLosses", 5):
        send_telegram("🧪 *PAPER ENTRY BLOCKED*\nConsecutive-loss limit reached.")
        return False
    position = {
        "ticket": f"PAPER-{int(time.time())}", "symbol": symbol,
        "type": "BUY" if action == mt5.ORDER_TYPE_BUY else "SELL",
        "volume": float(lot), "entry": float(price), "sl": float(sl),
        "tp": float(tp), "opened_at": datetime.now(timezone.utc).isoformat(),
        "comment": comment,
    }
    paper_state["position"] = position
    log_paper_trade(f"OPEN {position['type']}", position, price, 0.0, comment)
    save_runtime_state()
    send_telegram(f"🧪 *PAPER TRADE OPENED*\n{position['type']} {symbol}\nPrice: {price}\nLot: {lot}\nSL: {sl}")
    return True

def paper_trade_manager(curr_atr):
    account = mt5.account_info()
    base_balance = account.balance if account else 0.0
    current_equity = base_balance + paper_state["realized_pl"] + paper_state["floating_pl"]
    week_key = datetime.now().strftime("%G-W%V")
    if paper_state.get("week_key") != week_key:
        paper_state["week_key"] = week_key
        paper_state["week_start_balance"] = current_equity
        paper_state["consecutive_losses"] = 0
        if bot_state.get("weekly_lockout_week") != week_key:
            bot_state["weekly_lockout_week"] = None
        save_runtime_state()
    week_start = paper_state.get("week_start_balance") or current_equity
    weekly_limit = week_start * CONFIG.get("maxWeeklyDrawdownRisk", 5.0) / 100
    if weekly_limit > 0 and current_equity <= week_start - weekly_limit:
        bot_state["weekly_lockout_week"] = week_key
        bot_state["auto_trade"] = False
        position = paper_state.get("position")
        if position:
            tick = mt5.symbol_info_tick(position["symbol"])
            if tick:
                close_paper_position(tick.bid if position["type"] == "BUY" else tick.ask, "Max Weekly Drawdown")
        save_runtime_state()
        return

    position = paper_state.get("position")
    if not position:
        return
    tick = mt5.symbol_info_tick(position["symbol"])
    sym_info = mt5.symbol_info(position["symbol"])
    if not tick or not sym_info:
        return
    current_price = tick.bid if position["type"] == "BUY" else tick.ask
    paper_state["floating_pl"] = paper_profit(position, current_price)

    if (position["type"] == "BUY" and current_price <= position["sl"]) or (
        position["type"] == "SELL" and current_price >= position["sl"]
    ):
        close_paper_position(current_price, "Stop Loss")
        return

    if CONFIG.get("strategyMode") == "donchian":
        exit_len = CONFIG.get("donchianExitLen", 10)
        rates = mt5.copy_rates_from_pos(SYMBOL, get_mt5_timeframe(TIMEFRAME_STR), 0, exit_len + 2)
        if rates is not None and len(rates) >= exit_len + 2:
            exit_df = pd.DataFrame(rates)
            closed_bar = exit_df.iloc[-2]
            prior = exit_df.iloc[-(exit_len + 2):-2]
            broken = (position["type"] == "BUY" and closed_bar["close"] < prior["low"].min()) or (
                position["type"] == "SELL" and closed_bar["close"] > prior["high"].max()
            )
            if broken:
                close_paper_position(current_price, "Donchian Exit")
                return

    profit_dist = current_price - position["entry"] if position["type"] == "BUY" else position["entry"] - current_price
    old_sl = position["sl"]
    if CONFIG.get("enableBE") and profit_dist >= curr_atr * CONFIG["beTriggerATR"]:
        extra = CONFIG.get("beProfitPoints", 20.0) * sym_info.point
        be_price = position["entry"] + extra if position["type"] == "BUY" else position["entry"] - extra
        position["sl"] = max(position["sl"], be_price) if position["type"] == "BUY" else min(position["sl"], be_price)
    if CONFIG.get("enableTrailing") and profit_dist >= curr_atr * CONFIG["trailStartATR"]:
        trail = current_price - curr_atr * CONFIG["trailDistanceATR"] if position["type"] == "BUY" else current_price + curr_atr * CONFIG["trailDistanceATR"]
        position["sl"] = max(position["sl"], trail) if position["type"] == "BUY" else min(position["sl"], trail)
    if position["sl"] != old_sl:
        save_runtime_state()

def execute_trade(symbol, action, lot, price, sl, tp, comment):
    if EXECUTION_MODE != "live":
        return open_paper_trade(symbol, action, lot, price, sl, tp, comment)
    if lot <= 0:
        send_telegram("⚠️ *ORDER BLOCKED*\nCalculated lot is below the broker minimum or risk budget.")
        return False
    sym_info = mt5.symbol_info(symbol)
    if not sym_info:
        return False
    price = round(price, sym_info.digits)
    sl = round(sl, sym_info.digits) if sl else 0.0
    tp = round(tp, sym_info.digits) if tp else 0.0
    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": lot,
        "type": action, "price": price, "sl": sl, "tp": tp, "deviation": 20,
        "magic": CONFIG["magicNumber"], "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC, "type_filling": get_order_filling_mode(sym_info),
    }
    check = mt5.order_check(request)
    if not check or check.retcode != 0:
        error_msg = check.comment if check else str(mt5.last_error())
        send_telegram(f"❌ *ORDER CHECK FAILED*\nเหตุผล: {error_msg}")
        return False
    # A single send is intentional: blind retries can duplicate a filled order after a timeout.
    res = mt5.order_send(request)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        action_str = "BUY" if action == mt5.ORDER_TYPE_BUY else "SELL"
        log_trade(res.order, f"OPEN {action_str}", symbol, lot, request["price"], comment)
        send_telegram(f"🤖 *AUTO-TRADE EXECUTED*\nออเดอร์: {comment}\nTicket: `{res.order}`\nPrice: {request['price']}\nLot: {lot}")
        return True
    error_msg = res.comment if res else str(mt5.last_error())
    send_telegram(f"❌ *AUTO-TRADE FAILED*\nระบบไม่ส่งคำสั่งซ้ำเพื่อป้องกันออเดอร์ซ้อน\nเหตุผล: {error_msg}")
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
    result = mt5.order_send(req)
    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
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
        sym_info = mt5.symbol_info(symbol)
        if not tick or not sym_info:
            time.sleep(1)
            continue
        action_type = mt5.ORDER_TYPE_SELL if p_type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if p_type == mt5.ORDER_TYPE_BUY else tick.ask
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": volume,
            "type": action_type, "position": ticket, "price": price, "deviation": 20,
            "magic": CONFIG["magicNumber"], "comment": "Close Order",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": get_order_filling_mode(sym_info),
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
    bot_pos = [p for p in positions if p.magic == CONFIG["magicNumber"]]
    if not bot_pos: return

    acc_info = mt5.account_info()
    balance = acc_info.balance if acc_info else 0.0
    is_news_blocked = check_news_impact()
    sym_info = mt5.symbol_info(SYMBOL)

    donchian_exit = None
    if CONFIG.get("strategyMode") == "donchian":
        exit_len = CONFIG.get("donchianExitLen", 10)
        exit_rates = mt5.copy_rates_from_pos(SYMBOL, get_mt5_timeframe(TIMEFRAME_STR), 0, exit_len + 2)
        if exit_rates is not None and len(exit_rates) >= exit_len + 2:
            exit_df = pd.DataFrame(exit_rates)
            closed_bar = exit_df.iloc[-2]
            prior_bars = exit_df.iloc[-(exit_len + 2):-2]
            donchian_exit = {
                "close": closed_bar["close"],
                "low": prior_bars["low"].min(),
                "high": prior_bars["high"].max(),
            }

    for o_type in [mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL]:
        group = [p for p in bot_pos if p.type == o_type]
        if not group: continue

        if donchian_exit:
            channel_broken = (
                o_type == mt5.ORDER_TYPE_BUY and donchian_exit["close"] < donchian_exit["low"]
            ) or (
                o_type == mt5.ORDER_TYPE_SELL and donchian_exit["close"] > donchian_exit["high"]
            )
            if channel_broken:
                for p in group:
                    close_position(p.ticket, p.symbol, p.type, p.volume, p.profit + p.swap, "Donchian Exit")
                continue
        
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

        tick = mt5.symbol_info_tick(SYMBOL)
        if not tick or not sym_info:
            continue
        curr_price = tick.bid if o_type == mt5.ORDER_TYPE_BUY else tick.ask
        
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
                emergency_sl = calculate_basket_emergency_sl(SYMBOL, group, new_lot, curr_price, o_type) if new_lot > 0 else 0.0
                protected = emergency_sl > 0 and all(protect_position_with_sl(p, emergency_sl) for p in group)
                if protected:
                    execute_trade(SYMBOL, o_type, new_lot, curr_price, emergency_sl, 0.0, f"DCA Step {len(group)}")
                else:
                    send_telegram(f"⚠️ *DCA BLOCKED*\nไม่สามารถยืนยัน Emergency SL ภายในความเสี่ยงตะกร้า {CONFIG.get('maxBasketRisk', 5.0)}% ได้")

def analyze_data():
    if not mt5.initialize(): return None
    rate_count = required_rate_count()
    rates = mt5.copy_rates_from_pos(SYMBOL, get_mt5_timeframe(TIMEFRAME_STR), 0, rate_count)
    if rates is None or len(rates) < rate_count: return None

    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    
    df['emaFast'] = ta.trend.ema_indicator(df['close'], window=CONFIG['emaFastLen'])
    df['emaSlow'] = ta.trend.ema_indicator(df['close'], window=CONFIG['emaSlowLen'])
    df['emaEntry'] = ta.trend.ema_indicator(df['close'], window=CONFIG['emaEntryLen'])
    df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=CONFIG['adxLen']).adx()
    df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=CONFIG['atrLen']).average_true_range()
    entry_len = CONFIG.get("donchianEntryLen", 20)
    regime_len = CONFIG.get("atrRegimeLookback", 100)
    df['donchianHigh'] = df['high'].rolling(entry_len).max().shift(1)
    df['donchianLow'] = df['low'].rolling(entry_len).min().shift(1)
    df['atrMedian'] = df['atr'].rolling(regime_len).median()

    df['candleRange'] = df['high'] - df['low']
    df['upperWick'] = df['high'] - df[['open', 'close']].max(axis=1)
    df['lowerWick'] = df[['open', 'close']].min(axis=1) - df['low']
    df['upperWickPct'] = (df['upperWick'] / df['candleRange']) * 100
    df['lowerWickPct'] = (df['lowerWick'] / df['candleRange']) * 100

    curr = df.iloc[-2]
    prev = df.iloc[-3]

    required_values = [
        curr['emaFast'], curr['emaSlow'], curr['emaEntry'], curr['adx'], curr['atr'],
        prev['emaEntry'], curr['lowerWickPct'], curr['upperWickPct'],
    ]
    if not all(math.isfinite(float(value)) for value in required_values):
        return None

    # 🔒 บังคับผลลัพธ์ให้เป็น True/False แบบ 100% (Bulletproof)
    isUptrend = bool(curr['emaFast'] > curr['emaSlow'])
    isDowntrend = bool(curr['emaFast'] < curr['emaSlow'])
    isTrending = bool(curr['adx'] > CONFIG.get('adxThresh', 20.0))
    
    buyTrigger = bool((curr['close'] > curr['emaEntry']) and (prev['close'] <= prev['emaEntry']))
    sellTrigger = bool((curr['close'] < curr['emaEntry']) and (prev['close'] >= prev['emaEntry']))

    if CONFIG.get("strategyMode") == "donchian":
        atr_ratio = curr['atr'] / curr['atrMedian'] if curr['atrMedian'] > 0 else 0.0
        regime_ok = CONFIG.get("atrRegimeMin", 0.75) <= atr_ratio <= CONFIG.get("atrRegimeMax", 2.0)
        buySignal = bool(regime_ok and curr['close'] > curr['donchianHigh'])
        sellSignal = bool(regime_ok and curr['close'] < curr['donchianLow'])
    else:
        buySignal = bool(isUptrend and isTrending and buyTrigger and (curr['lowerWickPct'] >= CONFIG.get('minWickPct', 5.0)))
        sellSignal = bool(isDowntrend and isTrending and sellTrigger and (curr['upperWickPct'] >= CONFIG.get('minWickPct', 5.0)))

    raw_signal = "BUY" if buySignal else "SELL" if sellSignal else "WAIT"
    is_filter_passed, filter_reason = check_market_filters(raw_signal)
    if is_filter_passed and raw_signal in ["BUY", "SELL"]:
        sym_info = mt5.symbol_info(SYMBOL)
        tick = mt5.symbol_info_tick(SYMBOL)
        sl_distance = curr['atr'] * CONFIG['atrMultiplier']
        spread_price = tick.ask - tick.bid if tick else float("inf")
        max_ratio = CONFIG.get("maxSpreadRiskRatio", 0.1)
        if not sym_info or sl_distance <= 0 or spread_price / sl_distance > max_ratio:
            is_filter_passed = False
            filter_reason = f"Spread/SL ratio too high ({spread_price / sl_distance:.1%} > {max_ratio:.1%})" if sl_distance > 0 else "Invalid stop distance"
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
            if CONFIG.get("strategyMode") == "donchian":
                tp_price = 0.0
            lot_size = calculate_lot_size(SYMBOL, sl_points, CONFIG['riskPercent'])

    bot_state.update({
        "latest_price": df.iloc[-1]['close'], "signal": final_signal, 
        "last_update": df.iloc[-1]['time'].strftime("%Y-%m-%d %H:%M:%S"),
        "signal_candle": curr['time'].strftime("%Y-%m-%d %H:%M:%S"),
        "tp": tp_price, "sl": sl_price,
        "sl_distance": abs(curr['close'] - sl_price) if final_signal != "WAIT" else 0.0,
        "lot": lot_size,
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

    return final_signal

def get_bot_positions():
    positions = mt5.positions_get(symbol=SYMBOL) or []
    return [p for p in positions if p.magic == CONFIG["magicNumber"]]

def get_bot_daily_pl():
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    deals = mt5.history_deals_get(midnight, datetime.now() + timedelta(days=1)) or []
    realized = sum(
        d.profit + d.commission + d.swap + d.fee
        for d in deals
        if d.type in (0, 1) and d.magic == CONFIG["magicNumber"]
    )
    floating = sum(p.profit + p.swap for p in get_bot_positions())
    return realized, floating

def current_week_key():
    return datetime.now().strftime("%G-W%V")

def get_bot_weekly_pl():
    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    deals = mt5.history_deals_get(week_start, now + timedelta(days=1)) or []
    realized = sum(d.profit + d.commission + d.swap + d.fee for d in deals
                   if d.type in (0, 1) and d.magic == CONFIG["magicNumber"])
    floating = sum(p.profit + p.swap for p in get_bot_positions())
    return realized, floating

def get_live_consecutive_losses():
    now = datetime.now()
    deals = mt5.history_deals_get(now - timedelta(days=90), now + timedelta(days=1)) or []
    exits = [d for d in deals if d.magic == CONFIG["magicNumber"] and d.type in (0, 1)
             and getattr(d, "entry", None) == getattr(mt5, "DEAL_ENTRY_OUT", 1)]
    losses = 0
    for deal in sorted(exits, key=lambda d: getattr(d, "time_msc", d.time), reverse=True):
        if deal.profit + deal.commission + deal.swap + deal.fee >= 0:
            break
        losses += 1
    return losses

async def drawdown_monitor():
    while True:
        await asyncio.to_thread(check_daily_drawdown)
        await asyncio.sleep(5)

def check_daily_drawdown():
    """Run one blocking MT5 drawdown check outside the asyncio event loop."""
    dd_limit = CONFIG.get("maxDailyDrawdownRisk", 0.0)
    if bot_state["is_running"] and dd_limit > 0:
        acc_info = mt5.account_info()
        if acc_info:
            daily_pl, floating_pl = get_bot_daily_pl()
            total_daily_loss = daily_pl + floating_pl
            day_start_balance = max(acc_info.balance - daily_pl, 0.0)
            max_loss_limit = day_start_balance * (dd_limit / 100)

            if max_loss_limit > 0 and total_daily_loss < 0 and abs(total_daily_loss) >= max_loss_limit:
                bot_state["is_running"] = False
                bot_state["auto_trade"] = False
                bot_state["daily_lockout_date"] = datetime.now().date().isoformat()
                save_runtime_state()
                closed_count = 0
                for p in get_bot_positions():
                    if close_position(p.ticket, p.symbol, p.type, p.volume, p.profit + p.swap, "Max Daily Drawdown Close"):
                        closed_count += 1
                msg = (f"🛑 *MAX DAILY DRAWDOWN REACHED*\nพอร์ตขาดทุนรายวันเกินขีดจำกัด {dd_limit}%\n"
                       f"📉 ขาดทุนรวมวันนี้: {round(total_daily_loss, 2)}\n"
                       f"🛡️ ระบบปิดออเดอร์ {closed_count} ไม้ และ **STOP BOT** อัตโนมัติ!")
                send_telegram(msg)
                return

    if bot_state["is_running"] and EXECUTION_MODE == "live":
        acc_info = mt5.account_info()
        weekly_limit_pct = CONFIG.get("maxWeeklyDrawdownRisk", 0.0)
        if acc_info and weekly_limit_pct > 0:
            realized, floating = get_bot_weekly_pl()
            week_start_balance = max(acc_info.balance - realized, 0.0)
            if week_start_balance > 0 and realized + floating <= -(week_start_balance * weekly_limit_pct / 100):
                bot_state["auto_trade"] = False
                bot_state["weekly_lockout_week"] = current_week_key()
                save_runtime_state()
                for position in get_bot_positions():
                    close_position(position.ticket, position.symbol, position.type, position.volume,
                                   position.profit + position.swap, "Max Weekly Drawdown Close")
                send_telegram(f"🛑 *MAX WEEKLY DRAWDOWN REACHED*\nระงับการเปิดออเดอร์ใหม่จนถึงสัปดาห์ถัดไป ({weekly_limit_pct}%)")
                return
        if get_live_consecutive_losses() >= CONFIG.get("maxConsecutiveLosses", 5):
            bot_state["auto_trade"] = False

async def bot_loop():
    global last_signal_candle
    while bot_state["is_running"]:
        signal = await asyncio.to_thread(analyze_data)
        is_news_blocked = await asyncio.to_thread(check_news_impact)
        if bot_state["indicators"].get("ATR"):
            manager = paper_trade_manager if EXECUTION_MODE != "live" else trade_manager
            await asyncio.to_thread(manager, bot_state["indicators"]["ATR"])

        if signal in ["BUY", "SELL"]:
            signal_time = datetime.strptime(bot_state["signal_candle"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            signal_close_time = signal_time + timedelta(seconds=timeframe_seconds(TIMEFRAME_STR))
            if bot_started_at and signal_close_time <= bot_started_at:
                last_signal_candle = bot_state["signal_candle"]
                save_runtime_state()
                await asyncio.sleep(2)
                continue
            if is_news_blocked:
                await asyncio.to_thread(send_telegram, f"📰 *SIGNAL PAUSED (HIGH-IMPACT NEWS)*\nมีสัญญาณ {signal} แต่ระบบงดเข้าออเดอร์เนื่องจากติดข่าว:\n_{bot_state['news_status']['reason']}_")
                await asyncio.sleep(60)
                continue

            signal_candle = bot_state["signal_candle"]
            if signal_candle == last_signal_candle:
                await asyncio.sleep(2)
                continue
            last_signal_candle = signal_candle
            save_runtime_state()
            msg = (f"🚨 *{SYMBOL} SIGNAL: {signal}* 🚨\n\n"
                   f"💰 *Entry Price:* {bot_state['latest_price']}\n"
                   f"📦 *Lot Size:* {bot_state['lot']} (Risk {CONFIG['riskPercent']}%)\n"
                   f"🎯 *Take Profit:* {round(bot_state['tp'], 4)}\n"
                   f"🛑 *Stop Loss:* {round(bot_state['sl'], 4)}\n\n"
                   f"📊 ATR: {bot_state['indicators']['ATR']} | ADX: {bot_state['indicators']['ADX']}\n"
                   f"🛡️ Filter Status: {bot_state['market_filter_status']['reason']}")
            await asyncio.to_thread(send_telegram, msg)

            if bot_state["auto_trade"]:
                positions = await asyncio.to_thread(mt5.positions_get, symbol=SYMBOL)
                bot_positions = [p for p in positions if p.magic == CONFIG["magicNumber"]] if positions else []
                if len(bot_positions) == 0:
                    action = mt5.ORDER_TYPE_BUY if signal == "BUY" else mt5.ORDER_TYPE_SELL
                    tick = await asyncio.to_thread(mt5.symbol_info_tick, SYMBOL)
                    if not tick:
                        await asyncio.sleep(2)
                        continue
                    curr_price = tick.ask if signal == "BUY" else tick.bid
                    sl_distance = bot_state["sl_distance"]
                    sl_price = curr_price - sl_distance if signal == "BUY" else curr_price + sl_distance
                    tp_price = 0.0 if CONFIG.get("strategyMode") == "donchian" else (curr_price + sl_distance * CONFIG["rrRatio"] if signal == "BUY" else curr_price - sl_distance * CONFIG["rrRatio"])
                    await asyncio.to_thread(execute_trade, SYMBOL, action, bot_state["lot"], curr_price, sl_price, tp_price, "Master7 First Entry")
                else:
                    await asyncio.to_thread(send_telegram, "⚠️ *SIGNAL IGNORED*\nบอทยังมีออเดอร์ค้างอยู่ ข้ามการเปิดไม้ใหม่")
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
            
            await asyncio.to_thread(send_telegram, combined_message)
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
            await asyncio.to_thread(send_telegram, msg)

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
        if EXECUTION_MODE != "live":
            acc_data["balance"] = acc_info.balance + paper_state["realized_pl"]
            acc_data["equity"] = acc_data["balance"] + paper_state["floating_pl"]
            acc_data["floating_pl"] = paper_state["floating_pl"]
            acc_data["daily_pl"] = paper_state["realized_pl"]

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

    if EXECUTION_MODE != "live" and paper_state.get("position"):
        p = paper_state["position"]
        current = tick.bid if tick and p["type"] == "BUY" else tick.ask if tick else p["entry"]
        point = sym_info.point if sym_info and sym_info.point > 0 else 0.01
        diff = current - p["entry"] if p["type"] == "BUY" else p["entry"] - current
        pos_data = [{"ticket": p["ticket"], "symbol": p["symbol"], "type": p["type"], "volume": p["volume"],
                     "pips": round(diff / point, 1), "profit": round(paper_state["floating_pl"], 2), "bot": "Paper"}]

    runtime = {"execution_mode": EXECUTION_MODE.upper(), "strategy": CONFIG.get("strategyMode", "legacy").upper(),
               "timeframe": TIMEFRAME_STR, "paper": paper_state}
    return {"sys": sys_data, "acc": acc_data, "bot": bot_state, "market": market_data, "positions": pos_data, "runtime": runtime}

@app.get("/")
def read_root(request: Request): return templates.TemplateResponse(request=request, name="index.html", context={"state": bot_state})

@app.get("/health")
def health():
    terminal = mt5.terminal_info()
    return {"status": "ok", "mt5_connected": bool(terminal and terminal.connected)}

@app.get("/api/config")
def get_config(): return CONFIG

@app.post("/api/config")
def update_config(new_config: ConfigModel):
    global CONFIG
    CONFIG.update(new_config.model_dump())
    save_config()
    send_telegram("⚙️ *SYSTEM CONFIG UPDATED*\nผู้ใช้งานได้เปลี่ยนการตั้งค่าบอทผ่าน Web Dashboard")
    return {"status": "success", "message": "Configuration updated successfully."}

@app.post("/api/start")
async def start_bot():
    global bot_task, bot_started_at
    today = datetime.now().date().isoformat()
    if bot_state.get("daily_lockout_date") == today:
        return JSONResponse(status_code=423, content={"status": "locked", "message": "Daily drawdown lockout is active until tomorrow."})
    if bot_state.get("daily_lockout_date") != today:
        bot_state["daily_lockout_date"] = None
        save_runtime_state()
    week_key = current_week_key()
    if bot_state.get("weekly_lockout_week") == week_key:
        return JSONResponse(status_code=423, content={"status": "locked", "message": "Weekly drawdown lockout is active until next week."})
    if bot_state.get("weekly_lockout_week") and bot_state.get("weekly_lockout_week") != week_key:
        bot_state["weekly_lockout_week"] = None
        paper_state["week_start_balance"] = None
        paper_state["consecutive_losses"] = 0
        save_runtime_state()
    if not bot_state["is_running"]:
        bot_state["is_running"] = True
        bot_started_at = datetime.now(timezone.utc)
        bot_task = asyncio.create_task(bot_loop())
    return {"status": "started"}

@app.post("/api/stop")
def stop_bot():
    bot_state["is_running"] = False
    bot_state["auto_trade"] = False
    return {"status": "stopped", "auto_trade": False}

@app.post("/api/toggle_auto")
def toggle_auto():
    today = datetime.now().date().isoformat()
    if bot_state.get("daily_lockout_date") == today:
        return JSONResponse(status_code=423, content={"status": "locked", "auto_trade": False})
    if bot_state.get("weekly_lockout_week") == current_week_key():
        return JSONResponse(status_code=423, content={"status": "locked", "auto_trade": False})
    loss_streak = paper_state["consecutive_losses"] if EXECUTION_MODE != "live" else get_live_consecutive_losses()
    if loss_streak >= CONFIG.get("maxConsecutiveLosses", 5):
        return JSONResponse(status_code=423, content={"status": "locked", "auto_trade": False,
                                                       "message": "Consecutive-loss limit reached."})
    if not bot_state["is_running"]:
        return JSONResponse(status_code=409, content={"status": "stopped", "auto_trade": False})
    bot_state["auto_trade"] = not bot_state["auto_trade"]
    return {"auto_trade": bot_state["auto_trade"]}

@app.post("/api/close_all")
def close_all_orders():
    if EXECUTION_MODE != "live" and paper_state.get("position"):
        tick = mt5.symbol_info_tick(paper_state["position"]["symbol"])
        if not tick:
            return JSONResponse(status_code=503, content={"status": "error", "message": "No market tick available."})
        price = tick.bid if paper_state["position"]["type"] == "BUY" else tick.ask
        close_paper_position(price, "Manual Web Close")
        return {"status": "success", "closed": 1}
    positions = get_bot_positions()
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
    log_file = TRADE_LOG_FILE if EXECUTION_MODE == "live" else PAPER_LOG_FILE
    if not os.path.exists(log_file):
        return {"status": "success", "logs": []}
    try:
        df = pd.read_csv(log_file)
        # ดึง 50 รายการล่าสุด และกลับด้านให้รายการใหม่สุดอยู่บน
        last_logs = df.tail(50).fillna("").to_dict(orient="records")
        last_logs.reverse()
        return {"status": "success", "logs": last_logs}
    except Exception as e:
        return {"status": "error", "message": str(e)}
