import argparse
import itertools
import json
from pathlib import Path

import MetaTrader5 as mt5
import numpy as np
import pandas as pd
import ta

import main


def load_data(bars):
    terminal_path = r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe"
    if not mt5.initialize(path=terminal_path):
        raise RuntimeError(f"MT5 initialization failed: {mt5.last_error()}")
    mt5.symbol_select(main.SYMBOL, True)
    info = mt5.symbol_info(main.SYMBOL)
    rates = mt5.copy_rates_from_pos(main.SYMBOL, mt5.TIMEFRAME_M5, 0, bars)
    if rates is None or not len(rates):
        raise RuntimeError(f"No M5 rates: {mt5.last_error()}")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df.time, unit="s", utc=True)
    df = df.set_index("time")
    df["spread_price"] = df.spread * info.point
    df["atr"] = ta.volatility.AverageTrueRange(df.high, df.low, df.close, window=14).average_true_range()
    df["atr_median"] = df.atr.rolling(100).median()
    trend = df.close.resample("15min", closed="left", label="right").last().dropna().to_frame()
    return df, trend, info.point


def metrics(trades, initial_balance, ending_balance, max_drawdown):
    wins = sum(pnl > 0 for pnl in trades)
    gross_profit = sum(max(pnl, 0) for pnl in trades)
    gross_loss = -sum(min(pnl, 0) for pnl in trades)
    return {
        "trades": len(trades),
        "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0.0,
        "return_pct": round((ending_balance / initial_balance - 1) * 100, 3),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else 99.0,
        "max_drawdown_pct": round(max_drawdown, 3),
    }


def simulate(data, start, end, config, initial_balance=10_000.0):
    balance = initial_balance
    peak = balance
    max_drawdown = 0.0
    position = None
    trades = []
    warmup = max(300, start)

    for i in range(warmup, end - 1):
        bar = data[i]
        if position is not None:
            side, entry, stop, initial_risk, risk_amount = position
            if side == 1:
                stop_hit = bar[2] <= stop
                channel_exit = bar[3] < bar[8]
                if stop_hit or channel_exit:
                    exit_price = stop if stop_hit else bar[3]
                    r_multiple = (exit_price - entry) / initial_risk
                else:
                    profit_distance = bar[3] - entry
                    if profit_distance >= bar[5] * 2.0:
                        stop = max(stop, entry + config["be_points_price"])
                    if profit_distance >= bar[5] * config["trail_start"]:
                        stop = max(stop, bar[3] - bar[5] * config["trail_distance"])
                    position = (side, entry, stop, initial_risk, risk_amount)
                    continue
            else:
                ask_high = bar[1] + bar[4]
                ask_close = bar[3] + bar[4]
                stop_hit = ask_high >= stop
                channel_exit = ask_close > bar[7]
                if stop_hit or channel_exit:
                    exit_price = stop if stop_hit else ask_close
                    r_multiple = (entry - exit_price) / initial_risk
                else:
                    profit_distance = entry - ask_close
                    if profit_distance >= bar[5] * 2.0:
                        stop = min(stop, entry - config["be_points_price"])
                    if profit_distance >= bar[5] * config["trail_start"]:
                        stop = min(stop, ask_close + bar[5] * config["trail_distance"])
                    position = (side, entry, stop, initial_risk, risk_amount)
                    continue

            pnl = risk_amount * r_multiple
            balance += pnl
            peak = max(peak, balance)
            max_drawdown = max(max_drawdown, (peak - balance) / peak * 100)
            trades.append(pnl)
            position = None
            continue

        if i < start or not np.isfinite(bar[5:13]).all() or bar[6] <= 0:
            continue
        initial_risk = bar[5] * config["atr_multiplier"]
        atr_ratio = bar[5] / bar[6]
        spread_ok = bar[4] / initial_risk <= main.CONFIG["maxSpreadRiskRatio"]
        regime_ok = config["atr_min"] <= atr_ratio <= main.CONFIG["atrRegimeMax"]
        buy = regime_ok and spread_ok and bar[3] > bar[9] and bar[11] > bar[12]
        sell = regime_ok and spread_ok and bar[3] < bar[10] and bar[11] < bar[12]
        if not buy and not sell:
            continue
        next_bar = data[i + 1]
        side = 1 if buy else -1
        entry = next_bar[0] + next_bar[4] if buy else next_bar[0]
        stop = entry - initial_risk if buy else entry + initial_risk
        risk_amount = balance * main.CONFIG["riskPercent"] / 100
        position = (side, entry, stop, initial_risk, risk_amount)

    return metrics(trades, initial_balance, balance, max_drawdown)


