"""
vault_io.py

Handles everything that touches disk under vault/:

    vault/raw/<SYMBOL>/JSON-DD-MM-YYYY.json.gz   -- one growing file per
        symbol per day; each fetch cycle appends one more entry to a list
        inside the (re-written) gzip file.
    vault/tables/MAIN-DD-MM-YYYY.csv             -- one growing file per
        day, all symbols combined, one row per strike/expiry/option-type
        per fetch cycle.

Also owns the freshness check that runs before anything gets written --
a bad/empty NSE response should never silently pollute the archive.
"""

import csv
import gzip
import json
import os

VAULT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vault"
)
RAW_DIR = os.path.join(VAULT_DIR, "raw")
TABLES_DIR = os.path.join(VAULT_DIR, "tables")

CSV_COLUMNS = [
    "fetch_ts_utc",
    "fetch_ts_ist",
    "symbol",
    "expiry_date",
    "strike",
    "option_type",
    "underlying_value",
    "bid_price",
    "bid_qty",
    "ask_price",
    "ask_qty",
    "ltp",
    "mid_price",
    "open_interest",
    "change_in_oi",
    "total_traded_volume",
    "pchange_vs_prev_close",
    "nse_iv",
    "computed_iv",
    "delta", "gamma", "theta", "vega",
    "vanna", "charm", "vomma",
    "speed", "zomma", "color", "veta",
    "omega", "dual_delta", "dual_gamma",
    "time_to_expiry_years",
    "futures_price",
    "implied_cost_of_carry",
    "dividend_yield_used",
    "dividend_yield_source",
    "risk_free_rate_used",
    "india_vix",
    "lot_size",
    "underlying_day_open",
    "underlying_day_high",
    "underlying_day_low",
    "underlying_prev_close",
    "price_source_for_iv",
    "data_quality_flag",
]


def date_stamp(d) -> str:
    """DD-MM-YYYY, zero-padded, for filenames."""
    return d.strftime("%d-%m-%Y")


# ---------------------------------------------------------------------------
# Freshness check -- runs BEFORE any write
# ---------------------------------------------------------------------------

def freshness_ok(symbol: str, underlying_value, rows: list) -> bool:
    if not rows:
        print(f"  [{symbol}] freshness check FAILED: no rows produced.")
        return False
    if underlying_value in (None, 0):
        print(f"  [{symbol}] freshness check FAILED: missing/zero underlying value.")
        return False
    populated_price_rows = sum(
        1 for r in rows if r.get("ltp") or r.get("bid_price") or r.get("ask_price")
    )
    if populated_price_rows < max(1, len(rows) // 10):
        print(f"  [{symbol}] freshness check FAILED: "
              f"only {populated_price_rows}/{len(rows)} rows have any price data.")
        return False
    return True


# ---------------------------------------------------------------------------
# Raw JSON archive (per symbol, per day, gzip, day-appendable)
# ---------------------------------------------------------------------------

def append_raw_snapshot(symbol: str, day, snapshot: dict):
    """
    Appends one fetch-cycle's raw NSE responses to today's gzip archive for
    this symbol. Reads-modify-writes the whole file, which is fine at this
    data volume (a day's worth of 5-minute snapshots is small once gzipped).
    """
    out_dir = os.path.join(RAW_DIR, symbol)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"JSON-{date_stamp(day)}.json.gz")

    existing = []
    if os.path.isfile(out_path):
        try:
            with gzip.open(out_path, "rt", encoding="utf-8") as f:
                existing = json.load(f)
            if not isinstance(existing, list):
                existing = [existing]
        except Exception as exc:  # noqa: BLE001
            print(f"  [{symbol}] WARNING: could not read existing raw archive "
                  f"({exc}); starting a fresh list for today (old file backed up).")
            backup_path = out_path + ".corrupt"
            try:
                os.replace(out_path, backup_path)
            except OSError:
                pass
            existing = []

    existing.append(snapshot)

    tmp_path = out_path + ".tmp"
    with gzip.open(tmp_path, "wt", encoding="utf-8") as f:
        json.dump(existing, f)
    os.replace(tmp_path, out_path)  # atomic-ish swap, avoids half-written files

    print(f"  [{symbol}] raw archive updated -> {out_path} "
          f"({len(existing)} fetch cycle(s) today)")


# ---------------------------------------------------------------------------
# MAIN table (combined, all symbols, one file per day)
# ---------------------------------------------------------------------------

def append_main_rows(day, rows: list):
    if not rows:
        return
    os.makedirs(TABLES_DIR, exist_ok=True)
    out_path = os.path.join(TABLES_DIR, f"MAIN-{date_stamp(day)}.csv")

    file_exists = os.path.isfile(out_path)
    with open(out_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)

    print(f"  MAIN table updated -> {out_path} (+{len(rows)} rows)")
