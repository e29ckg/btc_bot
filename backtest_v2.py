import argparse
import json
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd
import ta

import main


def build_m15(rates, point):
    m1 = pd.DataFrame(rates)
    m1["time"] = pd.to_datetime(m1["time"], unit="s", utc=True)
    m1 = m1.set_index("time")
    m15 = m1.resample("15min", label="right", closed="right").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), spread=("spread", "max"), volume=("tick_volume", "sum"),
    ).dropna()
    m15["spread_price"] = m15["spread"] * point
    m15["atr"] = ta.volatility.AverageTrueRange(m15.high, m15.low, m15.close, window=14).average_true_range()
    m15["adx"] = ta.trend.ADXIndicator(m15.high, m15.low, m15.close, window=14).adx()
    m15["atr_median"] = m15.atr.rolling(100).median()
    m15["entry_high"] = m15.high.rolling(20).max().shift(1)
    m15["entry_low"] = m15.low.rolling(20).min().shift(1)
    m15["exit_high"] = m15.high.rolling(10).max().shift(1)
    m15["exit_low"] = m15.low.rolling(10).min().shift(1)

    h1 = m1.close.resample("1h", label="right", closed="right").last().dropna().to_frame()
    h1["ema200"] = ta.trend.ema_indicator(h1.close, window=200)
    m15 = pd.merge_asof(
        m15.reset_index().sort_values("time"),
        h1[["close", "ema200"]].rename(columns={"close": "h1_close"}).reset_index().sort_values("time"),
        on="time", direction="backward",
    ).set_index("time")
    return m15


def run_v2(df, risk_percent=0.5, initial_balance=10_000.0, adx_threshold=None):
    balance = initial_balance
    peak = balance
    max_drawdown = 0.0
    trades = []
    position = None

    for i in range(201, len(df) - 1):
        bar = df.iloc[i]
        next_bar = df.iloc[i + 1]
        if position:
            if position["side"] == "BUY":
                stop_hit = bar.low <= position["stop"]
                channel_exit = bar.close < bar.exit_low
                if stop_hit or channel_exit:
                    exit_price = position["stop"] if stop_hit else bar.close
                    r_multiple = (exit_price - position["entry"]) / position["initial_risk"]
                else:
                    position["stop"] = max(position["stop"], bar.close - 3.0 * bar.atr)
                    continue
            else:
                ask_high = bar.high + bar.spread_price
                ask_close = bar.close + bar.spread_price
                stop_hit = ask_high >= position["stop"]
                channel_exit = ask_close > bar.exit_high
                if stop_hit or channel_exit:
                    exit_price = position["stop"] if stop_hit else ask_close
                    r_multiple = (position["entry"] - exit_price) / position["initial_risk"]
                else:
                    position["stop"] = min(position["stop"], ask_close + 3.0 * bar.atr)
                    continue
            pnl = position["risk_amount"] * r_multiple
            balance += pnl
            peak = max(peak, balance)
            max_drawdown = max(max_drawdown, (peak - balance) / peak * 100)
            trades.append({**position, "exit_time": str(df.index[i]), "r": round(r_multiple, 4), "pnl": round(pnl, 2)})
            position = None
            continue

        finite = pd.notna(bar[["atr", "atr_median", "entry_high", "entry_low", "h1_close", "ema200"]]).all()
        if not finite or bar.atr_median <= 0:
            continue
        atr_ratio = bar.atr / bar.atr_median
        regime_ok = 0.75 <= atr_ratio <= 2.0
        cost_ok = 2.0 * bar.atr >= 8.0 * next_bar.spread_price
        adx_ok = adx_threshold is None or bar.adx >= adx_threshold
        buy = regime_ok and cost_ok and adx_ok and bar.close > bar.entry_high and bar.h1_close > bar.ema200
        sell = regime_ok and cost_ok and adx_ok and bar.close < bar.entry_low and bar.h1_close < bar.ema200
        if not buy and not sell:
            continue

        side = "BUY" if buy else "SELL"
        entry = next_bar.open + next_bar.spread_price if buy else next_bar.open
        initial_risk = 2.0 * bar.atr
        stop = entry - initial_risk if buy else entry + initial_risk
        position = {
            "side": side,
            "entry_time": str(df.index[i + 1]),
            "entry": float(entry),
            "stop": float(stop),
            "initial_risk": float(initial_risk),
            "risk_amount": balance * risk_percent / 100,
        }

    wins = sum(t["pnl"] > 0 for t in trades)
    gross_profit = sum(max(t["pnl"], 0) for t in trades)
    gross_loss = -sum(min(t["pnl"], 0) for t in trades)
    return {
        "strategy": "M15 Donchian(20/10) + H1 EMA200 + ATR regime",
        "adx_threshold": adx_threshold,
        "risk_percent": risk_percent,
        "trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0.0,
        "net_profit": round(balance - initial_balance, 2),
        "return_pct": round((balance / initial_balance - 1) * 100, 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "max_drawdown_pct": round(max_drawdown, 2),
        "ending_balance": round(balance, 2),
        "average_r": round(sum(t["r"] for t in trades) / len(trades), 3) if trades else 0.0,
    }


def main_cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=50_000)
    parser.add_argument("--output", default="backtest_v2_results.json")
    args = parser.parse_args()
    terminal_path = r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe"
    if not mt5.initialize(path=terminal_path):
        raise RuntimeError(f"MT5 initialization failed: {mt5.last_error()}")
    try:
        mt5.symbol_select(main.SYMBOL, True)
        info = mt5.symbol_info(main.SYMBOL)
        rates = mt5.copy_rates_from_pos(main.SYMBOL, mt5.TIMEFRAME_M15, 0, args.bars)
        if rates is None or not len(rates):
            raise RuntimeError(f"No rates: {mt5.last_error()}")
        df = build_m15(rates, info.point)
        result = {
            "symbol": main.SYMBOL,
            "source_bars_m15": len(rates),
            "bars_m15": len(df),
            "first_bar": str(df.index[0]),
            "last_bar": str(df.index[-1]),
            "results": [
                run_v2(df, adx_threshold=None),
                run_v2(df, adx_threshold=20.0),
                run_v2(df, adx_threshold=25.0),
            ],
            "limitations": [
                "Further out-of-sample and forward testing is required",
                "Uses broker M15 bars and recorded spread, not real tick sequencing",
                "News filter excluded",
                "Open position at end of sample is excluded",
            ],
        }
        serialized = json.dumps(result, indent=2, default=lambda value: value.item())
        Path(args.output).write_text(serialized, encoding="utf-8")
        print(serialized)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main_cli()
