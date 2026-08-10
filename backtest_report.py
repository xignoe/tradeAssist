#!/usr/bin/env python3
"""Render output/backtest_report.html — equity curves, metrics and a parameter
sweep for the "buy the biggest gap to target, sell near target, rotate" strategy.

Run: python backtest_report.py
"""

import json
import warnings
from pathlib import Path

import pandas as pd

import backtest as bt

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
START = "2015-01-01"
POSITIONS, EXIT_BAND = 10, 0.20


def curve_points(curve, n=420):
    """Downsample an equity curve for plotting."""
    step = max(1, len(curve) // n)
    s = curve.iloc[::step]
    if s.index[-1] != curve.index[-1]:
        s = pd.concat([s, curve.iloc[[-1]]])
    return [[d.strftime("%Y-%m-%d"), round(float(v), 2)] for d, v in s.items()]


def build(universe):
    tickers = bt.load_universe(universe)
    px_all = bt.load_prices(bt.load_universe("sp500") + bt.load_universe("qqq")
                            + ["SPY", "QQQ"], START)
    px = px_all[[t for t in tickers if t in px_all.columns]]
    px = px[px.index >= pd.Timestamp(START)]
    target = bt.signal_high52w(px)

    curve, trades = bt.run_backtest(px, target, n_positions=POSITIONS,
                                    exit_band=EXIT_BAND)
    ew = (px.pct_change(fill_method=None).mean(axis=1).fillna(0) + 1).cumprod() * 100_000
    bench = {}
    for b in ("SPY", "QQQ"):
        s = px_all[b].reindex(curve.index).ffill().dropna()
        bench[b] = (s / s.iloc[0]) * 100_000

    series = [
        {"name": "Strategy (top %d, exit %.0f%%)" % (POSITIONS, EXIT_BAND * 100),
         "pts": curve_points(curve)},
        {"name": "Universe equal-weight", "pts": curve_points(ew)},
        {"name": "SPY buy & hold", "pts": curve_points(bench["SPY"])},
        {"name": "QQQ buy & hold", "pts": curve_points(bench["QQQ"])},
    ]
    rows = [bt.metrics(curve, "Strategy (top %d, exit %.0f%%)" % (POSITIONS, EXIT_BAND * 100)),
            bt.metrics(ew, "Universe equal-weight"),
            bt.metrics(bench["SPY"], "SPY buy & hold"),
            bt.metrics(bench["QQQ"], "QQQ buy & hold")]

    sweep = []
    for n in (5, 10, 20):
        for band in (0.10, 0.20, 0.30):
            c, tr = bt.run_backtest(px, target, n_positions=n, exit_band=band)
            m = bt.metrics(c, "")
            sweep.append({"n": n, "band": band, "cagr": m["cagr"], "vol": m["vol"],
                          "sharpe": m["sharpe"], "dd": m["max_dd"], "trades": len(tr),
                          "win": float((tr["ret"] > 0).mean()) if len(tr) else None})

    tr_stats = {
        "n": int(len(trades)),
        "win": float((trades["ret"] > 0).mean()) if len(trades) else None,
        "median_ret": float(trades["ret"].median()) if len(trades) else None,
        "median_days": float(trades["days"].median()) if len(trades) else None,
        "best": [trades.loc[trades["ret"].idxmax(), "ticker"],
                 float(trades["ret"].max())] if len(trades) else None,
        "worst": [trades.loc[trades["ret"].idxmin(), "ticker"],
                  float(trades["ret"].min())] if len(trades) else None,
    }
    return {"universe": universe.upper(), "series": series, "metrics": rows,
            "sweep": sweep, "trades": tr_stats,
            "start": str(curve.index[0].date()), "end": str(curve.index[-1].date()),
            "ew_sharpe": rows[1]["sharpe"]}


def main():
    data = {u: build(u) for u in ("sp500", "qqq")}
    tmpl = (BASE_DIR / "backtest_template.html").read_text()
    OUTPUT_DIR.mkdir(exist_ok=True)
    out = OUTPUT_DIR / "backtest_report.html"
    out.write_text(tmpl.replace("__DATA__", json.dumps(data)))
    print("Wrote", out)


if __name__ == "__main__":
    main()
