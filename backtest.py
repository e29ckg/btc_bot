import argparse
import json
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd
import ta

import main


def prepare_rates(rates, config, point):
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time")
    df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=config["emaFastLen"])
    df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=config["emaSlowLen"])
    df["ema_entry"] = ta.trend.ema_indicator(df["close"], window=config["emaEntryLen"])
    df["adx"] = ta.trend.ADXIndicator(
        df["high"], df["low"], df["close"], window=config["adxLen"]
    ).adx()
    df["atr"] = ta.volatility.AverageTrueRange(
        df["high"], df["low"], df["close"], window=config["atrLen"]
    ).average_true_range()
    candle_range = (df["high"] - df["low"]).replace(0, pd.NA)
    df["upper_wick_pct"] = (df["high"] - df[["open", "close"]].max(axis=1)) / candle_range * 100
    df["lower_wick_pct"] = (df[["open", "close"]].min(axis=1) - df["low"]) / candle_range * 100

    h1 = df["close"].resample("1h", label="right", closed="right").last().dropna().to_frame()
    h1["h1_fast"] = ta.trend.ema_indicator(h1["close"], window=config["emaFastLen"])
    h1["h1_slow"] = ta.trend.ema_indicator(h1["close"], window=config["emaSlowLen"])
    df = pd.merge_asof(
        df.reset_index().sort_values("time"),
        h1[["h1_fast", "h1_slow"]].reset_index().sort_values("time"),
        on="time",
        direction="backward",
    ).set_index("time")
    df["spread_price"] = df["spread"] * point
    return df


def run_backtest(df, config, max_spread_points, initial_balance=10_000.0):
    balance = initial_balance
    peak = balance
    max_drawdown = 0.0
    trades = []
    i = 2
    while i < len(df) - 1:
        curr, prev = df.iloc[i], df.iloc[i - 1]
        buy = (
            curr.ema_fast > curr.ema_slow
            and curr.adx > config["adxThresh"]
            and curr.close > curr.ema_entry
            and prev.close <= prev.ema_entry
            and curr.lower_wick_pct >= config["minWickPct"]
            and (not config["enableMTFFilter"] or curr.h1_fast > curr.h1_slow)
        )
        sell = (
            curr.ema_fast < curr.ema_slow
            and curr.adx > config["adxThresh"]
            and curr.close < curr.ema_entry
            and prev.close >= prev.ema_entry
            and curr.upper_wick_pct >= config["minWickPct"]
            and (not config["enableMTFFilter"] or curr.h1_fast < curr.h1_slow)
        )
        if (not buy and not sell) or curr.spread > max_spread_points or pd.isna(curr.atr):
            i += 1
            continue

        side = "BUY" if buy else "SELL"
        entry_bar = df.iloc[i + 1]
        spread = entry_bar.spread_price
        entry = entry_bar.open + spread if buy else entry_bar.open
        sl_distance = curr.atr * config["atrMultiplier"]
        sl = entry - sl_distance if buy else entry + sl_distance
        tp = entry + sl_distance * config["rrRatio"] if buy else entry - sl_distance * config["rrRatio"]
        risk_amount = balance * config["riskPercent"] / 100
        exit_price = None
        outcome_r = 0.0
        j = i + 1
        while j < len(df):
            bar = df.iloc[j]
            if buy:
                stop_hit, target_hit = bar.low <= sl, bar.high >= tp
            else:
                ask_low, ask_high = bar.low + bar.spread_price, bar.high + bar.spread_price
                stop_hit, target_hit = ask_high >= sl, ask_low <= tp
            if stop_hit:  # Conservative when both levels occur in one candle.
                exit_price, outcome_r = sl, -1.0
                break
            if target_hit:
                exit_price, outcome_r = tp, config["rrRatio"]
                break
            j += 1
        if exit_price is None:
            break
        pnl = risk_amount * outcome_r
        balance += pnl
        peak = max(peak, balance)
        max_drawdown = max(max_drawdown, (peak - balance) / peak * 100)
        trades.append({"side": side, "entry_time": str(df.index[i + 1]), "exit_time": str(df.index[j]), "r": outcome_r, "pnl": pnl})
        i = j + 1

    wins = sum(t["r"] > 0 for t in trades)
    gross_profit = sum(max(t["pnl"], 0) for t in trades)
    gross_loss = -sum(min(t["pnl"], 0) for t in trades)
    return {
        "max_spread_points": max_spread_points,
        "trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0.0,
        "net_profit": round(balance - initial_balance, 2),
        "return_pct": round((balance / initial_balance - 1) * 100, 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "max_drawdown_pct": round(max_drawdown, 2),
        "ending_balance": round(balance, 2),
    }


def main_cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=50_000)
    parser.add_argument("--output", default="backtest_results.json")
    args = parser.parse_args()
    terminal_path = r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe"
    if not mt5.initialize(path=terminal_path):
        raise RuntimeError(f"MT5 initialization failed: {mt5.last_error()}")
    try:
        if not mt5.symbol_select(main.SYMBOL, True):
            raise RuntimeError(f"Cannot select {main.SYMBOL}: {mt5.last_error()}")
        info = mt5.symbol_info(main.SYMBOL)
        rates = mt5.copy_rates_from_pos(main.SYMBOL, main.get_mt5_timeframe(main.TIMEFRAME_STR), 0, args.bars)
        if rates is None or not len(rates):
            raise RuntimeError(f"No rates: {mt5.last_error()}")
        df = prepare_rates(rates, main.CONFIG, info.point)
        results = {
            "symbol": main.SYMBOL,
            "timeframe": main.TIMEFRAME_STR,
            "first_bar": str(df.index[0]),
            "last_bar": str(df.index[-1]),
            "bars": len(df),
            "limitations": ["News filter excluded: historical calendar archive unavailable", "DCA excluded because it is disabled in current config", "Intrabar SL/TP collision assumes SL first"],
            "scenarios": [
                run_backtest(df, main.CONFIG, main.CONFIG["maxSpreadPoints"]),
                run_backtest(df, main.CONFIG, 1200.0),
            ],
        }
        Path(args.output).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(json.dumps(results, indent=2))
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main_cli()
