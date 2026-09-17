import argparse
import json
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

import main
from backtest_v2 import build_m15


def run_breakout_retest(df, initial_balance=10_000.0, risk_percent=0.5):
    df = df.copy()
    df["atr_q30"] = df.atr.rolling(100).quantile(0.30)
    df["atr_q85"] = df.atr.rolling(100).quantile(0.85)
    df["exit_high"] = df.high.rolling(10).max().shift(1)
    df["exit_low"] = df.low.rolling(10).min().shift(1)
    balance = initial_balance
    peak = balance
    max_drawdown = 0.0
    trades = []
    pending = None
    position = None

    for i in range(201, len(df) - 1):
        bar = df.iloc[i]
        if position:
            held = i - position["entry_index"]
            if position["side"] == "BUY":
                stop_hit = bar.low <= position["stop"]
                channel_exit = bar.close < bar.exit_low
                weak_timeout = held >= 12 and bar.close < position["entry"] + 0.5 * position["initial_risk"]
                if stop_hit or channel_exit or weak_timeout:
                    exit_price = position["stop"] if stop_hit else bar.close
                else:
                    if bar.high >= position["entry"] + position["initial_risk"]:
                        position["stop"] = max(position["stop"], position["entry"])
                    if bar.high >= position["entry"] + 1.5 * position["initial_risk"]:
                        position["stop"] = max(position["stop"], bar.close - 3.0 * bar.atr)
                    continue
                r_multiple = (exit_price - position["entry"]) / position["initial_risk"]
            else:
                ask_low, ask_high = bar.low + bar.spread_price, bar.high + bar.spread_price
                ask_close = bar.close + bar.spread_price
                stop_hit = ask_high >= position["stop"]
                channel_exit = ask_close > bar.exit_high
                weak_timeout = held >= 12 and ask_close > position["entry"] - 0.5 * position["initial_risk"]
                if stop_hit or channel_exit or weak_timeout:
                    exit_price = position["stop"] if stop_hit else ask_close
                else:
                    if ask_low <= position["entry"] - position["initial_risk"]:
                        position["stop"] = min(position["stop"], position["entry"])
                    if ask_low <= position["entry"] - 1.5 * position["initial_risk"]:
                        position["stop"] = min(position["stop"], ask_close + 3.0 * bar.atr)
                    continue
                r_multiple = (position["entry"] - exit_price) / position["initial_risk"]

            pnl = position["risk_amount"] * r_multiple
            balance += pnl
            peak = max(peak, balance)
            max_drawdown = max(max_drawdown, (peak - balance) / peak * 100)
            trades.append({"pnl": float(pnl), "r": float(r_multiple), "held": held})
            position = None
            continue

        if pending and i > pending["expires"]:
            pending = None

        if pending and pending["start"] <= i <= pending["expires"]:
            candle_body = abs(bar.close - bar.open)
            if pending["side"] == "BUY":
                lower_wick = min(bar.open, bar.close) - bar.low
                rejection = bar.low <= pending["level"] and bar.close > pending["level"] and bar.close > bar.open and lower_wick >= candle_body * 0.5
            else:
                upper_wick = bar.high - max(bar.open, bar.close)
                rejection = bar.high >= pending["level"] and bar.close < pending["level"] and bar.close < bar.open and upper_wick >= candle_body * 0.5
            if rejection:
                next_bar = df.iloc[i + 1]
                entry = next_bar.open + next_bar.spread_price if pending["side"] == "BUY" else next_bar.open
                stop = bar.low - 0.2 * bar.atr if pending["side"] == "BUY" else bar.high + 0.2 * bar.atr
                initial_risk = entry - stop if pending["side"] == "BUY" else stop - entry
                if initial_risk > 0 and next_bar.spread_price <= 0.10 * initial_risk:
                    position = {
                        "side": pending["side"], "entry_index": i + 1,
                        "entry": float(entry), "stop": float(stop),
                        "initial_risk": float(initial_risk),
                        "risk_amount": balance * risk_percent / 100,
                    }
                pending = None
                continue

        needed = bar[["atr", "atr_q30", "atr_q85", "entry_high", "entry_low", "h1_close", "ema200"]]
        if not pd.notna(needed).all() or not 7 <= df.index[i].hour < 17:
            continue
        volatility_ok = bar.atr_q30 <= bar.atr <= bar.atr_q85
        ema_rising = bar.ema200 > df.iloc[i - 12].ema200
        ema_falling = bar.ema200 < df.iloc[i - 12].ema200
        buy_break = volatility_ok and ema_rising and bar.h1_close > bar.ema200 and bar.close > bar.entry_high + 0.2 * bar.atr
        sell_break = volatility_ok and ema_falling and bar.h1_close < bar.ema200 and bar.close < bar.entry_low - 0.2 * bar.atr
        if buy_break or sell_break:
            pending = {
                "side": "BUY" if buy_break else "SELL",
                "level": float(bar.entry_high if buy_break else bar.entry_low),
                "start": i + 1, "expires": i + 4,
            }

    wins = sum(t["pnl"] > 0 for t in trades)
    gross_profit = sum(max(t["pnl"], 0) for t in trades)
    gross_loss = -sum(min(t["pnl"], 0) for t in trades)
    return {
        "strategy": "V5 M15 breakout-retest + H1 EMA200 slope",
        "risk_percent": risk_percent, "trades": len(trades), "wins": wins,
        "losses": len(trades) - wins,
        "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0.0,
        "return_pct": round((balance / initial_balance - 1) * 100, 2),
        "net_profit": round(balance - initial_balance, 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "max_drawdown_pct": round(max_drawdown, 2),
        "average_r": round(sum(t["r"] for t in trades) / len(trades), 3) if trades else 0.0,
        "average_hold_minutes": round(sum(t["held"] for t in trades) * 15 / len(trades), 1) if trades else 0.0,
    }


def main_cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=20_000)
    parser.add_argument("--output", default="backtest_v5_results.json")
    args = parser.parse_args()
    path = r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe"
    if not mt5.initialize(path=path):
        raise RuntimeError(f"MT5 initialization failed: {mt5.last_error()}")
    try:
        mt5.symbol_select(main.SYMBOL, True)
        info = mt5.symbol_info(main.SYMBOL)
        rates = mt5.copy_rates_from_pos(main.SYMBOL, mt5.TIMEFRAME_M15, 0, args.bars)
        if rates is None or not len(rates):
            raise RuntimeError(f"No rates: {mt5.last_error()}")
        df = build_m15(rates, info.point)
        result = {
            "symbol": main.SYMBOL, "bars_m15": len(df),
            "first_bar": str(df.index[0]), "last_bar": str(df.index[-1]),
            "result": run_breakout_retest(df),
            "limitations": ["News filter excluded", "Uses M15 bars rather than real tick sequencing", "Session fixed at 07:00-17:00 UTC"],
        }
        serialized = json.dumps(result, indent=2, default=lambda value: value.item())
        Path(args.output).write_text(serialized, encoding="utf-8")
        print(serialized)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main_cli()
