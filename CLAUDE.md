# CLAUDE.md

## What this project is

This is an NSE (National Stock Exchange of India) options-chain scraper. On a
schedule during market hours it polls NSE's live option chain for NIFTY,
BANKNIFTY, and NIFTYNXT50, computes implied volatility and a full set of Greeks
from a synchronized spot + option-quote snapshot, and commits the results to
this repo as dated files under `vault/`. The vault is the product: a growing,
version-controlled archive of raw NSE responses plus derived tables, intended
for options research and strategy backtesting rather than live trading.

## Current architecture

The pipeline runs entirely inside GitHub Actions and writes back to the repo.

- **`.github/workflows/fetch-options.yml`** — the trigger. Runs on a cron
  schedule (every 5 min, Mon–Fri, across the UTC window covering 09:15–15:30
  IST) plus a manual `workflow_dispatch`. It checks out the repo, sets up
  Python 3.11, installs `requirements.txt`, runs the fetch script (wrapped in
  `nick-fields/retry@v3` for runner-level blips), then commits and pushes the
  vault changes.
- **`scripts/run_fetch.py`** — the entry point. One run = one fetch cycle across
  all three symbols. It skips cleanly on weekends/holidays, fetches and
  processes each symbol independently (one symbol's failure never blocks the
  others), runs a freshness check before writing, and orchestrates the other
  modules. `fetch_ts` is the script's own clock, logged on every row — the
  nominal cron time is never trusted.
- **`scripts/nse_fetch.py`** — the HTTP layer. A plain `requests`-based client
  for NSE's public JSON endpoints (no `nsepython` dependency), with a warmed-up
  shared session, retry/backoff, the two-step option-chain-v3 expiry-discovery
  fetch, futures parsing, and the index/VIX snapshot.
- **`config/symbols.yaml`** + **`scripts/config_loader.py`** — the **single
  source of truth for symbol config**: which symbols to poll and each one's
  `index_display_name` and `lot_size_fallback`. The loader normalizes names
  (uppercase/strip) and honors an `NSE_SYMBOLS` env override (comma-separated
  subset, wired to the workflow's `workflow_dispatch` input) so a manual run can
  narrow the set without a config edit. Nothing else hardcodes symbols, lot
  sizes, or display names anymore.
- **`scripts/schema.py`** — the **single source of truth for column types**: one
  explicit `pyarrow.schema()` (`VAULT_SCHEMA`) covering every column plus
  `poller_id`, with proper types (tz-aware timestamps, `date32` expiry, `int64`
  quantities, `float64` prices/greeks). Later phases (parquet writer, compactor,
  rebuild script) import it. `schema_matches_csv_columns()` guards it against
  drifting from `vault_io.CSV_COLUMNS`.
- **`scripts/greeks.py`** — Black-Scholes-Merton pricing and Greeks (see hard
  rules below).
- **`scripts/holidays.py`** — manually maintained NSE trading-holiday calendar;
  `is_trading_day()` gates whether a cycle runs at all.
- **`scripts/vault_io.py`** — the only module that touches disk under `vault/`.
  Owns the freshness check, the per-symbol/per-day raw gzip archive
  (`vault/raw/<SYMBOL>/JSON-*.json.gz`), and the per-symbol/per-day MAIN CSV
  tables (`vault/tables/<SYMBOL>/MAIN-*.csv`), including the canonical
  `CSV_COLUMNS` list (kept in sync with `schema.py`).

## Hard rules — do not break these

These guard the parts of the pipeline where a silent change would quietly
corrupt the research data. Treat them as blocking.

1. **Never change `greeks.py`'s sign conventions or its finite-difference
   approach without flagging it explicitly first.** The higher-order Greeks
   (vanna, charm, vomma, speed, zomma, color, veta) are deliberately computed
   via central finite differences of the verified first-order formulas rather
   than hand-transcribed closed forms — a conscious trade to avoid sign/
   convention bugs. The per-day sign logic (negating `∂/∂T` because calendar
   time runs opposite to time-to-expiry), the per-1-percentage-point vega
   scaling, and the bump sizes (`H_SIGMA`, `H_S_PCT`) are all load-bearing. If
   a task seems to require touching any of this, stop and flag it to the user
   with the specific change and its rationale before editing.

2. **Never touch the git commit/push logic in the workflow without preserving
   the pull-rebase retry loop.** Because many cycles run per day, push races
   are expected. The commit step in `fetch-options.yml` uses a
   `for i in 1 2 3; do git pull --rebase && git push && break; ... done` loop
   for exactly this reason. Any edit to that step must keep the pull-rebase +
   retry behavior intact. If you believe it needs to change, flag it first.

## Docs

**There is intentionally no `README.md`.** The old one had drifted out of sync
with the code and was deleted. Project documentation lives in **this file** and
in **`.claude/skills/`** (e.g. `nse-api-quirks`, `vault-schema`). Do **not**
recreate a `README.md` — if one reappears, treat it as stale. Put durable
documentation in this file or a skill instead.

## Phase checklist

Eight phases are planned (full detail in `phase_names.txt`). **Phase 1 is
complete; Phase 2 is next.**

- [x] **Phase 1 — Repo skeleton + config + schema** *(done)*
  `config/symbols.yaml` + `config_loader.py`, `schema.py`, filename format fix
  (`YYYY-MM-DD`), `poller_id` column, portable repo/vault structure.
- [ ] **Phase 2 — Concurrent fetch + speed**
  Parallel symbol fetch, kill the flat 2s sleep, warm session before spawning
  threads.
- [ ] **Phase 3 — Shard-per-cycle parquet storage**
  CSV → parquet, shard-per-cycle writes, idempotent shard naming,
  `.gitattributes` for `*.parquet`, `totalBuyQuantity`/`totalSellQuantity`
  columns.
- [ ] **Phase 4 — Compaction + query helper**
  DuckDB compaction, compaction safety, `scripts/query.py`.
- [ ] **Phase 5 — Data quality layer**
  Put-call parity check, smile monotonicity/convexity check, unit tests for
  `greeks.py`, lookahead-bias discipline.
- [ ] **Phase 6 — Replay-from-raw + versioning**
  `scripts/rebuild_table.py`, `algo_version` column, pinned dependency
  lockfile.
- [ ] **Phase 7 — Multi-expiry + partitioning**
  Tiered fetch policy, expiry-outer/date-inner partitioning, two-tier
  compaction, realized-vol backfill, term-structure/skew summary table.
- [ ] **Phase 8 — Operations, monitoring, resilience**
  Structured logs, per-cycle operational metrics table, circuit breaker,
  gap-checker.

## Tests

```
pytest tests/
```

*(placeholder — the test suite is introduced in Phase 5.)*
