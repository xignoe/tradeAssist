#!/usr/bin/env python3
"""qqq_targets — daily tables of QQQ and S&P 500 holdings ranked by analyst
price-target upside.

Data sources:
  QQQ holdings:  Invesco daily holdings CSV (primary), Schwab ETF holdings
                 module API (fallback).
  S&P holdings:  stockanalysis.com list endpoint.
  Targets:       TradingView scanner endpoint (the JSON API behind the forecast
                 widget), yfinance analyst_price_targets as a last-resort
                 fallback (only if installed).

Usage: python qqq_targets.py   (cron-able; re-running the same day overwrites
that day's rows)
"""

import asyncio
import base64
import csv
import http.cookiejar
import io
import json
import logging
import random
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "cache"
OUTPUT_DIR = BASE_DIR / "output"
DATA_DIR = BASE_DIR / "data"
DB_PATH = BASE_DIR / "qqq_targets.db"

SNAPSHOT_COLS = ["date", "ticker", "current_price", "avg_target", "max_target",
                 "min_target", "pe_ttm", "pe_fwd", "analysts", "rating"]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CONCURRENCY = 5
MAX_TRIES = 3
REQUEST_TIMEOUT = 20.0
NAME_WIDTH = 28

INVESCO_URL = ("https://www.invesco.com/us/financial-products/etfs/holdings/main/holdings/0"
               "?audienceType=Investor&action=download&ticker=QQQ")
SCHWAB_PAGE_URL = ("https://www.schwab.wallst.com/schwab/Prospect/research/etfs/schwabETF/"
                   "index.asp?type=holdings&symbol=QQQ")
SCHWAB_MODULE_URL = ("https://www.schwab.wallst.com/schwab/Prospect/research/resources/"
                     "server/Module/SchwabETF.ModuleAPI.asp")
STOCKANALYSIS_URL = "https://stockanalysis.com/list/sp-500-stocks/__data.json"
TV_SCANNER_URL = "https://scanner.tradingview.com/symbol"
TV_SCAN_BATCH_URL = "https://scanner.tradingview.com/america/scan"
TV_FIELDS = ("close,price_target_average,price_target_high,price_target_low,"
             "price_earnings_ttm,earnings_per_share_forecast_next_fy,change_abs,"
             "recommendation_total,recommendation_mark")
TV_COLUMNS = ["close", "price_target_average", "price_target_high", "price_target_low",
              # Trailing P/E comes straight from the API; there is no forward-P/E
              # field, so it is derived from next-fiscal-year EPS consensus.
              "price_earnings_ttm", "earnings_per_share_forecast_next_fy",
              # close - change_abs is the prior session's close, used to fill the
              # previous-price column when no stored snapshot covers this ticker.
              "change_abs",
              # How many analysts stand behind the consensus, and where they sit
              # on TradingView's 1 (strong buy) - 5 (strong sell) scale.
              "recommendation_total", "recommendation_mark"]
TV_EXCHANGES = ("NASDAQ", "NYSE", "AMEX", "CBOE")
BATCH_SIZE = 150

log = logging.getLogger("qqq_targets")


def _make_opener():
    cookies = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    opener.addheaders = [("User-Agent", UA)]
    return opener


# ----------------------------------------------------------------------------
# Holdings (each fetcher returns a list of (ticker, company_name))
# ----------------------------------------------------------------------------

def _holdings_from_invesco(opener):
    """Invesco's official daily holdings CSV. Frequently bot-blocked (406)."""
    text = opener.open(INVESCO_URL, timeout=REQUEST_TIMEOUT).read().decode("utf-8", "replace")
    reader = csv.DictReader(io.StringIO(text))
    field = None
    for candidate in ("Holding Ticker", "Ticker", "StockTicker", "Identifier"):
        if reader.fieldnames and candidate in reader.fieldnames:
            field = candidate
            break
    if field is None:
        raise ValueError("Invesco CSV: no ticker column found (fields=%r)" % (reader.fieldnames,))
    name_field = None
    for candidate in ("Name", "Holding Name", "Security Name"):
        if reader.fieldnames and candidate in reader.fieldnames:
            name_field = candidate
            break
    holdings = []
    for row in reader:
        t = (row.get(field) or "").strip()
        if t and re.fullmatch(r"[A-Z][A-Z0-9.]*", t):
            holdings.append((t, (row.get(name_field) or "").strip() if name_field else ""))
    if len(holdings) < 50:
        raise ValueError("Invesco CSV: only %d tickers parsed" % len(holdings))
    return holdings


