"""
make_batches.py  —  Step 1.2
Splits the 4 Olist fact tables into monthly CSV files:
  data/incoming/   — full months (>= MIN_ORDERS_PER_MONTH orders), except the last HOLDBACK_N
  data/holdback/   — last HOLDBACK_N full months (saved for the live demo)

Thin edge months (first and last in the dataset) are excluded entirely and logged.
Also injects a small, documented amount of mess into every batch using a fixed
random seed, and logs every injection to data/mess_log.csv.
"""

import logging
import pathlib

import numpy as np
import pandas as pd

# ── constants ─────────────────────────────────────────────────────────────────
RANDOM_SEED          = 42    # fixed → same mess every run; makes cleaning results reproducible
HOLDBACK_N           = 3     # last N full months go to holdback
MIN_ORDERS_PER_MONTH = 100   # months below this threshold are excluded from the feed

ROOT     = pathlib.Path(__file__).resolve().parent.parent   # DA-Project/
RAW      = ROOT / "data" / "raw"
INCOMING = ROOT / "data" / "incoming"
HOLDBACK = ROOT / "data" / "holdback"
MESS_LOG = ROOT / "data" / "mess_log.csv"

# All 5 date columns in orders (some are nullable — delivered_* are null for undelivered orders)
ORDER_DATE_COLS = [
    "order_purchase_timestamp",
    "order_approved_at",
    "order_delivered_carrier_date",
    "order_delivered_customer_date",
    "order_estimated_delivery_date",
]

# Maps the canonical (lowercase) status to realistic casing/whitespace variants.
# Simulates a CSV export from a system that doesn't normalise text, or someone
# copy-pasting from Excel with trailing spaces.
STATUS_VARIANTS: dict[str, list[str]] = {
    "delivered":   ["DELIVERED",   "Delivered",   "delivered ",  " delivered"],
    "shipped":     ["SHIPPED",     "Shipped",     "shipped "],
    "canceled":    ["CANCELED",    "Canceled",    "canceled "],
    "invoiced":    ["INVOICED",    "Invoiced"],
    "processing":  ["PROCESSING",  "Processing"],
    "unavailable": ["UNAVAILABLE", "Unavailable"],
    "approved":    ["APPROVED",    "Approved"],
    "created":     ["CREATED",     "Created"],
}

# Strings that are not valid ISO 8601 datetimes — pd.to_datetime(errors='coerce')
# will turn these into NaT; the count of NaTs produced = "malformed dates fixed" metric.
BAD_DATES = [
    "not-a-date",
    "2018-13-01 00:00:00",   # month 13 — impossible
    "00/00/0000",
    "N/A",
    "pending",
]

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── directory setup ───────────────────────────────────────────────────────────

def setup_dirs() -> None:
    """
    Create output directories.

    mkdir(parents=True, exist_ok=True):
      parents=True  — creates any missing intermediate folders in one call
                      (e.g. data/ and incoming/ together if data/ doesn't exist yet).
      exist_ok=True — silently skips if the folder already exists; makes this
                      function idempotent (safe to call multiple times).
    """
    for d in (INCOMING, HOLDBACK):
        d.mkdir(parents=True, exist_ok=True)
    log.info("Directories ready: incoming=%s  holdback=%s", INCOMING, HOLDBACK)


# ── loading ───────────────────────────────────────────────────────────────────

