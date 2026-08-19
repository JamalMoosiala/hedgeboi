"""
run_fetch.py

Entry point. One run = one fetch cycle across all three symbols.

Design principles baked in here (per everything discussed building this):
- Skip entirely, cleanly, on weekends/holidays -- no files touched, exit 0.
- Every symbol is fetched and processed independently: one symbol's
  failure (NSE hiccup, parsing error, whatever) is logged and skipped,
  the other symbols still get processed and written.
- fetch_ts is the script's OWN clock, logged on every row -- never trust
  the nominal cron time, cron drift is expected and handled by this.
- IV/Greeks prefer mid_price (bid+ask)/2 over LTP; LTP can be a stale
  trade from hours ago on an illiquid strike, mid_price reflects live
  market-maker quotes even without a trade.
- Bad-quote guardrail: price below intrinsic value -> no IV/Greeks (not
  forced), flagged no_price_available or similar in data_quality_flag.
- Wide-quote flag: a usable price with a very wide bid-ask spread gets
  IV/Greeks computed, but is flagged wide_quote_low_liquidity so you can
  choose whether to trust it downstream.
- Freshness check runs BEFORE writing anything for a symbol.
- Cost-of-carry (and therefore dividend yield) is derived from the
  futures price for the same symbol/expiry, not assumed -- see
  get_cost_of_carry(). Falls back to a static assumption, flagged, when
  futures aren't available.
- Raw JSON (untouched NSE responses) is archived per symbol per day
  regardless of whether the MAIN row-building succeeds, since the raw
  archive is the future-proofing layer and shouldn't be gated on today's
  parsing logic being perfect.
"""

import math
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import greeks
import holidays
import nse_fetch
import vault_io

IST = ZoneInfo("Asia/Kolkata")

SYMBOLS = ["NIFTY", "BANKNIFTY", "NIFTYNXT50"]

RISK_FREE_RATE = 0.065          # static assumption; only affects discounting
                                  # once cost-of-carry is futures-implied
FALLBACK_DIVIDEND_YIELD = 0.0    # used only when futures price unavailable
WIDE_QUOTE_SPREAD_PCT = 0.15     # bid-ask spread / mid_price threshold


def gha_warning(msg: str):
    print(f"::warning::{msg}")


def gha_error(msg: str):
    print(f"::error::{msg}")


def years_to_expiry(expiry_str: str, as_of: datetime) -> float:
    """expiry_str like '25-Sep-2026'. Options stop trading at 15:30 IST."""
    expiry_dt = datetime.strptime(expiry_str, "%d-%b-%Y").replace(
        hour=15, minute=30, tzinfo=IST
    )
    delta = (expiry_dt - as_of).total_seconds()
    return max(delta, 0.0) / (365.0 * 24 * 3600)


def get_cost_of_carry(futures_by_expiry: dict, expiry_date: str, S: float, T: float):
    """Returns (implied_cost_of_carry, dividend_yield_used, source)."""
    F = futures_by_expiry.get(expiry_date)
    if F and S and T and T > 0:
        b = math.log(F / S) / T
        q_implied = RISK_FREE_RATE - b
        return b, q_implied, "futures_implied", F
    b = RISK_FREE_RATE - FALLBACK_DIVIDEND_YIELD
    return b, FALLBACK_DIVIDEND_YIELD, "static_fallback", None


def classify_quote(bid, ask, mid) -> str:
    """ok vs wide_quote_low_liquidity, based on relative bid-ask spread."""
    if not bid or not ask or not mid:
        return "ok"  # spread check doesn't apply if we don't have both sides
    spread_pct = (ask - bid) / mid if mid else None
    if spread_pct is not None and spread_pct > WIDE_QUOTE_SPREAD_PCT:
        return "wide_quote_low_liquidity"
    return "ok"


