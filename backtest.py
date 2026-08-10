#!/usr/bin/env python3
"""backtest — test the "buy the biggest gap to target, sell near target, rotate" strategy.

Strategy under test
-------------------
  1. Rank the universe by upside = target / price - 1.
  2. Hold the top N names, equal weight.
  3. Sell a name once price rises within EXIT_BAND of its target
     (price >= (1 - EXIT_BAND) * target).
  4. Refill emptied slots from the current top of the ranking. Repeat.

Signal modes
------------
  --signal targets   Point-in-time analyst targets from qqq_targets.db. This is
                     the real strategy, and it is only as long as the history
                     the daily runs have accumulated (days, not years, for now).

  --signal high52w   A point-in-time-safe *proxy*: the "target" is the trailing
                     52-week high. Testable over a decade of real prices, and it
                     keeps the mechanics identical (buy the biggest discount to
                     an anchor, sell on recovery toward it, rotate). It is NOT
                     the analyst-target strategy — it substitutes a different
                     anchor — but it is a real, unbiased backtest of the shape.

Deliberately NOT offered: applying today's analyst targets to historical prices.
Today's consensus already reflects what those prices went on to do, so such a
run reports the future leaking into the past, not a strategy.

Known limitations (both modes)
------------------------------
  * Survivorship bias: the universe is today's index membership, so names that
    were dropped or went to zero are absent. This flatters any long strategy.
  * Prices are split/dividend adjusted closes; fills are modelled at the next
    close after the signal, with a per-trade cost in basis points.
  * No slippage model beyond that, no borrow, no taxes.

Not investment advice. A backtest describes the past under stated assumptions;
it does not predict returns.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
OUTPUT_DIR = BASE_DIR / "output"
DB_PATH = BASE_DIR / "qqq_targets.db"
PRICE_CACHE = CACHE_DIR / "prices.pkl"

TRADING_DAYS = 252


# ---------------------------------------------------------------------------
# Universe & prices
# ---------------------------------------------------------------------------

def load_universe(which):
    """Tickers from the holdings cache written by qqq_targets.py."""
    files = sorted(CACHE_DIR.glob("holdings_%s_*.json" % which))
    if not files:
        sys.exit("No cached holdings for %r — run qqq_targets.py first." % which)
    return [t for t, _name in json.loads(files[-1].read_text())]


def load_prices(tickers, start, refresh=False):
    """Daily adjusted closes, cached on disk (the download is the slow part)."""
    if PRICE_CACHE.exists() and not refresh:
        px = pd.read_pickle(PRICE_CACHE)
        if set(tickers) - set(px.columns) == set() and px.index.min() <= pd.Timestamp(start):
            return px[tickers]
    print("Downloading price history for %d tickers…" % len(tickers), file=sys.stderr)
    px = yf.download(tickers, start=start, progress=False, auto_adjust=True)["Close"]
    if isinstance(px, pd.Series):
        px = px.to_frame()
    px = px.dropna(axis=1, how="all").sort_index()
    CACHE_DIR.mkdir(exist_ok=True)
    px.to_pickle(PRICE_CACHE)
    return px


# ---------------------------------------------------------------------------
# Signals — each returns a DataFrame of targets aligned to the price index
# ---------------------------------------------------------------------------

def signal_high52w(px, window=TRADING_DAYS):
    """Trailing 52-week high. Includes today's bar, which is known at today's
    close, so there is no look-ahead."""
    return px.rolling(window, min_periods=window // 2).max()


def signal_targets(px):
    """Point-in-time analyst targets as recorded by the daily runs. Values are
    held forward between runs, and never back-filled before a ticker's first
    recorded date — a target is only known once it has been observed."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT date, ticker, avg_target FROM targets WHERE avg_target IS NOT NULL"
    ).fetchall()
    conn.close()
    if not rows:
        sys.exit("No stored target history in the database.")
    tgt = (pd.DataFrame(rows, columns=["date", "ticker", "avg_target"])
             .pivot(index="date", columns="ticker", values="avg_target"))
    tgt.index = pd.to_datetime(tgt.index)
    tgt = tgt.reindex(columns=px.columns)
    return tgt.reindex(px.index).ffill()


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