def _holdings_from_schwab(opener):
    """Schwab wallst holdings widget: GET page for a session token, then POST its
    module API once with a large numRows to get every holding as JSON.

    Uses urllib rather than httpx: the wallst ASP backend 500s on httpx's request
    normalization but accepts stdlib requests unchanged."""
    html = opener.open(SCHWAB_PAGE_URL, timeout=REQUEST_TIMEOUT).read().decode("utf-8", "replace")
    token_m = re.search(r"WSOD_DATA\.sessionID = '(YYY101[^']+)'", html)
    issue_m = re.search(r"gSymbolWSODIssue *= *'(\d+)'", html)
    if not token_m or not issue_m:
        raise ValueError("Schwab page: session token or wsodissue not found")
    module_args = {
        "module": "schwabETFHoldingsTable",
        "moduleArgs": {
            "ModuleID": "holdingsTableContainer",
            "symbol": "QQQ",
            "wsodissue": issue_m.group(1),
            "sortDir": "desc",
            "sortBy": "PctNetAssets",
            "page": "1",
            "numRows": 300,
            "isThirdPartyETF": True,
        },
    }
    b64 = base64.b64encode(json.dumps(module_args, separators=(",", ":")).encode()).decode()
    body = urllib.parse.urlencode({
        "inputs": "B64ENC" + b64,
        "..contenttype..": "text/javascript",
        "..requester..": "ContentBuffer",
    }).encode()
    # The signed session token must stay raw in the query string.
    req = urllib.request.Request(
        SCHWAB_MODULE_URL + "?" + token_m.group(1), data=body,
        headers={"Referer": SCHWAB_PAGE_URL,
                 "Content-Type": "application/x-www-form-urlencoded"})
    text = opener.open(req, timeout=REQUEST_TIMEOUT).read().decode("utf-8", "replace")
    holdings = re.findall(
        r'"symbol firstColumn","tsraw":"([A-Za-z.]+)".*?"description","tsraw":"([^"]*)"',
        text, re.S)
    if len(holdings) < 50:
        raise ValueError("Schwab module: only %d tickers parsed" % len(holdings))
    return [(t.upper(), n) for t, n in holdings]


def _holdings_from_stockanalysis(opener):
    """S&P 500 constituents from stockanalysis.com's SvelteKit data endpoint.
    The payload is devalue-encoded: a flat array where dict/list values are
    indices into the same array."""
    raw = opener.open(STOCKANALYSIS_URL, timeout=REQUEST_TIMEOUT).read().decode("utf-8", "replace")
    doc = json.loads(raw)

    for node in doc.get("nodes") or []:
        if not node or node.get("type") != "data":
            continue
        data = node["data"]
        root = data[0]
        if not isinstance(root, dict) or "stockData" not in root:
            continue

        def resolve(i):
            v = data[i]
            if isinstance(v, dict):
                return {k: resolve(ix) for k, ix in v.items()}
            if isinstance(v, list):
                return [resolve(ix) for ix in v]
            return v

        rows = resolve(root["stockData"])
        holdings = [((r.get("s") or "").strip().upper(), (r.get("n") or "").strip())
                    for r in rows if r.get("s")]
        if len(holdings) < 400:
            raise ValueError("stockanalysis: only %d tickers parsed" % len(holdings))
        return holdings
    raise ValueError("stockanalysis: stockData node not found")


def _fetch_qqq(opener):
    try:
        holdings = _holdings_from_invesco(opener)
        log.info("QQQ holdings: %d tickers from Invesco CSV", len(holdings))
    except Exception as exc:
        log.warning("Invesco holdings failed (%s); falling back to Schwab", exc)
        holdings = _holdings_from_schwab(opener)
        log.info("QQQ holdings: %d tickers from Schwab", len(holdings))
    return holdings


