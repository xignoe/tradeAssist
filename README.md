# qqq_targets

Daily tables of QQQ **and S&P 500** holdings ranked by analyst price-target
upside, with day-over-day change tracking.

## Run

```bash
.venv/bin/python qqq_targets.py
```

(or any Python 3.9+ with `httpx` installed: `pip install -r requirements.txt`)

Prints one aligned console table per universe (QQQ, then S&P 500), each sorted
by average-target upside, and writes `output/qqq_YYYY-MM-DD.csv` and
`output/sp500_YYYY-MM-DD.csv`. Re-running on the same day overwrites that
day's rows. The "yesterday" columns populate from the second daily run onward.

Each run also regenerates **`output/report.html`** (from
`report_template.html`) — a self-contained interactive page: KPI tiles, a
top-20 upside chart with min–max analyst ranges, a day-over-day movers chart
(appears once two dates of history exist), and a sortable table of every
holding, with a QQQ / S&P 500 toggle and light/dark theme. Open it in any
browser:

```bash
open output/report.html
```

Takes ~10 seconds (~520 unique tickers; overlapping tickers are fetched once).

## Scheduling — runs in the cloud, nothing to leave switched on

The daily collection runs on **GitHub Actions** (`.github/workflows/daily.yml`)
at 23:30 UTC on weekdays — 7:30pm EDT / 6:30pm EST, after the US close. Your
Mac can be asleep, closed, or off.

**Live report:** https://xignoe.github.io/tradeAssist/ (republished every run)

Each run commits that day's rows to `data/YYYY-MM-DD.csv`. Those text
snapshots are the durable record; `qqq_targets.db` is a cache rebuilt from
them, so a fresh clone recovers full history:

```bash
git pull        # fetch days collected in the cloud
python qqq_targets.py   # imports any missing days, then adds today
```

Trigger an off-schedule run without touching your machine:

```bash
gh workflow run "Daily targets"
```

Weekends and US market holidays have no rows by design — markets are closed,
so there is no new data to record.

### Old local scheduler (removed)

A launchd LaunchAgent used to run this on the Mac. It was removed: it only
fired if the machine happened to be awake at the scheduled time, and it
silently missed 2026-08-11 when the Mac slept. Missed days are unrecoverable,
because analyst consensus targets cannot be fetched for a past date.

## How it works

- **QQQ holdings**: tries Invesco's official QQQ holdings CSV first (usually
  bot-blocked at the CDN), then falls back to the Schwab wallst holdings
  widget — one GET for a signed session token, one POST to its
  `SchwabETF.ModuleAPI.asp` returns all ~101 tickers + company names as JSON.
- **S&P 500 holdings**: stockanalysis.com's SvelteKit data endpoint
  (`/list/sp-500-stocks/__data.json`), devalue-decoded to ~502 tickers +
  company names.
- Both lists are cached in `cache/holdings_<universe>_YYYY-MM-DD.json`,
  refreshed at most once per day. If one universe's holdings fetch fails
  entirely, the other still runs.
- **Price targets**: TradingView's batch scan endpoint
  (`POST https://scanner.tradingview.com/america/scan`) — ~150 symbols per
  request, so a full run is only ~5-6 requests and stays far under the rate
  limit. Exchange resolution tries NASDAQ → NYSE → AMEX → CBOE (misses are
  omitted from the response and re-tried on the next exchange). 429s honor
  Retry-After and back off 20-65s. If the batch endpoint fails entirely, the
  run falls back to per-symbol requests (async, concurrency 5, jitter,
  3-try backoff), and to `yfinance` after that (only if installed).
- **Persistence**: SQLite `qqq_targets.db`, table
  `targets(date, ticker, current_price, avg_target, max_target, min_target)`
  with `PRIMARY KEY (date, ticker)` — one row per unique ticker per day,
  shared across universes. "Yesterday" = the most recent prior date in the db
  per ticker; yesterday's upside is computed against yesterday's stored price.

A ticker with no data (delisted, or no analyst coverage — e.g. L, ERIE) is
logged and shown as N/A — it never crashes the run.
