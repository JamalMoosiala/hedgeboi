# NSE Options Vault

Polls NSE's live option chain for NIFTY, BANKNIFTY, and NIFTYNXT50 on a
schedule, computes implied volatility and a full set of Greeks from a
**synchronized** spot + option quote snapshot, and commits the results to
this repo as dated files under `vault/`.

## Why this exists (short version)

NSE's daily bhavcopy reports a last-traded price with no timestamp — for
an illiquid strike, that price might be from a trade hours before close,
compared against a spot price effectively as of close. Computing IV/Greeks
off that mismatched pair produces numbers that look precise but are
disconnected from the real market at any single moment.

This bot instead pulls spot and the full option chain in one call, prefers
the live bid/ask midpoint over last-traded-price when solving for IV, and
timestamps every row with its own fetch clock — not the nominal schedule
time.

## Folder structure

```
vault/
├── raw/
│   ├── NIFTY/JSON-18-08-2026.json.gz
│   ├── BANKNIFTY/JSON-18-08-2026.json.gz
│   └── NIFTYNXT50/JSON-18-08-2026.json.gz
└── tables/
    └── MAIN-18-08-2026.csv
```

- **`vault/raw/<SYMBOL>/JSON-DD-MM-YYYY.json.gz`** — one growing, gzipped
  file per symbol per day. Each fetch cycle appends one more entry (a list
  element) containing that cycle's *untouched* NSE responses (option
  chain, futures, index/VIX snapshot) plus a per-source `fetch_status`.
  This is the future-proofing layer: any field you didn't think to parse
  into the table today is still recoverable from here later.
- **`vault/tables/MAIN-DD-MM-YYYY.csv`** — one growing file per day, **all
  three symbols combined**, one row per strike × expiry × option type ×
  fetch cycle.

Filenames are zero-padded (`18-08-2026`, not `18-8-2026`) for clean
lexicographic sorting.

## Setup

1. Push as a **public** repo (unlimited free Actions minutes; private
   repos cap at 2,000 min/month, though this job is short enough that it's
   unlikely to matter either way).
2. Settings → Actions → General → Workflow permissions → confirm "Read and
   write permissions" is enabled (the commit-back step needs this; recent
   GitHub defaults usually handle it via the `permissions: contents:
   write` block in the workflow, but check if the push step fails).
3. Trigger a manual run first via the Actions tab (`workflow_dispatch`)
   and inspect the resulting files before trusting the schedule — see
   "Before you trust this unattended" below.

## Non-trading day handling

`scripts/holidays.py` skips Saturdays, Sundays, and a manually maintained
list of NSE holidays. **This list needs a yearly update** — no reliable
free API for the NSE trading calendar exists, so add next year's dates by
hand once NSE publishes them (usually a few months before year-end).
Unplanned one-off closures (e.g. an ad-hoc circuit halt) aren't on this
list by design — the freshness check already skips writing on any day NSE
returns unusable data, so it doesn't need separate handling.

## Error handling / robustness, summarized

- Every NSE call retries up to 3 times with exponential backoff before
  giving up (`nse_fetch.py`).
- Each symbol is fetched and processed **independently** — one symbol
  failing (NSE hiccup, unexpected response shape) is logged as a
  `::warning::`/`::error::` annotation and skipped; the other symbols
  still get written.
- The whole script (`run_fetch.py`) itself is wrapped by the GitHub Actions
  workflow in an outer retry (`nick-fields/retry@v3`, 2 attempts) to guard
  against runner-level network blips separate from NSE-side issues.
- Git push uses `pull --rebase` + retry, since multiple runs a day makes a
  push race more likely than in a once-a-day job.
- A bad/empty cycle for a symbol never fails the whole workflow run — it's
  logged clearly (check the Actions run summary for `::warning::`/
  `::error::` annotations) but doesn't block the next cycle 5 minutes later.

## Before you trust this unattended

- **The futures endpoint (`quote-derivative`) and the index-snapshot
  endpoint (`allIndices`) are less battle-tested than the option-chain
  endpoint.** Their exact JSON key names could differ slightly from what's
  coded in `nse_fetch.py` — I wrote the parsing based on documented/
  typical NSE response shapes but couldn't execute a live call against
  nseindia.com to verify field-for-field (network-restricted environment).
  Run a manual `workflow_dispatch` cycle, then check whether `futures_price`,
  `lot_size`, `india_vix`, and `underlying_day_*` columns actually populated
  in the MAIN csv — if they're all null, open the raw JSON archive for that
  symbol and diff the actual response shape against `parse_futures` /
  `parse_index_snapshot` in `nse_fetch.py`.
- **Lot sizes** (`nse_fetch.LOT_SIZE_FALLBACK`) are current as of the
  January 2026 NSE revision (NIFTY 65, BANKNIFTY 30, NIFTYNXT50 25) — these
  get revised periodically by NSE circular, so re-check and update this
  table every so often.