def _fetch_sp500(opener):
    holdings = _holdings_from_stockanalysis(opener)
    log.info("S&P 500 holdings: %d tickers from stockanalysis.com", len(holdings))
    return holdings


UNIVERSES = [
    ("qqq", "QQQ", _fetch_qqq),
    ("sp500", "S&P 500", _fetch_sp500),
]


def get_holdings(universe, fetcher, today):
    """Return [(ticker, name), ...], cached on disk, refreshed at most once a day."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / ("holdings_%s_%s.json" % (universe, today.isoformat()))
    if cache_file.exists():
        holdings = [tuple(x) for x in json.loads(cache_file.read_text())]
        log.info("%s holdings: %d tickers from cache (%s)",
                 universe, len(holdings), cache_file.name)
        return holdings

    holdings = fetcher(_make_opener())
    cache_file.write_text(json.dumps(holdings))
    for old in CACHE_DIR.glob("holdings_%s_*.json" % universe):
        if old != cache_file:
            old.unlink()
    return holdings


# ----------------------------------------------------------------------------
# Price targets — batch scan (primary path)
# ----------------------------------------------------------------------------

def _pos(v):
    """A P/E is only meaningful on positive earnings; negatives read as N/A."""
    return float(v) if v is not None and v > 0 else None


def _fwd_pe(close, fwd_eps):
    """Forward P/E = price / next-fiscal-year consensus EPS."""
    if close is None or fwd_eps is None or fwd_eps <= 0:
        return None
    return float(close) / float(fwd_eps)


def _batch_scan(client, prefixed):
    """One /america/scan POST for up to BATCH_SIZE 'EXCHANGE:TICKER' symbols.
    Returns {'EXCHANGE:TICKER': [close, avg, high, low]}; unknown symbols are
    simply omitted from the response."""
    for attempt in range(1, MAX_TRIES + 1):
        resp = client.post(
            TV_SCAN_BATCH_URL,
            json={"symbols": {"tickers": prefixed}, "columns": TV_COLUMNS},
            headers={"User-Agent": UA, "Referer": "https://www.tradingview.com/"},
        )
        if resp.status_code == 200:
            return {row["s"]: row["d"] for row in resp.json().get("data") or []}
        if attempt == MAX_TRIES:
            resp.raise_for_status()
        try:
            wait = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            wait = 0.0
        if wait <= 0:
            # 429s need patience, not a 2-second nudge.
            wait = 20.0 * attempt + random.uniform(0, 5)
        log.info("batch scan: HTTP %d, retrying in %.0fs (attempt %d/%d)",
                 resp.status_code, wait, attempt, MAX_TRIES)
        time.sleep(wait)


def fetch_all_targets_batch(tickers):
    """Fetch all tickers via the batch scan endpoint: one POST per ~150 symbols,
    with misses re-tried on the next exchange. ~5-6 requests for ~520 tickers."""
    out = {t: None for t in tickers}
    remaining = list(tickers)
    with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
        for exchange in TV_EXCHANGES:
            if not remaining:
                break
            found = {}
            prefixed = ["%s:%s" % (exchange, t.replace("/", ".")) for t in remaining]
            for i in range(0, len(prefixed), BATCH_SIZE):
                found.update(_batch_scan(client, prefixed[i:i + BATCH_SIZE]))
                time.sleep(random.uniform(0.5, 1.5))
            still = []
            for t in remaining:
                key = "%s:%s" % (exchange, t.replace("/", "."))
                if key not in found:
                    still.append(t)
                    continue
                (close, avg, high, low, pe_ttm, fwd_eps, chg_abs,
                 n_analysts, rating) = found[key]
                if close is None or avg is None:
                    log.warning("%s: no price target data", t)
                    continue  # resolved on this exchange, just no coverage
                out[t] = {"current": float(close), "avg": float(avg),
                          "high": float(high) if high is not None else None,
                          "low": float(low) if low is not None else None,
                          "pe_ttm": _pos(pe_ttm),
                          "pe_fwd": _fwd_pe(close, fwd_eps),
                          "prev_close": (float(close) - float(chg_abs)
                                         if chg_abs is not None else None),
                          "analysts": int(n_analysts) if n_analysts else None,
                          "rating": float(rating) if rating is not None else None}
            remaining = still
    for t in remaining:
        log.warning("%s: not found on %s", t, "/".join(TV_EXCHANGES))
    return out


# ----------------------------------------------------------------------------
# Price targets — per-symbol endpoint (fallback path)
# ----------------------------------------------------------------------------

async def _tv_fetch_symbol(client, exchange, ticker):
    """One scanner request. Returns a dict or None (None = wrong exchange / no data)."""
    resp = await client.get(
        TV_SCANNER_URL,
        params={"symbol": "%s:%s" % (exchange, ticker),
                "fields": TV_FIELDS, "no_404": "true"},
        headers={"User-Agent": UA, "Referer": "https://www.tradingview.com/"},
    )
    if resp.status_code == 429:
        raise httpx.HTTPStatusError("rate limited", request=resp.request, response=resp)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        return None
    return data


async def fetch_targets(client, sem, ticker):
    """Fetch (current, avg, high, low) for one ticker, trying NASDAQ then NYSE then AMEX.
    Returns (ticker, dict-or-None)."""
    async with sem:
        await asyncio.sleep(random.uniform(0.1, 0.6))
        tv_ticker = ticker.replace("/", ".")
        for attempt in range(1, MAX_TRIES + 1):
            try:
                data = None
                for exchange in ("NASDAQ", "NYSE", "AMEX", "CBOE"):
                    data = await _tv_fetch_symbol(client, exchange, tv_ticker)
                    if data is not None:
                        break
                if data is None:
                    log.warning("%s: not found on NASDAQ/NYSE/AMEX/CBOE", ticker)
                    return ticker, None
                current = data.get("close")
                avg = data.get("price_target_average")
                if current is None or avg is None:
                    log.warning("%s: no price target data", ticker)
                    return ticker, None
                return ticker, {
                    "current": float(current),
                    "avg": float(avg),
                    "high": (float(data["price_target_high"])
                             if data.get("price_target_high") is not None else None),
                    "low": (float(data["price_target_low"])
                            if data.get("price_target_low") is not None else None),
                    "pe_ttm": _pos(data.get("price_earnings_ttm")),
                    "pe_fwd": _fwd_pe(current,
                                      data.get("earnings_per_share_forecast_next_fy")),
                    "prev_close": (float(current) - float(data["change_abs"])
                                   if data.get("change_abs") is not None else None),
                }
            except Exception as exc:
                if attempt == MAX_TRIES:
                    log.warning("%s: failed after %d tries (%s)", ticker, MAX_TRIES, exc)
                    return ticker, None
                rate_limited = (isinstance(exc, httpx.HTTPStatusError)
                                and exc.response.status_code == 429)
                backoff = ((20.0 * attempt + random.uniform(0, 5)) if rate_limited
                           else (2 ** attempt) + random.uniform(0, 1))
                log.info("%s: attempt %d failed (%s), retrying in %.1fs",
                         ticker, attempt, exc, backoff)
                await asyncio.sleep(backoff)


async def fetch_all_targets(tickers):
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        results = await asyncio.gather(
            *(fetch_targets(client, sem, t) for t in tickers))
    return dict(results)


def fetch_targets_yfinance(tickers):
    """Last-resort fallback: same data via yfinance (only used if TradingView
    returned nothing at all and yfinance is installed)."""
    try:
        import yfinance as yf
    except ImportError:
        log.error("yfinance not installed; cannot fall back")
        return {}
    out = {}
    for t in tickers:
        try:
            pt = yf.Ticker(t).analyst_price_targets
            if pt and pt.get("mean") is not None:
                out[t] = {"current": pt.get("current"), "avg": pt.get("mean"),
                          "high": pt.get("high"), "low": pt.get("low")}
            else:
                out[t] = None
        except Exception as exc:
            log.warning("%s: yfinance failed (%s)", t, exc)
            out[t] = None
    return out


# ----------------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------------

def open_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS targets (
            date          TEXT NOT NULL,
            ticker        TEXT NOT NULL,
            current_price REAL,
            avg_target    REAL,
            max_target    REAL,
            min_target    REAL,
            PRIMARY KEY (date, ticker)
        )""")
    # Additive migration for databases created before the P/E columns existed.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(targets)")}
    for col in ("pe_ttm", "pe_fwd", "analysts", "rating"):
        if col not in existing:
            conn.execute("ALTER TABLE targets ADD COLUMN %s REAL" % col)
    conn.commit()
    return conn