def load_raw_tables() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Load the 4 fact tables from data/raw/.

    parse_dates=[...]: read_csv parses the listed columns as datetime64 objects in
    one pass. Alternative: omit parse_dates and call pd.to_datetime() after reading
    — same result but requires a second step.

    items/payments/reviews have no timestamp column so we read them as plain strings.
    """
    log.info("Loading raw tables …")

    orders = pd.read_csv(
        RAW / "olist_orders_dataset.csv",
        parse_dates=ORDER_DATE_COLS,
    )
    items    = pd.read_csv(RAW / "olist_order_items_dataset.csv")
    payments = pd.read_csv(RAW / "olist_order_payments_dataset.csv")
    reviews  = pd.read_csv(RAW / "olist_order_reviews_dataset.csv")

    log.info(
        "Loaded  orders=%d  items=%d  payments=%d  reviews=%d",
        len(orders), len(items), len(payments), len(reviews),
    )
    return orders, items, payments, reviews


# ── month assignment ──────────────────────────────────────────────────────────

def assign_purchase_month(
    orders: pd.DataFrame,
    items: pd.DataFrame,
    payments: pd.DataFrame,
    reviews: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Add a 'purchase_month' column ("YYYY_MM") to every table.

    For orders (has a date column):
      .dt.to_period('M') — converts each Timestamp to a Period object that holds
                           only year + month, e.g. Timestamp('2018-03-15') →
                           Period('2018-03', 'M').  Strips the day/time component.
      .astype(str)       — Period → plain string "2018-03"
      .str.replace('-','_') — "2018-03" → "2018_03" (safe as a filename segment)

    For items/payments/reviews (no date column):
      merge(how='left') — joins the month_map on order_id to inherit the month.
      how='left' keeps every row even if order_id is missing from orders (a data
      quality issue we want to surface later).
      Alternative: how='inner' would silently drop those orphan rows.

    merge vs join: merge() is explicit about which columns to match;
    join() matches on the index — merge() is clearer and safer here.
    """
    # Build a lightweight lookup: order_id → purchase_month
    month_map = (
        orders[["order_id", "order_purchase_timestamp"]]
        .assign(
            purchase_month=lambda df: (
                df["order_purchase_timestamp"]
                .dt.to_period("M")               # datetime → Period('YYYY-MM')
                .astype(str)                      # Period  → "YYYY-MM"
                .str.replace("-", "_", regex=False)  # "YYYY-MM" → "YYYY_MM"
            )
        )
        [["order_id", "purchase_month"]]          # keep only the two columns we need
    )

    orders   = orders.merge(month_map,   on="order_id", how="left")
    items    = items.merge(month_map,    on="order_id", how="left")
    payments = payments.merge(month_map, on="order_id", how="left")
    reviews  = reviews.merge(month_map,  on="order_id", how="left")

    return orders, items, payments, reviews


# ── month filtering ──────────────────────────────────────────────────────────

def filter_full_months(orders: pd.DataFrame, months: list[str]) -> list[str]:
    """
    Return only the months that have at least MIN_ORDERS_PER_MONTH orders.
    Log each excluded month with its order count and the reason.

    Why exclude thin months?
      The first and last months in a dataset are almost always partial — the
      dataset started mid-September 2016 (5 orders) and was cut off mid-October
      2018 (5 orders).  Keeping them distorts month-over-month trend calculations
      (a half-month next to a full month looks like a 90 % drop) and makes the
      pipeline's demo unreliable.  Excluding them is a deliberate, documented
      analytical choice, not data loss.

    groupby().size(): counts rows per group, returns a Series keyed by group value.
    Alt: orders['purchase_month'].value_counts() — same numbers, different sort order
    (value_counts sorts descending by count; groupby().size() keeps key order).
    """
    counts = orders.groupby("purchase_month").size()   # Series: month → order count

    valid: list[str] = []
    excluded: list[tuple[str, int]] = []

    for m in months:
        n = int(counts.get(m, 0))
        if n >= MIN_ORDERS_PER_MONTH:
            valid.append(m)
        else:
            excluded.append((m, n))

    for m, n in excluded:
        log.info(
            "EXCLUDED month=%s  orders=%4d  reason=below MIN_ORDERS_PER_MONTH=%d",
            m, n, MIN_ORDERS_PER_MONTH,
        )

    log.info(
        "%d full months kept, %d thin edge months excluded",
        len(valid), len(excluded),
    )
    return valid


# ── mess injection helpers ────────────────────────────────────────────────────