def run_backtest(px, target, n_positions=10, exit_band=0.20, cost_bps=5.0,
                 capital=100_000.0, min_upside=None):
    """Daily loop. Signals are measured at each close and filled at the NEXT
    close, so no decision uses a price it could not have seen."""
    # A name whose upside already sits below the exit threshold would be sold
    # the day after buying it, so it is never a candidate in the first place.
    # exit at price >= (1-band)*target  <=>  upside <= band/(1-band)
    if min_upside is None:
        min_upside = exit_band / (1 - exit_band)
    # Carry the last observed price through gaps (halts, missing bars). Without
    # this a single NaN makes the portfolio total NaN and the equity curve
    # collapses to cash for that day.
    px = px.ffill()
    target = target.ffill()
    upside = target / px - 1.0
    dates = px.index
    cost = cost_bps / 10_000.0

    cash = capital
    shares = {}          # ticker -> share count
    equity_curve, trades = [], []
    entry_info = {}      # ticker -> (entry_date, entry_price)

    # pending orders decided at t-1, executed at t
    pending_sells, pending_buys = [], []

    for i, day in enumerate(dates):
        price_row = px.loc[day]

        # ---- execute what yesterday's close decided -----------------------
        for tkr in pending_sells:
            p = price_row.get(tkr)
            if tkr in shares and pd.notna(p):
                proceeds = shares[tkr] * p
                cash += proceeds * (1 - cost)
                ed, ep = entry_info.pop(tkr, (None, np.nan))
                trades.append({"ticker": tkr, "entry": ed, "exit": day,
                               "entry_px": ep, "exit_px": p,
                               "ret": (p / ep - 1) if ep and pd.notna(ep) else np.nan,
                               "days": (day - ed).days if ed is not None else np.nan})
                del shares[tkr]
        if pending_buys:
            # Size each new slot at 1/N of current equity, capped by cash on
            # hand — not "all remaining cash", which would over-concentrate
            # whenever a single slot reopens.
            mtm = sum(q * price_row.get(t, 0.0) for t, q in shares.items())
            equity_now = cash + (0.0 if pd.isna(mtm) else mtm)
            per_slot = min(equity_now / n_positions, cash / len(pending_buys))
            for tkr in pending_buys:
                p = price_row.get(tkr)
                if pd.isna(p) or p <= 0 or per_slot <= 0:
                    continue
                qty = (per_slot * (1 - cost)) / p
                shares[tkr] = shares.get(tkr, 0) + qty
                cash -= per_slot
                entry_info[tkr] = (day, p)
        pending_sells, pending_buys = [], []

        # ---- mark to market ------------------------------------------------
        holdings_value = float(np.nansum([q * price_row.get(t, np.nan)
                                          for t, q in shares.items()])) if shares else 0.0
        equity = cash + holdings_value
        equity_curve.append(equity)

        if i == len(dates) - 1:
            break

        # ---- decide at this close, execute next close ----------------------
        up_row = upside.loc[day]
        tgt_row = target.loc[day]

        for tkr in list(shares):
            p, tg = price_row.get(tkr), tgt_row.get(tkr)
            if pd.isna(p):
                continue
            # exit when price has closed within exit_band of the target,
            # or the anchor itself has vanished (no current target)
            if pd.isna(tg) or p >= (1 - exit_band) * tg:
                pending_sells.append(tkr)

        open_slots = n_positions - (len(shares) - len(pending_sells))
        if open_slots > 0:
            held = set(shares) - set(pending_sells)
            candidates = (up_row.dropna()
                          .drop(labels=[t for t in held if t in up_row.index],
                                errors="ignore"))
            candidates = candidates[candidates > min_upside]
            # only names with a live price tomorrow are investable
            nxt = px.loc[dates[i + 1]]
            candidates = candidates[[t for t in candidates.index if pd.notna(nxt.get(t))]]
            pending_buys = list(candidates.sort_values(ascending=False)
                                .head(open_slots).index)

    curve = pd.Series(equity_curve, index=dates, name="equity")
    return curve, pd.DataFrame(trades)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def metrics(curve, label):
    ret = curve.pct_change().dropna()
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    total = curve.iloc[-1] / curve.iloc[0] - 1
    cagr = (curve.iloc[-1] / curve.iloc[0]) ** (1 / years) - 1 if years > 0 else np.nan
    vol = ret.std() * np.sqrt(TRADING_DAYS)
    sharpe = (ret.mean() * TRADING_DAYS) / vol if vol else np.nan
    dd = (curve / curve.cummax() - 1).min()
    return {"strategy": label, "total_return": total, "cagr": cagr,
            "vol": vol, "sharpe": sharpe, "max_dd": dd, "years": years}