def save_today(conn, today, results):
    """Upsert this run's successful tickers. Deliberately does NOT clear the
    day's other rows first: a degraded run (rate limit, network blip) would
    otherwise destroy good rows written earlier the same day, and analyst
    targets can't be re-fetched for a past date. The (date, ticker) primary key
    keeps re-runs idempotent per ticker."""
    rows = [(today.isoformat(), t, d["current"], d["avg"], d["high"], d["low"],
             d.get("pe_ttm"), d.get("pe_fwd"), d.get("analysts"), d.get("rating"))
            for t, d in results.items() if d is not None]
    with conn:
        conn.executemany(
            """INSERT OR REPLACE INTO targets
               (date, ticker, current_price, avg_target, max_target, min_target,
                pe_ttm, pe_fwd, analysts, rating)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
    return len(rows)


def write_snapshot(conn, today):
    """Mirror the day's stored rows to data/YYYY-MM-DD.csv.

    The SQLite file is a derived cache; these text snapshots are the durable
    record. They diff well in git, so the scheduled cloud run can commit each
    day's data back to the repo and any checkout can rebuild full history."""
    DATA_DIR.mkdir(exist_ok=True)
    rows = conn.execute(
        "SELECT %s FROM targets WHERE date = ? ORDER BY ticker" % ", ".join(SNAPSHOT_COLS),
        (today.isoformat(),)).fetchall()
    if not rows:
        return None
    path = DATA_DIR / ("%s.csv" % today.isoformat())
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SNAPSHOT_COLS)
        w.writerows(rows)
    return path


