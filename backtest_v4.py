import argparse
import json
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

import main
from backtest_v3 import prepare_m5


def run_order_block(df, initial_balance=10_000.0, risk_percent=0.25):
    df = df.copy()
    df["structure_high"] = df.high.rolling(30).max().shift(1)
    df["structure_low"] = df.low.rolling(30).min().shift(1)
    balance = initial_balance
    peak = balance
    max_drawdown = 0.0
    trades = []
    position = None
    pending = None
    cooldown_until = -1
    day = None
    day_start_balance = balance
    consecutive_losses = 0

    for i in range(201, len(df) - 1):
        bar = df.iloc[i]
        current_day = df.index[i].date()
        if current_day != day:
            day = current_day
            day_start_balance = balance
            consecutive_losses = 0

        if position:
            held = i - position["entry_index"]
            if position["side"] == "BUY":
                stop_hit, target_hit = bar.low <= position["stop"], bar.high >= position["target"]
                if stop_hit or target_hit:
                    exit_price = position["stop"] if stop_hit else position["target"]
                elif held >= 24:
                    exit_price = bar.close
                else:
                    if bar.high >= position["entry"] + position["initial_risk"]:
                        position["stop"] = max(position["stop"], position["entry"])
                    continue
                r_multiple = (exit_price - position["entry"]) / position["initial_risk"]
            else:
                ask_low, ask_high = bar.low + bar.spread_price, bar.high + bar.spread_price
                ask_close = bar.close + bar.spread_price
                stop_hit, target_hit = ask_high >= position["stop"], ask_low <= position["target"]
                if stop_hit or target_hit:
                    exit_price = position["stop"] if stop_hit else position["target"]
                elif held >= 24:
                    exit_price = ask_close
                else:
                    if ask_low <= position["entry"] - position["initial_risk"]:
                        position["stop"] = min(position["stop"], position["entry"])
                    continue
                r_multiple = (position["entry"] - exit_price) / position["initial_risk"]

            pnl = position["risk_amount"] * r_multiple
            balance += pnl
            peak = max(peak, balance)
            max_drawdown = max(max_drawdown, (peak - balance) / peak * 100)
            consecutive_losses = consecutive_losses + 1 if pnl < 0 else 0
            trades.append({
                "side": position["side"], "entry_time": position["entry_time"],
                "exit_time": str(df.index[i]), "r": round(float(r_multiple), 4),
                "pnl": round(float(pnl), 2), "held_bars": held,
            })
            position = None
            cooldown_until = i + 3
            continue

        if pending and i > pending["expires"]:
            pending = None
        if i <= cooldown_until or balance <= day_start_balance * 0.985 or consecutive_losses >= 4:
            continue

        if pending and i > pending["created"]:
            rejection = (
                bar.low <= pending["high"] and bar.close > pending["high"]
                if pending["side"] == "BUY"
                else bar.high >= pending["low"] and bar.close < pending["low"]
            )
            if rejection:
                next_bar = df.iloc[i + 1]
                entry = next_bar.open + next_bar.spread_price if pending["side"] == "BUY" else next_bar.open
                stop = pending["stop_anchor"] - 0.2 * bar.atr if pending["side"] == "BUY" else pending["stop_anchor"] + 0.2 * bar.atr
                initial_risk = entry - stop if pending["side"] == "BUY" else stop - entry
                cost_ok = initial_risk > 0 and next_bar.spread_price <= 0.10 * initial_risk
                if cost_ok:
                    target = entry + 2.5 * initial_risk if pending["side"] == "BUY" else entry - 2.5 * initial_risk
                    position = {
                        "side": pending["side"], "entry_index": i + 1,
                        "entry_time": str(df.index[i + 1]), "entry": float(entry),
                        "stop": float(stop), "target": float(target),
                        "initial_risk": float(initial_risk),
                        "risk_amount": balance * risk_percent / 100,
                    }
                pending = None
                continue

        required = bar[["atr", "structure_high", "structure_low", "h1_close", "ema200"]]
        if not pd.notna(required).all() or bar.atr <= 0:
            continue
        candle_range = bar.high - bar.low
        body = abs(bar.close - bar.open)
        volume_impulse = bar.tick_volume >= 1.5 * bar.volume_median
        displacement = candle_range >= 2.0 * bar.atr and candle_range > 0 and body / candle_range >= 0.7 and volume_impulse
        ema_rising = bar.ema200 > df.iloc[i - 12].ema200
        ema_falling = bar.ema200 < df.iloc[i - 12].ema200
        bullish_break = displacement and bar.close > bar.structure_high and bar.h1_close > bar.ema200 and ema_rising
        bearish_break = displacement and bar.close < bar.structure_low and bar.h1_close < bar.ema200 and ema_falling
        if not bullish_break and not bearish_break:
            continue

        lookback = df.iloc[max(0, i - 5):i]
        candidates = lookback[lookback.close < lookback.open] if bullish_break else lookback[lookback.close > lookback.open]
        if candidates.empty:
            continue
        order_candle = candidates.iloc[-1]
        pending = {
            "side": "BUY" if bullish_break else "SELL",
            "low": float(min(order_candle.open, order_candle.close)),
            "high": float(max(order_candle.open, order_candle.close)),
            "stop_anchor": float(order_candle.low if bullish_break else order_candle.high),
            "created": i, "expires": i + 12,
        }

    wins = sum(t["pnl"] > 0 for t in trades)
    gross_profit = sum(max(t["pnl"], 0) for t in trades)
    gross_loss = -sum(min(t["pnl"], 0) for t in trades)
    return {
        "strategy": "M5 quality order block rejection + H1 EMA200 slope",
        "risk_percent": risk_percent, "trades": len(trades), "wins": wins,
        "losses": len(trades) - wins,
        "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0.0,
        "net_profit": round(balance - initial_balance, 2),
        "return_pct": round((balance / initial_balance - 1) * 100, 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "max_drawdown_pct": round(max_drawdown, 2), "ending_balance": round(balance, 2),
        "average_r": round(sum(t["r"] for t in trades) / len(trades), 3) if trades else 0.0,
        "average_hold_minutes": round(sum(t["held_bars"] for t in trades) * 5 / len(trades), 1) if trades else 0.0,
    }


def main_cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=50_000)
    parser.add_argument("--output", default="backtest_v4_results.json")
    args = parser.parse_args()
    path = r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe"
    if not mt5.initialize(path=path):
        raise RuntimeError(f"MT5 initialization failed: {mt5.last_error()}")
    try:
        mt5.symbol_select(main.SYMBOL, True)
        info = mt5.symbol_info(main.SYMBOL)
        rates = mt5.copy_rates_from_pos(main.SYMBOL, mt5.TIMEFRAME_M5, 0, args.bars)
        if rates is None or not len(rates):
            raise RuntimeError(f"No rates: {mt5.last_error()}")
        df = prepare_m5(rates, info.point)
        result = {
            "symbol": main.SYMBOL, "source_bars_m5": len(rates),
            "first_bar": str(df.index[0]), "last_bar": str(df.index[-1]),
            "result": run_order_block(df),
            "rules": {
                "displacement": "range >= 2 ATR, body >= 70%, volume >= 1.5x median, close breaks 30-bar structure",
                "order_block": "body of latest opposite candle in previous 5 bars; stop beyond full candle",
                "entry": "rejection closes beyond block within 12 bars; enter next open",
                "exit": "SL beyond block + 0.2 ATR, TP 2.5R, BE at 1R, time stop 24 bars",
            },
            "limitations": ["Uses M5 bars, not real tick sequencing", "News filter excluded", "SL wins if SL and TP occur in same bar"],
        }
        serialized = json.dumps(result, indent=2, default=lambda value: value.item())
        Path(args.output).write_text(serialized, encoding="utf-8")
        print(serialized)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main_cli()
