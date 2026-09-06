---
name: nse-api-quirks
description: Hard-won facts about which NSE option-chain endpoints actually work, which are dead, and the two-step fetch pattern this project relies on. Read before touching nse_fetch.py or debugging empty/404 NSE responses.
---

# NSE API quirks

Everything here was confirmed by live debugging (see the docstrings in
`scripts/nse_fetch.py`). NSE's public endpoints are undocumented and change
without notice, so treat these as observed behavior, not guarantees.

## Endpoint status

- **`option-chain-indices` (classic) — DEAD.** Returns an empty `{}` even when
  hit directly, from **both** a GitHub Actions cloud runner **and** a
  residential IP. This is almost certainly retired on NSE's side, not an IP
  block. Do not reintroduce it.
- **`quote-derivative` — DEAD.** Genuine HTTP 404 "Resource not found" on every
  URL shape tried (bare symbol, symbol+identifier, identifier only). It used to
  be the source for futures prices and lot size. Both now come from elsewhere
  (see below).
- **`NextApi/apiClient/GetQuoteApi` (functionName=`getSymbolDerivativesData`) —
  WORKS.** Returns real, live, full option-chain data. This is what
  `fetch_option_chain()` calls. (The `nsepython`/`nsepythonserver` library wraps
  this same endpoint internally but its reshaping logic returned `{}` regardless
  — a library bug, not an NSE/IP problem. That's why this project uses plain
  `requests` instead.)
- **`option-chain-v3` — WORKS, but requires an explicit `&expiry=` param.**
  Omitting the expiry returns HTTP 200 with completely empty `records` (no data,
  no metadata). This is the only endpoint that carries real bid/ask and NSE's
  own published IV.
- **`allIndices` — WORKS.** Reliable from both cloud and residential IPs.
  Source for index day OHLC, India VIX, and the published dividend yield (`dy`).

## Why plain `requests` works

The proven pattern (ported from this project's bhavcopy downloader): a
`requests.Session()`, a real browser User-Agent, and visiting
`nseindia.com/option-chain` **first** to collect cookies before hitting the API.
That's `_get_session()`'s warm-up. No special cloud-workaround library is needed.

## The two-step bootstrap-then-target pattern

`option-chain-v3` needs a valid expiry, but you don't know a valid expiry until
you've asked NSE — and v3 with no expiry returns nothing. So `process_symbol()`
in `run_fetch.py` does two steps per symbol per cycle:

1. **Bootstrap.** Call `fetch_option_chain()` (the flat NextApi endpoint, already
   proven reliable). This gives a guaranteed-valid expiry string plus futures
   data. The first valid `OPT*` expiry from its entries seeds step 2.
2. **Target.** Call `fetch_option_chain_v3()` with that bootstrap expiry. The
   response also includes the **full** list of every expiry NSE has for the
   symbol (`records.expiryDates`) — so one call with any known-good expiry
   doubles as expiry discovery. From that list, pick the nearest monthly expiry
   (`pick_nearest_monthly_expiry()`) and, **only if it differs** from the
   bootstrap expiry, make one more v3 call for it. If the bootstrap expiry is
   already the nearest monthly, no second call is made.

**Why it exists:** v3 is the only endpoint with bid/ask + NSE IV, but it can't be
called blind. The flat endpoint can be called blind but lacks bid/ask + IV. The
bootstrap hands the flat endpoint's reliability to v3, and the extra call is
skipped whenever possible.

## Data sourcing that moved because of dead endpoints

- **Futures prices:** `parse_futures_from_entries()` filters the option-chain
  response itself for `instrumentType` starting with `FUT` (futures legs ride
  along with the options entries). Replaces the dead `quote-derivative` call.
- **Lot size:** no live source exists anymore. `LOT_SIZE_FALLBACK` in
  `nse_fetch.py` is the only source — hand-maintained, re-check NSE circulars
  periodically.

## Data limitation to remember

The working flat endpoint (`GetQuoteApi`) has **no bid/ask and no NSE IV** —
only v3 does. That's the whole reason the two-step pattern exists rather than
just using the flat endpoint everywhere.