def main_cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=50_000)
    parser.add_argument("--output", default="optimize_m5_donchian_results.json")
    args = parser.parse_args()
    try:
        df, trend, point = load_data(args.bars)
        split = int(len(df) * 0.65)
        results = []
        channel_cache = {}
        trend_cache = {}
        for entry, exit_len, trend_ema, atr_mult, atr_min, trail_start, trail_dist in itertools.product(
            (15, 20, 30, 40), (5, 10, 15), (100, 200, 300), (1.5, 2.0, 2.5),
            (0.75, 0.9), (2.5, 3.0), (0.75, 1.0),
        ):
            if exit_len >= entry:
                continue
            channel_key = (entry, exit_len)
            if channel_key not in channel_cache:
                channel_cache[channel_key] = (
                    df.low.rolling(exit_len).min().shift(1).to_numpy(),
                    df.high.rolling(exit_len).max().shift(1).to_numpy(),
                    df.low.rolling(entry).min().shift(1).to_numpy(),
                    df.high.rolling(entry).max().shift(1).to_numpy(),
                )
            if trend_ema not in trend_cache:
                trend_frame = trend.copy()
                trend_frame["ema"] = ta.trend.ema_indicator(trend_frame.close, window=trend_ema)
                merged = pd.merge_asof(
                    df.reset_index()[["time"]], trend_frame.reset_index().sort_values("time"),
                    on="time", direction="backward",
                )
                trend_cache[trend_ema] = (merged.close.to_numpy(), merged.ema.to_numpy())
            exit_low, exit_high, entry_low, entry_high = channel_cache[channel_key]
            trend_close, trend_average = trend_cache[trend_ema]
            data = np.column_stack((
                df.open, df.high, df.low, df.close, df.spread_price, df.atr, df.atr_median,
                exit_high, exit_low, entry_high, entry_low, trend_close, trend_average,
            ))
            config = {
                "entry": entry, "exit": exit_len, "trend_ema": trend_ema,
                "atr_multiplier": atr_mult, "atr_min": atr_min,
                "trail_start": trail_start, "trail_distance": trail_dist,
                "be_points_price": main.CONFIG["beProfitPoints"] * point,
            }
            train = simulate(data, 300, split, config)
            test = simulate(data, split, len(data), config)
            if train["trades"] >= 50 and test["trades"] >= 25:
                score = min(train["profit_factor"], test["profit_factor"]) - 0.02 * test["max_drawdown_pct"]
                results.append({"score": round(score, 4), "config": config, "train": train, "test": test})

        results.sort(key=lambda row: row["score"], reverse=True)
        robust = [row for row in results if row["train"]["return_pct"] > 0 and row["test"]["return_pct"] > 0
                  and row["train"]["profit_factor"] > 1 and row["test"]["profit_factor"] > 1]
        report = {
            "symbol": main.SYMBOL, "bars": len(df), "first_bar": str(df.index[0]),
            "last_bar": str(df.index[-1]), "split_time": str(df.index[split]),
            "tested_configs": len(results), "robust_configs": len(robust),
            "best_robust": robust[:10], "best_overall": results[:10],
        }
        serialized = json.dumps(report, indent=2)
        Path(args.output).write_text(serialized, encoding="utf-8")
        print(serialized)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main_cli()