def process_symbol(symbol: str, fetch_ts_utc: datetime, fetch_ts_ist: datetime,
                    index_snapshot_raw: dict):
    """
    Fetches and processes one symbol end to end. Returns (rows, raw_snapshot,
    status_str). Raises only on truly unexpected errors the caller should
    log and move past -- most expected failure modes (missing futures,
    missing VIX row, etc.) are handled internally and reflected in
    per-row flags / status, not exceptions.
    """
    status = {"option_chain": "ok", "futures": "ok", "lot_size": "ok"}

    option_chain_raw = nse_fetch.fetch_option_chain(symbol)

    if not option_chain_raw or "records" not in option_chain_raw:
        status["option_chain"] = "empty_response"
        gha_warning(f"[{symbol}] option chain came back as an empty/unexpected response "
                    f"(no 'records' key) -- this symbol will fail its freshness check "
                    f"this cycle. Raw response still archived for inspection.")

    records = option_chain_raw.get("records", {})
    underlying_value = records.get("underlyingValue")
    data = records.get("data", [])

    futures_by_expiry = {}
    lot_size = None
    futures_raw = None
    try:
        futures_raw = nse_fetch.fetch_futures_raw(symbol)
        futures_by_expiry = nse_fetch.parse_futures(futures_raw)
        lot_size = nse_fetch.parse_lot_size(futures_raw, symbol)
        if not futures_by_expiry:
            status["futures"] = "empty_response"
            gha_warning(f"[{symbol}] futures response parsed but no expiries found; "
                        f"dividend yield will use static_fallback for all rows this cycle.")
    except Exception as exc:  # noqa: BLE001
        status["futures"] = f"fetch_failed: {exc}"
        gha_warning(f"[{symbol}] futures fetch failed after retries ({exc}); "
                    f"dividend yield will use static_fallback for all rows this cycle.")

    if lot_size is None:
        lot_size = nse_fetch.LOT_SIZE_FALLBACK.get(symbol)
        status["lot_size"] = "used_fallback_table"

    idx_ohlc = nse_fetch.parse_index_snapshot(index_snapshot_raw, symbol) if index_snapshot_raw else {}
    india_vix_row = nse_fetch.parse_india_vix(index_snapshot_raw) if index_snapshot_raw else {}
    india_vix = india_vix_row.get("last")

    rows = []
    for entry in data:
        expiry_date = entry.get("expiryDate")
        strike = entry.get("strikePrice")
        T = years_to_expiry(expiry_date, fetch_ts_ist) if expiry_date else None

        for opt_type in ("CE", "PE"):
            leg = entry.get(opt_type)
            if not leg:
                continue

            bid = leg.get("bidprice") or leg.get("bidPrice") or 0.0
            ask = leg.get("askPrice") or 0.0
            ltp = leg.get("lastPrice") or 0.0
            mid = (bid + ask) / 2 if (bid and ask) else None
            nse_iv = leg.get("impliedVolatility")
            pchange = leg.get("pChange")

            row = {c: None for c in vault_io.CSV_COLUMNS}
            row.update({
                "fetch_ts_utc": fetch_ts_utc.isoformat(),
                "fetch_ts_ist": fetch_ts_ist.isoformat(),
                "symbol": symbol,
                "expiry_date": expiry_date,
                "strike": strike,
                "option_type": opt_type,
                "underlying_value": underlying_value,
                "bid_price": bid,
                "bid_qty": leg.get("bidQty"),
                "ask_price": ask,
                "ask_qty": leg.get("askQty"),
                "ltp": ltp,
                "mid_price": mid,
                "open_interest": leg.get("openInterest"),
                "change_in_oi": leg.get("changeinOpenInterest"),
                "total_traded_volume": leg.get("totalTradedVolume"),
                "pchange_vs_prev_close": pchange,
                "nse_iv": nse_iv,
                "time_to_expiry_years": T,
                "india_vix": india_vix,
                "lot_size": lot_size,
                "underlying_day_open": idx_ohlc.get("open"),
                "underlying_day_high": idx_ohlc.get("high"),
                "underlying_day_low": idx_ohlc.get("low"),
                "underlying_prev_close": idx_ohlc.get("prev_close"),
            })

            price_for_iv = mid if mid else (ltp if ltp else None)
            price_source = "mid_price" if mid else ("ltp" if ltp else "none")
            row["price_source_for_iv"] = price_source

            if not price_for_iv or not underlying_value or not strike or not T or T <= 0:
                row["data_quality_flag"] = "no_price_available"
                rows.append(row)
                continue

            b, q_used, carry_source, F = get_cost_of_carry(
                futures_by_expiry, expiry_date, underlying_value, T,
            )
            row["futures_price"] = F
            row["implied_cost_of_carry"] = b
            row["dividend_yield_used"] = q_used
            row["dividend_yield_source"] = carry_source
            row["risk_free_rate_used"] = RISK_FREE_RATE

            iv = greeks.implied_vol(
                price_for_iv, underlying_value, strike, T,
                RISK_FREE_RATE, q_used, opt_type,
            )
            row["computed_iv"] = iv

            if iv is None:
                row["data_quality_flag"] = "no_price_available"
                rows.append(row)
                continue

            g = greeks.compute_all_greeks(
                underlying_value, strike, T, RISK_FREE_RATE, q_used, iv, opt_type,
            )
            row.update(g)
            row["data_quality_flag"] = classify_quote(bid, ask, mid)

            rows.append(row)

    snapshot = {
        "fetch_ts_utc": fetch_ts_utc.isoformat(),
        "fetch_ts_ist": fetch_ts_ist.isoformat(),
        "symbol": symbol,
        "responses": {
            "option_chain": option_chain_raw,
            "futures": futures_raw,
            "index_snapshot": index_snapshot_raw,
        },
        "fetch_status": status,
    }

    return rows, snapshot


