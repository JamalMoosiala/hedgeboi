"""
nse_fetch.py

All calls to NSE's unofficial JSON endpoints, wrapped with retries so a
transient blip on NSE's side (or a rate-limit hiccup) doesn't kill an
entire run. Every function here can raise -- callers are expected to catch
per-symbol, not let one bad symbol take down the others (see run_fetch.py).

NOTE ON RELIABILITY: nse_optionchain_scrapper (option chain) is the most
battle-tested of these, widely used by the nsepython community. The
futures endpoint (quote-derivative) and the index-snapshot endpoint
(allIndices) are used here directly via nsefetch and are more likely to
have their exact JSON key names drift over time, since they see less
community-maintenance attention. If lot_size or futures_price start
showing up as None across the board, that's the first place to check --
open https://www.nseindia.com/api/quote-derivative?symbol=NIFTY (or
allIndices) in a browser and diff the JSON shape against the parsing
below.
"""

import time

# IMPORTANT: nsepythonserver, not nsepython. nsepython's own docs split
# into a "Local Edition" (laptops) and a "Server Edition" for cloud/
# datacenter IPs (AWS, Google Colab, DigitalOcean) -- GitHub Actions
# runners are the same class of infrastructure, and NSE's anti-bot layer
# silently returns empty/invalid responses to nsepython's local edition
# from these IP ranges. If function names ever diverge between the two
# packages in a future nsepython release, this is the first place to check.
from nsepythonserver import nse_optionchain_scrapper, nsefetch

MAX_RETRIES = 3
RETRY_BACKOFF_BASE_SECONDS = 3  # 3s, 6s, 12s...

# Deliberate pause after EVERY successful NSE call, regardless of which
# function made it. A single run makes up to ~7 calls (1 index snapshot +
# 3 symbols x 2 calls each) in rapid succession -- live testing showed the
# FIRST call in a run succeeding with real data while every call after it
# came back as an empty {} (not an error, not a block page -- just
# nothing). That pattern points to NSE rate-limiting bursts of requests
# from the same session/IP within a short window, not a blanket block.
# Spacing calls out is the cheap first thing to try before assuming a
# harder infrastructure fix (self-hosted runner, proxy) is needed.
SLEEP_BETWEEN_CALLS_SECONDS = 4

# Fallback lot sizes, used only if the live futures response doesn't carry
# a parseable lot size. Current as of the January 2026 NSE revision --
# check https://www.nseindia.com/all-reports-derivatives (lot size circular)
# periodically and update this table when NSE next revises lot sizes.
LOT_SIZE_FALLBACK = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "NIFTYNXT50": 25,
}

# NSE's display names for these indices on the allIndices endpoint. If the
# index-snapshot fetch starts returning None for a symbol, the likely cause
# is NSE having renamed the index slightly -- check the raw allIndices
# response and update these strings.
INDEX_DISPLAY_NAME = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "NIFTYNXT50": "NIFTY NEXT 50",
}
INDIA_VIX_DISPLAY_NAME = "INDIA VIX"


def _retry(fn, *args, what: str, **kwargs):
    """Generic retry wrapper with exponential backoff. Re-raises the last
    exception if all attempts fail, so the caller can decide how to log/
    skip; this function's only job is to absorb TRANSIENT failures.

    Also pauses SLEEP_BETWEEN_CALLS_SECONDS after every successful call --
    see the constant's comment above for why. This means every NSE-hitting
    function in this module (fetch_option_chain, fetch_futures_raw,
    fetch_index_snapshot_raw) is automatically spaced out, without callers
    needing to remember to add their own delays between calls."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = fn(*args, **kwargs)
            time.sleep(SLEEP_BETWEEN_CALLS_SECONDS)
            return result
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                print(f"  [{what}] attempt {attempt}/{MAX_RETRIES} failed "
                      f"({exc}); retrying in {wait}s...")
                time.sleep(wait)
    raise last_exc


def fetch_option_chain(symbol: str) -> dict:
    """Spot + full option chain, in one call -- this is what keeps spot and
    option quotes synchronized to the same moment."""
    return _retry(nse_optionchain_scrapper, symbol, what=f"option_chain:{symbol}")


def fetch_futures_raw(symbol: str) -> dict:
    """Raw quote-derivative response for a symbol's futures contracts."""
    url = f"https://www.nseindia.com/api/quote-derivative?symbol={symbol}"
    return _retry(nsefetch, url, what=f"futures:{symbol}")


def parse_futures(raw: dict) -> dict:
    """Returns {expiry_date_str: futures_price} from a quote-derivative response."""
    futures = {}
    for item in raw.get("stocks", []):
        meta = item.get("metadata", {})
        instrument = meta.get("instrumentType", "")
        if "Futures" in instrument:
            expiry = meta.get("expiryDate")
            ltp = meta.get("lastPrice")
            if expiry and ltp:
                futures[expiry] = ltp
    return futures


def parse_lot_size(raw: dict, symbol: str) -> int:
    """Tries a couple of plausible key locations for lot size in the
    quote-derivative response; falls back to the static table if none work."""
    for item in raw.get("stocks", []):
        meta = item.get("metadata", {})
        for key in ("lotSize", "marketLot"):
            val = meta.get(key)
            if val:
                return int(val)
        trade_info = item.get("marketDeptOrderBook", {}).get("tradeInfo", {})
        for key in ("marketLot", "lotSize"):
            val = trade_info.get(key)
            if val:
                return int(val)
    return LOT_SIZE_FALLBACK.get(symbol)


def fetch_index_snapshot_raw() -> dict:
    """Single call covering day OHLC for all indices plus India VIX."""
    url = "https://www.nseindia.com/api/allIndices"
    return _retry(nsefetch, url, what="all_indices")


def parse_index_snapshot(raw: dict, symbol: str) -> dict:
    """Returns {open, high, low, prev_close, last} for one of our tracked
    symbols, pulled from the allIndices response. Returns a dict of Nones
    if the display name isn't found (logged by the caller)."""
    target_name = INDEX_DISPLAY_NAME.get(symbol)
    return _extract_index_row(raw, target_name)


def parse_india_vix(raw: dict) -> dict:
    return _extract_index_row(raw, INDIA_VIX_DISPLAY_NAME)


def _extract_index_row(raw: dict, target_name: str) -> dict:
    empty = {"open": None, "high": None, "low": None, "prev_close": None, "last": None}
    if not target_name:
        return empty
    for row in raw.get("data", []):
        name = (row.get("index") or row.get("indexSymbol") or "").strip().upper()
        if name == target_name.upper():
            return {
                "open": row.get("open"),
                "high": row.get("high") or row.get("dayHigh"),
                "low": row.get("low") or row.get("dayLow"),
                "prev_close": row.get("previousClose"),
                "last": row.get("last") or row.get("lastPrice"),
            }
    return empty
