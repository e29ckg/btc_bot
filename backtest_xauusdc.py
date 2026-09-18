"""Conservative MT5 backtest for the XAUUSDc M1 scalping candidate.

Run this on the Windows machine that has the target broker's MT5 terminal and
historical data. Bars are treated as bid prices. When SL and TP are both
touched in one M1 bar, the stop is assumed to be hit first.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd
import ta


@dataclass
class Trade:
    side: str
    signal_time: str
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    sl_initial: float
    tp_initial: float
    volume: float
    pnl_gross: float
    costs: float
    pnl_net: float
    balance: float
    r_multiple: float
    exit_reason: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest XAUUSDc M1 scalping strategy from MT5 history")
    parser.add_argument("--symbol", default="XAUUSDc")
    parser.add_argument("--start", default="2026-06-17", help="UTC date, YYYY-MM-DD")
    parser.add_argument("--end", default="2026-09-17", help="UTC date, YYYY-MM-DD (inclusive)")
    parser.add_argument("--balance", type=float, default=1000.0)
    parser.add_argument("--risk-percent", type=float, default=0.25)
    parser.add_argument("--daily-loss-percent", type=float, default=3.0)
    parser.add_argument("--session-start", type=int, default=7, help="UTC hour, inclusive")
    parser.add_argument("--session-end", type=int, default=20, help="UTC hour, exclusive")
    parser.add_argument("--slippage-points", type=float, default=5.0)
    parser.add_argument("--commission-per-lot", type=float, default=0.0, help="Round-turn account-currency cost")
    parser.add_argument("--max-hold-bars", type=int, default=30)
    parser.add_argument("--output-dir", default="backtest_results")
    return parser.parse_args()


def utc_date(value: str, end_of_day: bool = False) -> datetime:
    parsed = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return parsed + timedelta(days=1) if end_of_day else parsed


def initialize_mt5(symbol: str):
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"Symbol {symbol!r} was not found in this MT5 terminal")
    if not info.visible and not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Could not select {symbol!r} in Market Watch")
    return info


def fetch_rates(symbol: str, timeframe: int, start: datetime, end: datetime) -> pd.DataFrame:
    rates = mt5.copy_rates_range(symbol, timeframe, start, end)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"No historical rates returned for {symbol}: {mt5.last_error()}")
    frame = pd.DataFrame(rates)
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    return frame.sort_values("time").drop_duplicates("time").reset_index(drop=True)


def prepare_data(m1: pd.DataFrame, m5: pd.DataFrame) -> pd.DataFrame:
    data = m1.copy()
    data["ema9"] = ta.trend.ema_indicator(data["close"], window=9)
    data["atr"] = ta.volatility.AverageTrueRange(
        data["high"], data["low"], data["close"], window=14
    ).average_true_range()
    adx_indicator = ta.trend.ADXIndicator(data["high"], data["low"], data["close"], window=14)
    data["adx"] = adx_indicator.adx()
    data["adx_prev"] = data["adx"].shift(1)
    candle_range = (data["high"] - data["low"]).replace(0, pd.NA)
    data["lower_wick_pct"] = ((data[["open", "close"]].min(axis=1) - data["low"]) / candle_range) * 100
    data["upper_wick_pct"] = ((data["high"] - data[["open", "close"]].max(axis=1)) / candle_range) * 100
    data["swing_low"] = data["low"].rolling(5).min()
    data["swing_high"] = data["high"].rolling(5).max()

    trend = m5.copy()
    trend["m5_ema20"] = ta.trend.ema_indicator(trend["close"], window=20)
    trend["m5_ema50"] = ta.trend.ema_indicator(trend["close"], window=50)
    trend = trend[["time", "m5_ema20", "m5_ema50"]]

    # Use only a completed M5 bar: make its trend values available five minutes later.
    trend["time"] = trend["time"] + pd.Timedelta(minutes=5)
    return pd.merge_asof(data, trend, on="time", direction="backward").dropna().reset_index(drop=True)


def in_session(hour: int, start: int, end: int) -> bool:
    return start <= hour < end if start < end else hour >= start or hour < end


def normalize_volume(raw: float, info) -> float:
    if raw <= 0 or info.volume_step <= 0:
        return 0.0
    volume = math.floor(raw / info.volume_step) * info.volume_step
    if volume < info.volume_min:
        return 0.0
    return round(min(volume, info.volume_max), 8)


def profit_for(symbol: str, side: str, volume: float, entry: float, exit_price: float) -> float:
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    result = mt5.order_calc_profit(order_type, symbol, volume, entry, exit_price)
    if result is None:
        raise RuntimeError(f"order_calc_profit failed: {mt5.last_error()}")
    return float(result)


def position_size(symbol: str, side: str, entry: float, sl: float, risk_amount: float, info) -> float:
    one_lot_loss = abs(profit_for(symbol, side, 1.0, entry, sl))
    return normalize_volume(risk_amount / one_lot_loss, info) if one_lot_loss > 0 else 0.0


def max_drawdown(equity_curve: list[float]) -> tuple[float, float]:
    peak = equity_curve[0]
    max_amount = 0.0
    max_percent = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        drawdown = peak - equity
        drawdown_percent = (drawdown / peak * 100) if peak > 0 else 0.0
        max_amount = max(max_amount, drawdown)
        max_percent = max(max_percent, drawdown_percent)
    return max_amount, max_percent


def run_backtest(data: pd.DataFrame, info, args: argparse.Namespace) -> tuple[list[Trade], dict]:
    balance = args.balance
    equity_curve = [balance]
    trades: list[Trade] = []
    consecutive_losses = 0
    cooldown_until: pd.Timestamp | None = None
    active_day = None
    day_start_balance = balance
    daily_locked = False
    point = info.point

    i = 1
    while i < len(data) - 1:
        signal_bar = data.iloc[i]
        previous = data.iloc[i - 1]
        day = signal_bar["time"].date()
        if day != active_day:
            active_day = day
            day_start_balance = balance
            daily_locked = False

        if balance <= 0 or daily_locked:
            i += 1
            continue
        if cooldown_until is not None and signal_bar["time"] < cooldown_until:
            i += 1
            continue
        if not in_session(signal_bar["time"].hour, args.session_start, args.session_end):
            i += 1
            continue

        spread_points = max(float(signal_bar.get("spread", 0)), 0.0)
        spread_price = spread_points * point
        spread_to_atr = spread_price / signal_bar["atr"] if signal_bar["atr"] > 0 else 999
        if spread_to_atr > 0.12:
            i += 1
            continue

        adx_ok = signal_bar["adx"] >= 23 and signal_bar["adx"] >= signal_bar["adx_prev"]
        buy = (
            signal_bar["m5_ema20"] > signal_bar["m5_ema50"]
            and previous["close"] <= previous["ema9"]
            and signal_bar["close"] > signal_bar["ema9"]
            and signal_bar["lower_wick_pct"] >= 20
            and adx_ok
        )
        sell = (
            signal_bar["m5_ema20"] < signal_bar["m5_ema50"]
            and previous["close"] >= previous["ema9"]
            and signal_bar["close"] < signal_bar["ema9"]
            and signal_bar["upper_wick_pct"] >= 20
            and adx_ok
        )
        if not buy and not sell:
            i += 1
            continue

        side = "BUY" if buy else "SELL"
        entry_bar = data.iloc[i + 1]
        slippage = args.slippage_points * point
        if side == "BUY":
            entry = entry_bar["open"] + spread_price + slippage
            atr_stop = entry - 1.3 * signal_bar["atr"]
            structure_stop = signal_bar["swing_low"] - 0.2 * signal_bar["atr"]
            sl = min(atr_stop, structure_stop)
            tp = entry + 1.3 * (entry - sl)
        else:
            entry = entry_bar["open"] - slippage
            atr_stop = entry + 1.3 * signal_bar["atr"]
            structure_stop = signal_bar["swing_high"] + 0.2 * signal_bar["atr"]
            sl = max(atr_stop, structure_stop)
            tp = entry - 1.3 * (sl - entry)

        initial_risk_distance = abs(entry - sl)
        risk_amount = balance * (args.risk_percent / 100)
        volume = position_size(args.symbol, side, entry, sl, risk_amount, info)
        if volume <= 0 or initial_risk_distance <= 0:
            i += 1
            continue

        exit_price = entry
        exit_reason = "MAX_HOLD"
        exit_index = min(i + 1 + args.max_hold_bars, len(data) - 1)
        current_sl = sl
        for j in range(i + 1, exit_index + 1):
            bar = data.iloc[j]
            if side == "BUY":
                # Conservative ordering for ambiguous intrabar paths.
                if bar["low"] <= current_sl:
                    exit_price, exit_reason, exit_index = current_sl - slippage, "SL", j
                    break
                if bar["high"] >= tp:
                    exit_price, exit_reason, exit_index = tp - slippage, "TP", j
                    break
                favorable = bar["high"] - entry
                if favorable >= 1.2 * initial_risk_distance:
                    current_sl = max(current_sl, bar["close"] - 0.7 * bar["atr"])
                elif favorable >= 0.9 * initial_risk_distance:
                    current_sl = max(current_sl, entry + spread_price)
            else:
                bar_spread = max(float(bar.get("spread", 0)), 0.0) * point
                ask_high = bar["high"] + bar_spread
                ask_low = bar["low"] + bar_spread
                if ask_high >= current_sl:
                    exit_price, exit_reason, exit_index = current_sl + slippage, "SL", j
                    break
                if ask_low <= tp:
                    exit_price, exit_reason, exit_index = tp + slippage, "TP", j
                    break
                favorable = entry - bar["low"]
                if favorable >= 1.2 * initial_risk_distance:
                    current_sl = min(current_sl, bar["close"] + bar_spread + 0.7 * bar["atr"])
                elif favorable >= 0.9 * initial_risk_distance:
                    current_sl = min(current_sl, entry - bar_spread)
            exit_price = bar["close"] - slippage if side == "BUY" else bar["close"] + max(float(bar.get("spread", 0)), 0.0) * point + slippage

        gross = profit_for(args.symbol, side, volume, entry, exit_price)
        costs = args.commission_per_lot * volume
        net = gross - costs
        balance += net
        realized_risk = abs(profit_for(args.symbol, side, volume, entry, sl))
        r_multiple = net / realized_risk if realized_risk > 0 else 0.0
        trades.append(
            Trade(
                side=side,
                signal_time=signal_bar["time"].isoformat(),
                entry_time=entry_bar["time"].isoformat(),
                exit_time=data.iloc[exit_index]["time"].isoformat(),
                entry=round(entry, info.digits),
                exit=round(exit_price, info.digits),
                sl_initial=round(sl, info.digits),
                tp_initial=round(tp, info.digits),
                volume=volume,
                pnl_gross=round(gross, 2),
                costs=round(costs, 2),
                pnl_net=round(net, 2),
                balance=round(balance, 2),
                r_multiple=round(r_multiple, 3),
                exit_reason=exit_reason,
            )
        )
        equity_curve.append(balance)

        consecutive_losses = consecutive_losses + 1 if net < 0 else 0
        if consecutive_losses >= 3:
            cooldown_until = data.iloc[exit_index]["time"] + pd.Timedelta(minutes=60)
            consecutive_losses = 0
        if balance <= day_start_balance * (1 - args.daily_loss_percent / 100):
            daily_locked = True
        i = exit_index + 3  # Three completed bars before another entry.

    wins = [trade.pnl_net for trade in trades if trade.pnl_net > 0]
    losses = [trade.pnl_net for trade in trades if trade.pnl_net < 0]
    max_dd_amount, max_dd_percent = max_drawdown(equity_curve)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    summary = {
        "symbol": args.symbol,
        "timeframe": "M1 entry / M5 trend",
        "period_utc": f"{args.start} to {args.end}",
        "initial_balance": round(args.balance, 2),
        "final_balance": round(balance, 2),
        "net_profit": round(balance - args.balance, 2),
        "return_percent": round((balance / args.balance - 1) * 100, 2),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_percent": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "expectancy": round((balance - args.balance) / len(trades), 3) if trades else 0.0,
        "max_drawdown": round(max_dd_amount, 2),
        "max_drawdown_percent": round(max_dd_percent, 2),
        "risk_percent": args.risk_percent,
        "daily_loss_percent": args.daily_loss_percent,
        "session_utc": f"{args.session_start:02d}:00-{args.session_end:02d}:00",
        "commission_per_lot_round_turn": args.commission_per_lot,
        "slippage_points_each_side": args.slippage_points,
        "limitations": [
            "M1 OHLC cannot reconstruct tick order; SL is assumed first when SL and TP share a bar.",
            "Historical news events are not filtered.",
            "Commission must be supplied for the broker account if it is not zero.",
        ],
    }
    return trades, summary


def save_results(trades: list[Trade], summary: dict, output_dir: str) -> tuple[Path, Path]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    trades_path = target / "xauusdc_m1_trades.csv"
    summary_path = target / "xauusdc_m1_summary.json"
    with trades_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(Trade.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(trade) for trade in trades)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return trades_path, summary_path


def main():
    args = parse_args()
    if args.balance <= 0 or not 0 < args.risk_percent <= 1:
        raise ValueError("balance must be positive and risk-percent must be between 0 and 1")
    start = utc_date(args.start)
    end = utc_date(args.end, end_of_day=True)
    if start >= end:
        raise ValueError("start must be before end")

    try:
        info = initialize_mt5(args.symbol)
        warmup = start - timedelta(days=7)
        m1 = fetch_rates(args.symbol, mt5.TIMEFRAME_M1, warmup, end)
        m5 = fetch_rates(args.symbol, mt5.TIMEFRAME_M5, warmup, end)
        data = prepare_data(m1, m5)
        data = data[(data["time"] >= start) & (data["time"] < end)].reset_index(drop=True)
        trades, summary = run_backtest(data, info, args)
        trades_path, summary_path = save_results(trades, summary, args.output_dir)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"\nTrades: {trades_path.resolve()}")
        print(f"Summary: {summary_path.resolve()}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