def _mess_duplicates(
    df: pd.DataFrame, table: str, month: str, rng: np.random.Generator
) -> tuple[pd.DataFrame, dict]:
    """
    Append ~1 % of rows as exact duplicates.

    sample(frac=0.01): randomly picks 1 % of rows without replacement.
    Alt: sample(n=k) picks exactly k rows — useful when frac gives 0 rows on tiny frames.
    random_state: makes the sample reproducible; we derive a fresh int from rng
    so each call uses a different draw even though the top-level seed is fixed.

    concat(..., ignore_index=True): stacks two DataFrames vertically and
    resets the integer index (0, 1, 2 …).  Without ignore_index=True the
    duplicate rows would share index labels with their originals.
    """
    seed = int(rng.integers(0, 2**31))
    dup_rows = df.sample(frac=0.01, random_state=seed)
    n_dups = max(1, len(dup_rows))   # guarantee at least 1 duplicate
    if len(dup_rows) == 0:
        dup_rows = df.sample(n=1, random_state=seed)

    df = pd.concat([df, dup_rows], ignore_index=True)

    return df, {
        "file":         f"{table}_{month}.csv",
        "table":        table,
        "month":        month,
        "mess_type":    "duplicate_rows",
        "affected_rows": len(dup_rows),
        "details":      f"Appended {len(dup_rows)} duplicate rows (~1 % of original batch)",
    }


def _mess_status_casing(
    df: pd.DataFrame, table: str, month: str, rng: np.random.Generator
) -> tuple[pd.DataFrame, dict]:
    """
    Replace ~2 % of order_status values with casing/whitespace variants
    (e.g. "delivered" → "DELIVERED", "Delivered", "delivered ").

    Realistic origin: manual CSV exports, copy-paste from Excel, or a system
    that doesn't enforce a controlled vocabulary.

    rng.choice(len(df), size=n, replace=False): picks n unique row positions
    (integers 0 … len-1); replace=False means no position is chosen twice.
    df.index[positions]: converts positional integers to the actual index labels
    so we can use label-safe .at[label, col] instead of position-based .iat[].
    """
    n_rows = max(1, round(len(df) * 0.02))
    positions = rng.choice(len(df), size=min(n_rows, len(df)), replace=False)
    row_labels = df.index[positions].tolist()

    count = 0
    for label in row_labels:
        original = str(df.at[label, "order_status"])
        variants = STATUS_VARIANTS.get(original.lower().strip(), [])
        if variants:
            df.at[label, "order_status"] = variants[int(rng.integers(0, len(variants)))]
            count += 1

    return df, {
        "file":         f"{table}_{month}.csv",
        "table":        table,
        "month":        month,
        "mess_type":    "status_casing",
        "affected_rows": count,
        "details":      f"Replaced {count} order_status values with casing/whitespace variants",
    }


def _mess_bad_dates(
    df: pd.DataFrame, table: str, month: str, rng: np.random.Generator
) -> tuple[pd.DataFrame, dict]:
    """
    Replace a handful of valid date strings with unparseable values.

    Targets order_approved_at and order_delivered_customer_date — both nullable
    columns (NaT for unapproved / undelivered orders).  At the point this function
    runs, all datetime columns have already been converted to strings by the caller,
    so we simply overwrite some non-null string cells.

    pd.to_datetime(col, errors='coerce') in clean.py will turn these into NaT.
    The count of NaTs produced there = your "malformed dates fixed" metric.
    errors='coerce' vs errors='raise': 'coerce' replaces bad values with NaT and
    continues; 'raise' crashes on the first bad value — coerce is safer for pipelines.
    """
    target_cols = [
        c for c in ("order_approved_at", "order_delivered_customer_date")
        if c in df.columns
    ]
    n_per_col = max(1, round(len(df) * 0.005))   # ~0.5 % per target column
    count = 0

    for col in target_cols:
        # Only corrupt rows that already have a valid date string (non-null)
        non_null_labels = df.index[df[col].notna()].tolist()
        if not non_null_labels:
            continue
        n = min(n_per_col, len(non_null_labels))
        chosen_pos = rng.choice(len(non_null_labels), size=n, replace=False)
        for pos in chosen_pos:
            df.at[non_null_labels[pos], col] = BAD_DATES[int(rng.integers(0, len(BAD_DATES)))]
            count += 1

    return df, {
        "file":         f"{table}_{month}.csv",
        "table":        table,
        "month":        month,
        "mess_type":    "malformed_dates",
        "affected_rows": count,
        "details":      f"Inserted {count} bad date strings into {target_cols}",
    }


