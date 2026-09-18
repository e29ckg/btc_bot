import argparse
import json
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd
import ta

import main


def prepare_data(rates, point, config, trend_rule="15min"):
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time")
    df["spread_price"] = df.spread * point
    df["atr"] = ta.volatility.AverageTrueRange(
        df.high, df.low, df.close, window=config["atrLen"]
    ).average_true_range()
    df["atr_median"] = df.atr.rolling(config["atrRegimeLookback"]).median()
    df["entry_high"] = df.high.rolling(config["donchianEntryLen"]).max().shift(1)
    df["entry_low"] = df.low.rolling(config["donchianEntryLen"]).min().shift(1)
    df["exit_high"] = df.high.rolling(config["donchianExitLen"]).max().shift(1)
    df["exit_low"] = df.low.rolling(config["donchianExitLen"]).min().shift(1)

    # Label higher-timeframe closes at the moment they become available to
    # avoid using an unfinished trend candle.
    trend = df.close.resample(trend_rule, closed="left", label="right").last().dropna().to_frame()
    trend["trend_ema"] = ta.trend.ema_indicator(trend.close, window=config["trendEmaLen"])
    return pd.merge_asof(
        df.reset_index().sort_values("time"),
        trend.rename(columns={"close": "trend_close"}).reset_index().sort_values("time"),
        on="time", direction="backward",
    ).set_index("time")


def run_live_rules(df, config, point, initial_balance=10_000.0):
    balance = initial_balance
    peak = balance
    max_drawdown = 0.0
    position = None
    trades = []

    for i in range(max(config["atrRegimeLookback"], 201), len(df) - 1):
        bar = df.iloc[i]
        if position:
            if position["side"] == "BUY":
                stop_hit = bar.low <= position["stop"]
                channel_exit = bar.close < bar.exit_low
                if stop_hit or channel_exit:
                    exit_price = position["stop"] if stop_hit else bar.close
                    r_multiple = (exit_price - position["entry"]) / position["initial_risk"]
                else:
                    profit_distance = bar.close - position["entry"]
                    if config["enableBE"] and profit_distance >= bar.atr * config["beTriggerATR"]:
                        position["stop"] = max(position["stop"], position["entry"] + config["beProfitPoints"] * point)
                    if config["enableTrailing"] and profit_distance >= bar.atr * config["trailStartATR"]:
                        position["stop"] = max(position["stop"], bar.close - bar.atr * config["trailDistanceATR"])
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
                    profit_distance = position["entry"] - ask_close
                    if config["enableBE"] and profit_distance >= bar.atr * config["beTriggerATR"]:
                        position["stop"] = min(position["stop"], position["entry"] - config["beProfitPoints"] * point)
                    if config["enableTrailing"] and profit_distance >= bar.atr * config["trailStartATR"]:
                        position["stop"] = min(position["stop"], ask_close + bar.atr * config["trailDistanceATR"])
                    continue

            pnl = position["risk_amount"] * r_multiple
            balance += pnl
            peak = max(peak, balance)
            max_drawdown = max(max_drawdown, (peak - balance) / peak * 100)
            trades.append({"pnl": float(pnl), "r": float(r_multiple)})
            position = None
            continue

        required = bar[["atr", "atr_median", "entry_high", "entry_low", "trend_close", "trend_ema"]]
        if not pd.notna(required).all() or bar.atr_median <= 0:
            continue
        atr_ratio = bar.atr / bar.atr_median
        regime_ok = config["atrRegimeMin"] <= atr_ratio <= config["atrRegimeMax"]
        initial_risk = bar.atr * config["atrMultiplier"]
        spread_ok = (bar.spread <= config["maxSpreadPoints"] and initial_risk > 0
                     and bar.spread_price / initial_risk <= config.get("maxSpreadRiskRatio", 0.1))
        buy = regime_ok and spread_ok and bar.close > bar.entry_high and bar.trend_close > bar.trend_ema
        sell = regime_ok and spread_ok and bar.close < bar.entry_low and bar.trend_close < bar.trend_ema
        if not buy and not sell:
            continue

        next_bar = df.iloc[i + 1]
        side = "BUY" if buy else "SELL"
        entry = next_bar.open + next_bar.spread_price if buy else next_bar.open
        stop = entry - initial_risk if buy else entry + initial_risk
        position = {
            "side": side, "entry": float(entry), "stop": float(stop),
            "initial_risk": float(initial_risk),
            "risk_amount": balance * config["riskPercent"] / 100,
        }

    wins = sum(t["pnl"] > 0 for t in trades)
    gross_profit = sum(max(t["pnl"], 0) for t in trades)
    gross_loss = -sum(min(t["pnl"], 0) for t in trades)
    return {
        "strategy": "Live Donchian rules",
        "risk_percent": config["riskPercent"], "trades": len(trades),
        "wins": wins, "losses": len(trades) - wins,
        "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0.0,
        "return_pct": round((balance / initial_balance - 1) * 100, 2),
        "net_profit": round(balance - initial_balance, 2),
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss else None,
        "max_drawdown_pct": round(max_drawdown, 2),
        "average_r": round(sum(t["r"] for t in trades) / len(trades), 3) if trades else 0.0,
    }