def import_snapshots(conn):
    """Load any data/*.csv days the database doesn't have yet. This is how a
    fresh clone — or a laptop that missed the days the cloud run captured —
    catches up to the full history."""
    if not DATA_DIR.exists():
        return 0
    have = {r[0] for r in conn.execute("SELECT DISTINCT date FROM targets")}
    imported = 0
    for path in sorted(DATA_DIR.glob("*.csv")):
        if path.stem in have:
            continue
        with open(path) as f:
            rows = [[r.get(c) or None for c in SNAPSHOT_COLS] for r in csv.DictReader(f)]
        if rows:
            with conn:
                conn.executemany(
                    "INSERT OR REPLACE INTO targets (%s) VALUES (%s)"
                    % (", ".join(SNAPSHOT_COLS), ", ".join("?" * len(SNAPSHOT_COLS))),
                    rows)
            imported += 1
    if imported:
        log.info("Imported %d day(s) of history from data/", imported)
    return imported


def load_history(conn, tickers, days=400):
    """Per-ticker series for the drill-down chart:
    {ticker: [[date, upside_pct, price, avg_target], ...]} oldest first."""
    placeholders = ",".join("?" * len(tickers))
    rows = conn.execute(
        """SELECT ticker, date, current_price, avg_target FROM targets
           WHERE ticker IN (%s) AND current_price > 0 AND avg_target IS NOT NULL
             AND date >= date('now', ?)
           ORDER BY ticker, date""" % placeholders,
        list(tickers) + ["-%d days" % days]).fetchall()
    hist = {}
    for ticker, day, price, target in rows:
        hist.setdefault(ticker, []).append(
            [day, round((target / price - 1) * 100, 2), round(price, 2), round(target, 2)])
    return hist