def main():
    fetch_ts_utc = datetime.now(timezone.utc)
    fetch_ts_ist = fetch_ts_utc.astimezone(IST)
    today = fetch_ts_ist.date()

    if not holidays.is_trading_day(today):
        print(f"{today.isoformat()} is not a trading day (weekend or holiday). "
              f"Skipping run cleanly -- no files touched.")
        sys.exit(0)

    print(f"Fetch cycle started at {fetch_ts_ist.isoformat()} (IST)")

    # One call, reused across all three symbols (day OHLC + India VIX).
    # If this fails, we don't abort the whole run -- OHLC/VIX columns will
    # just be null for this cycle, everything else still gets fetched.
    index_snapshot_raw = None
    try:
        index_snapshot_raw = nse_fetch.fetch_index_snapshot_raw()
    except Exception as exc:  # noqa: BLE001
        gha_warning(f"Index snapshot (OHLC + India VIX) fetch failed after retries "
                    f"({exc}); those columns will be null for this cycle.")

    any_symbol_succeeded = False
    all_main_rows = []

    for symbol in SYMBOLS:
        print(f"Processing {symbol}...")
        try:
            rows, snapshot = process_symbol(symbol, fetch_ts_utc, fetch_ts_ist, index_snapshot_raw)
        except Exception as exc:  # noqa: BLE001
            gha_error(f"[{symbol}] unexpected failure, skipping this symbol this cycle: {exc}")
            continue

        underlying_value = rows[0]["underlying_value"] if rows else None
        if not vault_io.freshness_ok(symbol, underlying_value, rows):
            gha_warning(f"[{symbol}] failed freshness check -- not writing MAIN rows "
                        f"for this symbol this cycle (raw archive still saved for audit).")
            # Still archive the raw response even on freshness failure --
            # useful for debugging why NSE returned something unusable.
            try:
                vault_io.append_raw_snapshot(symbol, today, snapshot)
            except Exception as exc:  # noqa: BLE001
                gha_warning(f"[{symbol}] could not write raw archive either: {exc}")
            continue

        try:
            vault_io.append_raw_snapshot(symbol, today, snapshot)
        except Exception as exc:  # noqa: BLE001
            gha_warning(f"[{symbol}] raw archive write failed: {exc} (MAIN rows still kept)")

        all_main_rows.extend(rows)
        any_symbol_succeeded = True
        print(f"  [{symbol}] {len(rows)} rows ready for MAIN table.")

    if all_main_rows:
        vault_io.append_main_rows(today, all_main_rows)

    if not any_symbol_succeeded:
        gha_error("No symbol produced usable data this cycle. Check NSE endpoint "
                  "health and the warnings/errors above.")
        # Exit 0 anyway -- a single bad cycle shouldn't fail the whole
        # scheduled workflow (retries + the next cycle will likely recover).
        # The ::error:: annotation above still makes this visible in the
        # Actions run summary.
    sys.exit(0)


if __name__ == "__main__":
    main()