def main_cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=20_000)
    parser.add_argument("--output", default="backtest_live_donchian_results.json")
    parser.add_argument("--entry-timeframe", default=main.TIMEFRAME_STR)
    parser.add_argument("--trend-timeframe", default=main.TREND_TIMEFRAME_STR)
    parser.add_argument("--entry-len", type=int)
    parser.add_argument("--exit-len", type=int)
    parser.add_argument("--trend-ema", type=int)
    parser.add_argument("--atr-multiplier", type=float)
    parser.add_argument("--atr-min", type=float)
    parser.add_argument("--trail-start", type=float)
    parser.add_argument("--trail-distance", type=float)
    args = parser.parse_args()
    terminal_path = r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe"
    if not mt5.initialize(path=terminal_path):
        raise RuntimeError(f"MT5 initialization failed: {mt5.last_error()}")
    try:
        mt5.symbol_select(main.SYMBOL, True)
        info = mt5.symbol_info(main.SYMBOL)
        timeframe_map = {
            "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
            "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
        }
        rule_map = {"M5": "5min", "M15": "15min", "M30": "30min", "H1": "1h", "H4": "4h"}
        if args.entry_timeframe not in timeframe_map or args.trend_timeframe not in rule_map:
            raise ValueError("Unsupported entry or trend timeframe")
        rates = mt5.copy_rates_from_pos(main.SYMBOL, timeframe_map[args.entry_timeframe], 0, args.bars)
        if rates is None or not len(rates):
            raise RuntimeError(f"No rates: {mt5.last_error()}")
        config = main.CONFIG.copy()
        overrides = {
            "donchianEntryLen": args.entry_len, "donchianExitLen": args.exit_len,
            "trendEmaLen": args.trend_ema, "atrMultiplier": args.atr_multiplier,
            "atrRegimeMin": args.atr_min, "trailStartATR": args.trail_start,
            "trailDistanceATR": args.trail_distance,
        }
        config.update({key: value for key, value in overrides.items() if value is not None})
        df = prepare_data(rates, info.point, config, rule_map[args.trend_timeframe])
        result = {
            "symbol": main.SYMBOL, "entry_timeframe": args.entry_timeframe,
            "trend_timeframe": args.trend_timeframe, "bars": len(df),
            "first_bar": str(df.index[0]), "last_bar": str(df.index[-1]),
            "config": {key: config[key] for key in overrides},
            "result": run_live_rules(df, config, info.point),
            "limitations": [
                "Bar-level approximation of tick-driven BE and trailing updates",
                "News filter and daily lockout excluded",
                "Open position at sample end excluded",
            ],
        }
        serialized = json.dumps(result, indent=2, default=lambda value: value.item())
        Path(args.output).write_text(serialized, encoding="utf-8")
        print(serialized)
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main_cli()