def load_previous(conn, today, tickers):
    """Most recent prior-date row per ticker: {ticker: (current_price, avg_target)}."""
    placeholders = ",".join("?" * len(tickers))
    rows = conn.execute(
        """SELECT t.ticker, t.current_price, t.avg_target
           FROM targets t
           JOIN (SELECT ticker, MAX(date) AS d FROM targets
                 WHERE date < ? AND ticker IN (%s) GROUP BY ticker) latest
             ON latest.ticker = t.ticker AND latest.d = t.date""" % placeholders,
        [today.isoformat()] + list(tickers)).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------

def pct(target, current):
    if target is None or not current:
        return None
    return (target / current - 1.0) * 100.0


def fmt_pct(v, signed=True):
    if v is None:
        return "N/A"
    return ("%+.1f%%" if signed else "%.1f%%") % v


def build_rows(holdings, results, previous):
    rows = []
    for t, name in holdings:
        d = results.get(t)
        cur = d["current"] if d else None
        avg_pct = pct(d["avg"], cur) if d else None
        max_pct = pct(d["high"], cur) if d else None
        min_pct = pct(d["low"], cur) if d else None
        # Preferred basis: the stored snapshot, which pairs the prior price with
        # the prior *target*, so the change reflects both. When no snapshot covers
        # this ticker, fall back to the prior session's close: the price is exact,
        # but the prior target is unknowable (no source publishes historical
        # consensus targets), so the derived figures assume the target was
        # unchanged and isolate the price effect. Those are flagged estimated.
        y_price = y_avg_pct = y_target = None
        estimated = False
        if t in previous:
            y_cur, y_avg = previous[t]
            y_price, y_target = y_cur, y_avg
            y_avg_pct = pct(y_avg, y_cur)
        elif d and d.get("prev_close"):
            y_price = d["prev_close"]
            y_target = d["avg"]          # assumed unchanged — see above
            y_avg_pct = pct(d["avg"], y_price)
            estimated = True
        change_pp = (avg_pct - y_avg_pct
                     if avg_pct is not None and y_avg_pct is not None else None)

        # Split the move into the two things that cause it. Upside U = T/P - 1, so
        #   price effect  = T0/P1 - T0/P0   (target held at yesterday's)
        #   target effect = (T1 - T0)/P1    (price held at today's)
        # These sum exactly to the total change. Only the target effect is news;
        # the price effect is something the ticker tape already told you.
        chg_price_pp = chg_target_pp = None
        if (change_pp is not None and y_price and cur and y_target is not None):
            chg_price_pp = (y_target / cur - y_target / y_price) * 100.0
            chg_target_pp = ((d["avg"] - y_target) / cur) * 100.0
        rows.append({"ticker": t, "name": name, "current": cur,
                     "pe_ttm": d.get("pe_ttm") if d else None,
                     "pe_fwd": d.get("pe_fwd") if d else None,
                     "avg_target": d["avg"] if d else None, "avg_pct": avg_pct,
                     "max_pct": max_pct, "min_pct": min_pct,
                     "y_price": y_price, "y_avg_pct": y_avg_pct,
                     "change_pp": change_pp, "chg_price_pp": chg_price_pp,
                     "chg_target_pp": chg_target_pp, "estimated": estimated,
                     "analysts": d.get("analysts") if d else None,
                     "rating": d.get("rating") if d else None})
    rows.sort(key=lambda r: (r["avg_pct"] is None,
                             -(r["avg_pct"] if r["avg_pct"] is not None else 0)))
    return rows


HEADERS = ["Ticker", "Name", "An", "Price", "Yday Price", "P/E", "Fwd P/E",
           "Avg Tgt", "Avg Tgt %", "Max Tgt %", "Min Tgt %", "Yday Avg %",
           "Chg (pp)", "Tgt Δ"]
LEFT_ALIGNED = {0, 1}  # Ticker and Name columns


def _est(text, estimated):
    """Mark a value derived without a stored snapshot, so it never reads as
    recorded history."""
    return ("~" + text) if (estimated and text != "N/A") else text


def _clip(name):
    if len(name) <= NAME_WIDTH:
        return name
    return name[:NAME_WIDTH - 1] + "…"


