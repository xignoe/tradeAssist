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

## Scheduling

Runs automatically at 6:30pm Mon-Fri via a launchd LaunchAgent
(`~/Library/LaunchAgents/com.user.qqq-targets.plist`), logging to
`cron.log`. Unlike cron, launchd runs the job on wake if the Mac was asleep
at the scheduled time.

Note: the project must live outside `~/Documents`/`~/Desktop`/`~/Downloads` —
macOS blocks background jobs (launchd/cron) from reading those folders.

```bash
# check it's loaded
launchctl list | grep qqq-targets

# trigger a run right now
launchctl kickstart gui/$(id -u)/com.user.qqq-targets

# uninstall
launchctl bootout gui/$(id -u)/com.user.qqq-targets && rm ~/Library/LaunchAgents/com.user.qqq-targets.plist
```

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