def buy_and_hold(px, ticker, capital=100_000.0):
    s = px[ticker].dropna()
    return (s / s.iloc[0]) * capital


def fmt_row(m):
    return ("{strategy:<34} {total_return:>11.1%} {cagr:>8.1%} {vol:>8.1%} "
            "{sharpe:>8.2f} {max_dd:>9.1%}").format(**m)


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--signal", choices=["high52w", "targets"], default="high52w")
    ap.add_argument("--universe", choices=["qqq", "sp500"], default="sp500")
    ap.add_argument("--positions", type=int, default=10)
    ap.add_argument("--exit-band", type=float, default=0.20,
                    help="sell once price is within this fraction of target")
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--refresh", action="store_true", help="re-download prices")
    args = ap.parse_args()

    tickers = load_universe(args.universe)
    px = load_prices(tickers + ["SPY", "QQQ"], args.start, refresh=args.refresh)
    bench_px = px[["SPY", "QQQ"]]
    px = px[[t for t in tickers if t in px.columns]]
    px = px[px.index >= pd.Timestamp(args.start)]

    if args.signal == "high52w":
        target = signal_high52w(px)
    else:
        target = signal_targets(px)
        both = target.dropna(how="all").index
        if len(both) < 30:
            print("\n!! Only %d day(s) of stored target history — far too short to "
                  "draw any conclusion from. Reported for mechanics only.\n"
                  % len(both), file=sys.stderr)
        px = px.loc[px.index >= both.min()]
        target = target.loc[px.index]

    curve, trades = run_backtest(px, target, n_positions=args.positions,
                                 exit_band=args.exit_band, cost_bps=args.cost_bps)

    rows = [metrics(curve, "Strategy (%s, top %d)" % (args.signal, args.positions))]
    eq_weight = (px.pct_change().mean(axis=1).fillna(0) + 1).cumprod() * 100_000
    rows.append(metrics(eq_weight, "Universe equal-weight (hold)"))
    for b in ("SPY", "QQQ"):
        s = bench_px[b].reindex(curve.index).dropna()
        if len(s) > 2:
            rows.append(metrics((s / s.iloc[0]) * 100_000, "%s buy & hold" % b))

    print()
    print("%-34s %11s %8s %8s %8s %9s"
          % ("", "Total", "CAGR", "Vol", "Sharpe", "Max DD"))
    print("-" * 82)
    for m in rows:
        print(fmt_row(m))
    print("-" * 82)
    print("Period: %s → %s (%.1f years) · %d positions · exit within %.0f%% of target "
          "· %.0f bps/trade" % (curve.index[0].date(), curve.index[-1].date(),
                                rows[0]["years"], args.positions,
                                args.exit_band * 100, args.cost_bps))

    if not trades.empty:
        wins = (trades["ret"] > 0).mean()
        print("Trades: %d · win rate %.0f%% · median return %.1f%% · median hold %.0f days"
              % (len(trades), wins * 100, trades["ret"].median() * 100,
                 trades["days"].median()))
        print("Best: %s %+.0f%% · Worst: %s %+.0f%%"
              % (trades.loc[trades["ret"].idxmax(), "ticker"], trades["ret"].max() * 100,
                 trades.loc[trades["ret"].idxmin(), "ticker"], trades["ret"].min() * 100))

    OUTPUT_DIR.mkdir(exist_ok=True)
    curve.to_frame("equity").to_csv(OUTPUT_DIR / "backtest_equity.csv")
    if not trades.empty:
        trades.to_csv(OUTPUT_DIR / "backtest_trades.csv", index=False)
    pd.DataFrame(rows).to_csv(OUTPUT_DIR / "backtest_summary.csv", index=False)
    print("\nWrote output/backtest_{equity,trades,summary}.csv")
    print("\nSurvivorship bias: universe is TODAY's index membership. "
          "Not investment advice.")


if __name__ == "__main__":
    main()