def _mess_blank_strings(
    df: pd.DataFrame, table: str, month: str, rng: np.random.Generator
) -> tuple[pd.DataFrame, dict | None]:
    """
    Replace ~30 % of NaN values in nullable text columns with a single space ' '.

    Why ' ' (space) instead of '' (empty string)?
      An empty string written to CSV becomes an empty cell, which pd.read_csv()
      converts back to NaN on the next read — the mess disappears instantly.
      A space ' ' is non-empty, so it round-trips: CSV → ' ' → still a string.
      df.isna() will NOT flag it.  The cleaning step must use:
          df[col].str.strip().replace('', np.nan)
      That two-step pattern — strip whitespace, then replace the resulting empty
      string with NaN — is a classic interview question about data cleaning.

    Only applied to free-text columns that are genuinely nullable:
      reviews → review_comment_title, review_comment_message
        (most reviewers don't write a comment, so ~60-70 % of rows are null here)
    """
    nullable_cols = {
        "reviews": ["review_comment_title", "review_comment_message"],
    }.get(table, [])

    count = 0
    for col in nullable_cols:
        if col not in df.columns:
            continue
        null_labels = df.index[df[col].isna()].tolist()
        if not null_labels:
            continue
        n = max(1, round(len(null_labels) * 0.30))
        chosen_pos = rng.choice(len(null_labels), size=min(n, len(null_labels)), replace=False)
        for pos in chosen_pos:
            df.at[null_labels[pos], col] = " "   # space: survives CSV round-trip, looks blank
            count += 1

    if count == 0:
        return df, None

    return df, {
        "file":         f"{table}_{month}.csv",
        "table":        table,
        "month":        month,
        "mess_type":    "blank_strings",
        "affected_rows": count,
        "details":      f"Replaced {count} NaN values with ' ' in {nullable_cols}",
    }


def inject_mess(
    df: pd.DataFrame, table: str, month: str, rng: np.random.Generator
) -> tuple[pd.DataFrame, list[dict]]:
    """
    Apply all mess injections in sequence.  Returns the messy DataFrame and a
    list of log-entry dicts (one per injection type that produced at least 1 change).
    """
    df = df.copy()
    entries: list[dict] = []

    # 1. Duplicate rows — every table
    df, e = _mess_duplicates(df, table, month, rng)
    entries.append(e)

    # 2. Status casing — orders only (only table with order_status column)
    if "order_status" in df.columns:
        df, e = _mess_status_casing(df, table, month, rng)
        entries.append(e)

    # 3. Bad dates — orders only (date columns must already be strings when this runs)
    if "order_purchase_timestamp" in df.columns:
        df, e = _mess_bad_dates(df, table, month, rng)
        entries.append(e)

    # 4. Blank strings — reviews only
    df, e = _mess_blank_strings(df, table, month, rng)
    if e is not None:
        entries.append(e)

    return df, entries


# ── batch writer ──────────────────────────────────────────────────────────────