def print_table(rows):
    table = [[r["ticker"], _clip(r["name"]),
              "%d" % r["analysts"] if r["analysts"] else "—",
              "%.2f" % r["current"] if r["current"] is not None else "N/A",
              "%.2f" % r["y_price"] if r["y_price"] is not None else "N/A",
              "%.1f" % r["pe_ttm"] if r["pe_ttm"] is not None else "N/A",
              "%.1f" % r["pe_fwd"] if r["pe_fwd"] is not None else "N/A",
              "%.2f" % r["avg_target"] if r["avg_target"] is not None else "N/A",
              fmt_pct(r["avg_pct"]), fmt_pct(r["max_pct"]), fmt_pct(r["min_pct"]),
              _est(fmt_pct(r["y_avg_pct"]), r["estimated"]),
              _est("%+.2f" % r["change_pp"] if r["change_pp"] is not None else "N/A",
                   r["estimated"]),
              "%+.2f" % r["chg_target_pp"] if r["chg_target_pp"] is not None else "N/A"]
             for r in rows]
    widths = [max(len(HEADERS[i]), max((len(row[i]) for row in table), default=0))
              for i in range(len(HEADERS))]
    def line(cells):
        return "  ".join(c.ljust(w) if i in LEFT_ALIGNED else c.rjust(w)
                         for i, (c, w) in enumerate(zip(cells, widths))).rstrip()
    print(line(HEADERS))
    print(line(["-" * w for w in widths]))
    for row in table:
        print(line(row))


def write_csv(rows, universe, today):
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / ("%s_%s.csv" % (universe, today.isoformat()))
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "company_name", "analysts", "consensus_rating",
                    "current_price", "yesterday_price",
                    "pe_ttm", "pe_forward", "avg_target", "avg_target_pct",
                    "max_target_pct", "min_target_pct", "yesterday_avg_target_pct",
                    "change_pp", "change_pp_from_price", "change_pp_from_target",
                    "prev_basis"])
        for r in rows:
            w.writerow([r["ticker"], r["name"], r["analysts"] or "",
                        round(r["rating"], 2) if r["rating"] is not None else "",
                        round(r["current"], 4) if r["current"] is not None else "",
                        round(r["y_price"], 4) if r["y_price"] is not None else "",
                        round(r["pe_ttm"], 2) if r["pe_ttm"] is not None else "",
                        round(r["pe_fwd"], 2) if r["pe_fwd"] is not None else "",
                        round(r["avg_target"], 4) if r["avg_target"] is not None else "",
                        round(r["avg_pct"], 2) if r["avg_pct"] is not None else "",
                        round(r["max_pct"], 2) if r["max_pct"] is not None else "",
                        round(r["min_pct"], 2) if r["min_pct"] is not None else "",
                        round(r["y_avg_pct"], 2) if r["y_avg_pct"] is not None else "",
                        round(r["change_pp"], 2) if r["change_pp"] is not None else "",
                        round(r["chg_price_pp"], 2) if r["chg_price_pp"] is not None else "",
                        round(r["chg_target_pp"], 2) if r["chg_target_pp"] is not None else "",
                        ("" if r["y_price"] is None else
                         "prior-session close (target assumed unchanged)"
                         if r["estimated"] else "stored snapshot")])
    return path


def write_report(report_universes, today, history=None):
    """Render output/report.html from report_template.html with the day's data
    embedded — a self-contained page, refreshed on every run."""
    template_path = BASE_DIR / "report_template.html"
    if not template_path.exists():
        log.warning("report_template.html missing; skipping HTML report")
        return None
    payload = {
        "date": today.isoformat(),
        "history": history or {},
        "universes": {
            key: {"label": label,
                  "rows": [{"t": r["ticker"], "n": r["name"],
                            "price": r["current"], "y_price": r["y_price"],
                            "avg_t": r["avg_target"],
                            "pe": _round(r["pe_ttm"], 1),
                            "fpe": _round(r["pe_fwd"], 1),
                            "avg": _round(r["avg_pct"]), "mx": _round(r["max_pct"]),
                            "mn": _round(r["min_pct"]), "y": _round(r["y_avg_pct"]),
                            "chg": _round(r["change_pp"]),
                            "chg_p": _round(r["chg_price_pp"]),
                            "chg_t": _round(r["chg_target_pp"]),
                            "an": r["analysts"], "rat": _round(r["rating"], 2),
                            "est": r["estimated"]} for r in rows]}
            for key, (label, rows) in report_universes.items()
        },
    }
    html = (template_path.read_text()
            .replace("__DATA__", json.dumps(payload))
            .replace("__DATE__", today.isoformat())
            .replace("__GENERATED__", time.strftime("%Y-%m-%d %H:%M")))
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / "report.html"
    path.write_text(html)
    return path