---

## Data dictionary — `vault/tables/MAIN-*.csv`

### Identifiers & timestamps
| Column | Meaning |
|---|---|
| `fetch_ts_utc` / `fetch_ts_ist` | The script's own clock at fetch time — always use this for analysis, not the cron schedule time (cron can drift a few minutes). |
| `symbol` | NIFTY / BANKNIFTY / NIFTYNXT50 |
| `expiry_date` | Contract expiry, as reported by NSE |
| `strike` | Strike price |
| `option_type` | CE or PE |

### Quote data (as fetched)
| Column | Meaning |
|---|---|
| `underlying_value` | Spot, from the **same** response as the option quote (this is what makes it synchronized) |
| `bid_price` / `bid_qty`, `ask_price` / `ask_qty` | Live quote, top of book |
| `ltp` | Last traded price — can be stale on illiquid strikes, kept for reference/fallback only |
| `mid_price` | (bid+ask)/2 — **preferred over LTP** for the IV solve |
| `open_interest` / `change_in_oi` | Standard OI fields |
| `total_traded_volume` | Volume for the day so far |
| `pchange_vs_prev_close` | % change vs previous close, as published directly by NSE |
| `nse_iv` | NSE's own published IV for this strike — compare against `computed_iv` as a sanity check |
| `futures_price` | Same symbol/expiry futures price, used to derive cost-of-carry (see below) |
| `india_vix` | Repeated on every row for easy filtering, though it's really one value per fetch cycle, not per strike |
| `lot_size` | Contract multiplier — from the live futures response if parseable, else the static fallback table |
| `underlying_day_open/high/low/prev_close` | Underlying index's own day OHLC (also repeated per fetch cycle) |

### How IV / Greeks were computed
| Column | Meaning |
|---|---|
| `price_source_for_iv` | `mid_price`, `ltp`, or `none` — which price was actually used to solve for IV |
| `implied_cost_of_carry` | `b = ln(F/S)/T`, derived from the futures price — this is what actually drives the dividend-yield estimate, not a guess |
| `dividend_yield_used` | `q = risk_free_rate − b` — fed into the Black-Scholes formulas |
| `dividend_yield_source` | `futures_implied` (trust this) or `static_fallback` (futures unavailable this cycle — lower confidence) |
| `risk_free_rate_used` | Static assumption (see `run_fetch.py`); only affects discounting once cost-of-carry is futures-implied, so its impact on Delta/IV is small |
| `computed_iv` | Our own solved IV, from `price_source_for_iv` |
| `data_quality_flag` | `ok`, `wide_quote_low_liquidity` (usable price but a wide bid-ask spread — treat with more caution), or `no_price_available` (guardrail tripped: no usable price, or price below intrinsic value — a sign of a stale/bad quote; IV/Greeks left null rather than force-fit) |

### Greeks (all Black-Scholes-Merton, using `dividend_yield_used` above)
| Column | Order | Meaning |
|---|---|---|
| `delta` | 1st | ∂Price/∂S |
| `gamma` | 1st | ∂Delta/∂S |
| `theta` | 1st | ∂Price/∂t, **per calendar day** |
| `vega` | 1st | ∂Price/∂σ, **per 1 percentage point** of IV |
| `vanna` | 2nd | ∂Delta/∂σ (= ∂Vega/∂S) |
| `charm` | 2nd | ∂Delta/∂t, per day — how much rehedging you'll need tomorrow even if nothing else moves |
| `vomma` | 2nd | ∂Vega/∂σ — vega's own convexity |
| `speed` | 3rd | ∂Gamma/∂S |
| `zomma` | 3rd | ∂Gamma/∂σ |
| `color` | 3rd | ∂Gamma/∂t, per day |
| `veta` | 3rd | ∂Vega/∂t, per day |
| `omega` | 1st, normalized | Elasticity/leverage: %ΔPrice / %ΔS |
| `dual_delta` | 1st (w.r.t. K) | ≈ risk-neutral probability of finishing ITM |
| `dual_gamma` | 2nd (w.r.t. K) | Risk-neutral probability density at this strike |

**On the higher-order Greeks (vanna, charm, vomma, speed, zomma, color,
veta):** computed via central finite differences of the verified
first-order formulas, not hand-transcribed closed forms — a deliberate
choice to avoid the sign/convention errors that are easy to introduce
copying third-derivative formulas from memory. See the docstring in
`scripts/greeks.py` for bump sizes and reasoning. These are more sensitive
to input noise than the first-order Greeks (differentiation amplifies
noise) — trust them most on `data_quality_flag = ok` rows, treat them as
close to uninterpretable on `wide_quote_low_liquidity` rows.

**On `time_to_expiry_years`:** computed assuming options stop trading at
15:30 IST on `expiry_date`.