def write_batches(
    dfs: dict[str, pd.DataFrame],
    holdback_months: set[str],
) -> list[dict]:
    """
    Iterate over every purchase month, write 4 CSV files per month, and inject mess.

    Uses a groupby loop over orders to discover months, then isin() to filter
    the other 3 tables for each month.

    groupby('purchase_month'): splits the DataFrame into groups by column value and
    yields (group_key, sub_dataframe) pairs — one per unique month.
    Alt: iterate over a sorted list of months and use df[df['col'] == month].

    isin([month]): keeps rows where purchase_month matches; written as a list to
    show the isin pattern — handy when you need to filter for multiple values at once.
    Alt: df['purchase_month'] == month — identical for a single value.
    """
    # Single seeded generator — all randomness in this run is derived from RANDOM_SEED.
    # np.random.default_rng is the modern NumPy API (replaces np.random.seed + legacy functions).
    rng = np.random.default_rng(RANDOM_SEED)
    all_entries: list[dict] = []

    # groupby on the orders table drives the loop — orders defines which months exist
    for month, orders_grp in dfs["orders"].groupby("purchase_month"):
        dest = HOLDBACK if month in holdback_months else INCOMING

        # Build a dict of {table_name: batch_df} for this month
        batches: dict[str, pd.DataFrame] = {
            "orders": orders_grp.drop(columns=["purchase_month"]).copy(),
        }
        for tname in ("order_items", "payments", "reviews"):
            df = dfs[tname]
            batches[tname] = (
                df[df["purchase_month"].isin([month])]
                .drop(columns=["purchase_month"])
                .copy()
            )

        for table_name, batch in batches.items():
            if len(batch) == 0:
                log.warning("No rows for table=%s month=%s — skipping", table_name, month)
                continue

            # Convert datetime columns to strings BEFORE mess injection so every
            # inject function works with uniform string types.
            # dt.strftime('%Y-%m-%d %H:%M:%S'): NaT → NaN (not the string "NaT"),
            # valid timestamps → "2018-03-15 10:00:00".
            # Alt: .astype(str) — converts NaT to the literal string "NaT", which
            # would appear in the CSV and confuse date parsers downstream.
            if table_name == "orders":
                for col in ORDER_DATE_COLS:
                    if col in batch.columns and pd.api.types.is_datetime64_any_dtype(batch[col]):
                        batch[col] = batch[col].dt.strftime("%Y-%m-%d %H:%M:%S")

            batch, entries = inject_mess(batch, table_name, month, rng)
            all_entries.extend(entries)

            # to_csv(index=False): write the data only — without the pandas row-number
            # index column (0, 1, 2 …) which the downstream pipeline doesn't expect.
            # Alt: to_excel() — heavier format, not needed for plain pipeline input files.
            out_path = dest / f"{table_name}_{month}.csv"
            batch.to_csv(out_path, index=False)

        log.info("month=%-9s → %s  (%s)", month, dest.name,
                 "holdback" if month in holdback_months else "incoming")

    return all_entries


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """Load → split by month → inject mess → save CSVs → write mess_log.csv."""
    setup_dirs()

    orders, items, payments, reviews = load_raw_tables()
    orders, items, payments, reviews = assign_purchase_month(orders, items, payments, reviews)

    # unique(): returns an array of the distinct values in a Series (unordered).
    # sorted(): sorts lexicographically — for YYYY_MM strings this equals chronological order.
    # dropna(): excludes the rare row whose purchase_month is NaN (e.g. a null timestamp).
    all_months = sorted(
        m for m in orders["purchase_month"].dropna().unique()
        if str(m) != "NaT"
    )
    log.info("Raw months found: %d  (%s … %s)", len(all_months), all_months[0], all_months[-1])

    # Drop thin edge months before deciding holdback — ensures the last 3 months
    # in the feed are genuinely full months, not partial dataset boundaries.
    months = filter_full_months(orders, all_months)
    log.info("Full months: %d  (%s … %s)", len(months), months[0], months[-1])

    # Last HOLDBACK_N full months are withheld for the live demo
    holdback_months = set(months[-HOLDBACK_N:])
    log.info(
        "Holdback: %s  |  Incoming: %d full months",
        sorted(holdback_months), len(months) - HOLDBACK_N,
    )

    # Filter every table to full months only so write_batches never sees excluded rows.
    # isin(months): keeps rows whose purchase_month is in the valid list.
    dfs = {
        "orders":      orders[orders["purchase_month"].isin(months)],
        "order_items": items[items["purchase_month"].isin(months)],
        "payments":    payments[payments["purchase_month"].isin(months)],
        "reviews":     reviews[reviews["purchase_month"].isin(months)],
    }

    all_entries = write_batches(dfs, holdback_months)

    # Build the mess log and save — one row per injection event
    mess_df = pd.DataFrame(all_entries)
    mess_df.to_csv(MESS_LOG, index=False)
    log.info(
        "Mess log written → %s  (%d entries)",
        MESS_LOG.relative_to(ROOT), len(mess_df),
    )

    incoming_count = len(list(INCOMING.glob("*.csv")))
    holdback_count = len(list(HOLDBACK.glob("*.csv")))
    log.info(
        "Done — incoming: %d files  holdback: %d files",
        incoming_count, holdback_count,
    )


if __name__ == "__main__":
    main()