def _round(v, nd=2):
    return None if v is None else round(v, nd)


def print_summary(rows):
    top_upside = [r for r in rows if r["avg_pct"] is not None][:5]
    movers = sorted((r for r in rows if r["change_pp"] is not None and r["change_pp"] > 0),
                    key=lambda r: -r["change_pp"])[:5]
    print()
    print("Top 5 upside:    " + ", ".join(
        "%s %s" % (r["ticker"], fmt_pct(r["avg_pct"])) for r in top_upside))
    if movers:
        print("Top 5 movers up: " + ", ".join(
            "%s %+.2fpp" % (r["ticker"], r["change_pp"]) for r in movers))
    else:
        print("Top 5 movers up: N/A (no prior-day data)")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(message)s")
    today = date.today()

    universes = {}  # key -> (label, holdings)
    for key, label, fetcher in UNIVERSES:
        try:
            universes[key] = (label, get_holdings(key, fetcher, today))
        except Exception as exc:
            log.error("%s holdings failed entirely (%s); skipping this universe",
                      label, exc)

    if not universes:
        log.error("No holdings could be fetched; nothing to do")
        sys.exit(1)

    # One fetch per unique ticker across all universes.
    all_tickers = []
    seen = set()
    for _, holdings in universes.values():
        for t, _name in holdings:
            if t not in seen:
                seen.add(t)
                all_tickers.append(t)

    try:
        results = fetch_all_targets_batch(all_tickers)
    except Exception as exc:
        log.warning("Batch scan failed (%s); falling back to per-symbol requests", exc)
        results = asyncio.run(fetch_all_targets(all_tickers))
    ok = sum(1 for v in results.values() if v is not None)
    log.info("Targets: %d/%d tickers fetched from TradingView", ok, len(all_tickers))
    if ok == 0:
        log.warning("TradingView returned nothing; trying yfinance fallback")
        results = fetch_targets_yfinance(all_tickers)
        ok = sum(1 for v in results.values() if v is not None)
        log.info("Targets: %d/%d tickers fetched from yfinance", ok, len(all_tickers))
    if ok < len(all_tickers) * 0.9:
        log.warning("DEGRADED RUN: only %d/%d tickers fetched — today's stored "
                    "snapshot is incomplete, which will leave gaps in tomorrow's "
                    "day-over-day columns. Consider re-running once the source "
                    "recovers.", ok, len(all_tickers))

    conn = open_db()
    try:
        import_snapshots(conn)
        previous = load_previous(conn, today, all_tickers)
        saved = save_today(conn, today, results)
        log.info("Saved %d rows for %s (prior data for %d tickers)",
                 saved, today, len(previous))
        snap = write_snapshot(conn, today)
        if snap:
            log.info("Snapshot written to %s", snap)
        history = load_history(conn, all_tickers)
    finally:
        conn.close()

    report_universes = {}
    for key, (label, holdings) in universes.items():
        rows = build_rows(holdings, results, previous)
        report_universes[key] = (label, rows)
        print()
        print("=" * 30, label, "=" * 30)
        print_table(rows)
        print_summary(rows)
        csv_path = write_csv(rows, key, today)
        log.info("%s CSV written to %s", label, csv_path)

    report_path = write_report(report_universes, today, history)
    if report_path:
        log.info("HTML report written to %s", report_path)


if __name__ == "__main__":
    main()
