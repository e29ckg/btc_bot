import argparse
import json
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd
import ta

import main


def prepare_m5(rates, point):
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time")
    df["spread_price"] = df.spread * point
    df["atr"] = ta.volatility.AverageTrueRange(df.high, df.low, df.close, window=14).average_true_range()
    df["atr_median"] = df.atr.rolling(100).median()
    df["adx"] = ta.trend.ADXIndicator(df.high, df.low, df.close, window=14).adx()
    df["volume_median"] = df.tick_volume.rolling(20).median()
    df["entry_high"] = df.high.rolling(12).max().shift(1)
    df["entry_low"] = df.low.rolling(12).min().shift(1)

    h1 = df.close.resample("1h", label="right", closed="right").last().dropna().to_frame()
    h1["ema200"] = ta.trend.ema_indicator(h1.close, window=200)
    return pd.merge_asof(
        df.reset_index().sort_values("time"),
        h1[["close", "ema200"]].rename(columns={"close": "h1_close"}).reset_index().sort_values("time"),
        on="time", direction="backward",
    ).set_index("time")


def run_v3(df, initial_balance=10_000.0, risk_percent=0.25):
    balance = initial_balance
    peak = balance
    max_drawdown = 0.0
    trades = []
    position = None
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
            held_bars = i - position["entry_index"]
            if position["side"] == "BUY":
                stop_hit = bar.low <= position["stop"]
                target_hit = bar.high >= position["target"]
                if stop_hit or target_hit:
                    exit_price = position["stop"] if stop_hit else position["target"]
                elif held_bars >= 12:
                    exit_price = bar.close
                else:
                    if bar.high >= position["be_trigger"]:
                        position["stop"] = max(position["stop"], position["entry"])
                    continue
                r_multiple = (exit_price - position["entry"]) / position["initial_risk"]
            else:
                ask_low = bar.low + bar.spread_price
                ask_high = bar.high + bar.spread_price
                ask_close = bar.close + bar.spread_price
                stop_hit = ask_high >= position["stop"]
                target_hit = ask_low <= position["target"]
                if stop_hit or target_hit:
                    exit_price = position["stop"] if stop_hit else position["target"]
                elif held_bars >= 12:
                    exit_price = ask_close
                else:
                    if ask_low <= position["be_trigger"]:
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
                "pnl": round(float(pnl), 2), "held_bars": held_bars,
            })
            position = None
            cooldown_until = i + 3
            continue

        if i <= cooldown_until:
            continue
        if balance <= day_start_balance * 0.985 or consecutive_losses >= 4:
            continue
        needed = bar[["atr", "atr_median", "adx", "volume_median", "entry_high", "entry_low", "h1_close", "ema200"]]
        if not pd.notna(needed).all() or bar.atr_median <= 0:
            continue

        stop_distance = 1.2 * bar.atr
        compressed = bar.atr <= 0.85 * bar.atr_median
        momentum = bar.adx > 20 and bar.adx > df.iloc[i - 1].adx
        volume_ok = bar.tick_volume > bar.volume_median
        cost_ok = bar.spread_price <= 0.15 * stop_distance
        buy = compressed and momentum and volume_ok and cost_ok and bar.close > bar.entry_high and bar.h1_close > bar.ema200
        sell = compressed and momentum and volume_ok and cost_ok and bar.close < bar.entry_low and bar.h1_close < bar.ema200
        if not buy and not sell:
            continue

        next_bar = df.iloc[i + 1]
        side = "BUY" if buy else "SELL"
        entry = next_bar.open + next_bar.spread_price if buy else next_bar.open
        stop = entry - stop_distance if buy else entry + stop_distance
        target = entry + 1.5 * stop_distance if buy else entry - 1.5 * stop_distance
        be_trigger = entry + 0.8 * stop_distance if buy else entry - 0.8 * stop_distance
        position = {
            "side": side, "entry_index": i + 1, "entry_time": str(df.index[i + 1]),
            "entry": float(entry), "stop": float(stop), "target": float(target),
            "be_trigger": float(be_trigger), "initial_risk": float(stop_distance),
            "risk_amount": balance * risk_percent / 100,
        }

    wins = sum(t["pnl"] > 0 for t in trades)
    gross_profit = sum(max(t["pnl"], 0) for t in trades)
    gross_loss = -sum(min(t["pnl"], 0) for t in trades)
    return {
        "strategy": "M5 compression breakout + H1 EMA200",
        "risk_percent": risk_percent,
        "trades": len(trades), "wins": wins, "losses": len(trades) - wins,
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
    parser.add_argument("--output", default="backtest_v3_results.json")
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
            "result": run_v3(df),
            "limitations": [
                "Uses M5 bars and recorded spread, not real tick sequencing",
                "News filter excluded", "Intrabar SL/TP collision assumes SL first",
            ],
        }
        serialized = json.dumps(result, indent=2, default=lambda value: value.item())
        Path(args.output).write_text(serialized, encoding="utf-8")
        print(serialized)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main_cli()
