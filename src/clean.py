"""
clean.py  —  Step 1.4
Turns raw, inconsistent Olist files into analysis-ready DataFrames.

One function per table. Each takes a DataFrame read *as raw text* (see read_csv_as_text)
and returns (clean_df, stats_dict). Works on both the raw reference tables in data/raw/
and any monthly file in data/incoming/, data/holdback/ or data/archive/. It never writes to data/.

Cleaning order (checklist A–J in PROJECT_GUIDE.md):
  A. standardize text, turn blanks / whitespace-only strings into real nulls
  B. fix types (dates, numbers); count values that were malformed
  C. drop exact duplicates, THEN key-based duplicates (two separate counts)
  D. missing values: drop / fill / flag, decided per column
  E. fix column names
  F. enrich (English category names)
  G. flag logic problems (never delete)
  H. derived columns
  I. stats dict returned by every function
  J. proof: tests/test_clean.py reconciles stats against data/mess_log.csv

Usage:
  python src/clean.py            → clean the raw reference tables, log stats
  python src/clean.py 2017_11    → clean one monthly batch (incoming, holdback or archive), log stats
"""

import logging
import pathlib
import sys
import time

import numpy as np
import pandas as pd

# ── paths & constants ─────────────────────────────────────────────────────────
ROOT     = pathlib.Path(__file__).resolve().parent.parent   # DA-Project/
RAW      = ROOT / "data" / "raw"
INCOMING = ROOT / "data" / "incoming"
HOLDBACK = ROOT / "data" / "holdback"
ARCHIVE  = ROOT / "data" / "archive"     # files the loader has already loaded (Step 2.2)

# Every Olist timestamp uses this layout. An explicit format is strict: "2018-13-01" fails
# (→ NaT) instead of being guessed, and parsing is faster than letting pandas infer.
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

ORDER_DATE_COLS = [
    "order_purchase_timestamp",
    "order_approved_at",
    "order_delivered_carrier_date",
    "order_delivered_customer_date",
    "order_estimated_delivery_date",
]

# The 8 legitimate order statuses. Anything else after standardizing gets flagged.
CANONICAL_STATUSES = {
    "delivered", "shipped", "canceled", "unavailable",
    "invoiced", "processing", "created", "approved",
}

# Normal shipping deadlines are 2–7 days after purchase (median 6). Profiling found 4 rows
# ~1,000 days out (year 2020). 365 days catches exactly those; 60 days would also catch 8 more.
SHIPPING_LIMIT_MAX_DAYS = 365

log = logging.getLogger(__name__)


# ── reading ───────────────────────────────────────────────────────────────────

def read_csv_as_text(path: pathlib.Path) -> pd.DataFrame:
    """
    Read a CSV with every cell as literal text.

    dtype=str               — nothing is guessed: zip "01046" keeps its leading zero, IDs stay text.
    keep_default_na=False   — pandas would otherwise silently turn "N/A", "NA", "null" into NaN
                              (Issue #15: that hid 15 malformed dates). We decide what is null.
    encoding='utf-8-sig'    — strips the invisible BOM at the start of the translation file,
                              which would otherwise corrupt the first column name.
    Alternative: read_csv(parse_dates=..., dtype={'zip': str}) — faster to write, but hides problems.
    """
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


# ── A. standardize text ───────────────────────────────────────────────────────

def standardize_text(
    df: pd.DataFrame, lower_cols: tuple[str, ...] = ()
) -> tuple[pd.DataFrame, dict]:
    """
    Strip spaces on every text column, lowercase the categorical ones, and turn
    empty or whitespace-only strings into real nulls (NaN).

    str.strip()            — removes leading/trailing spaces, tabs, \\r\\n
    .str.lower()           — only for categorical columns (status, payment type, city...);
                             free text (review comments) and IDs keep their case
    .replace('', np.nan)   — after stripping, ' ' becomes '' → NaN, so isna() finally sees it

    Excel: =TRIM(LOWER(A2)) + Find & Replace blanks.  SQL: NULLIF(LOWER(TRIM(col)), '').
    Returns counts: empty cells, whitespace-only cells, and values changed per lower_col
    (e.g. 'DELIVERED' → 'delivered').
    """
    df = df.copy()
    stats = {"empty_cells_to_null": 0, "whitespace_cells_to_null": 0}

    for col in df.columns:
        if not pd.api.types.is_string_dtype(df[col]):
            continue                          # already numeric/datetime: nothing to strip
        original = df[col]
        cleaned  = original.str.strip()

        stats["empty_cells_to_null"]      += int((original == "").sum())
        stats["whitespace_cells_to_null"] += int(((original != "") & (cleaned == "")).sum())

        if col in lower_cols:
            cleaned = cleaned.str.lower()
            # count real values whose text changed (casing or padding), e.g. 'Delivered ' → 'delivered'
            changed = original.notna() & (cleaned != "") & (cleaned != original)
            stats[f"{col}_values_standardized"] = int(changed.sum())

        df[col] = cleaned.replace("", np.nan)

    return df, stats


def remove_accents(s: pd.Series) -> pd.Series:
    """
    'são paulo' → 'sao paulo'.  NFKD splits 'ã' into 'a' + a combining tilde; encoding to
    ASCII with errors='ignore' drops the tilde.  Profiling: 8,011 city spellings → 5,967.
    Excel has no clean equivalent (nested SUBSTITUTE); Postgres has unaccent().
    """
    return s.str.normalize("NFKD").str.encode("ascii", errors="ignore").str.decode("ascii")


# ── B. fix types ──────────────────────────────────────────────────────────────

def parse_dates(df: pd.DataFrame, cols: list[str]) -> tuple[pd.DataFrame, dict]:
    """
    Convert text columns to datetime64 and count malformed values.

    pd.to_datetime(errors='coerce') — a bad value becomes NaT instead of crashing the run.
    errors='raise' would stop the whole pipeline on one typo; errors='ignore' (removed in
    pandas 3) would leave the column as text.

    Malformed = had text before parsing, NaT after.  A value that was already null
    (genuinely missing) is NOT counted.  Adds a row-level flag_malformed_date so the
    affected rows stay traceable.
    Excel: =ISNUMBER(DATEVALUE(A2)).  SQL (SQLite): date(col) IS NULL AND col IS NOT NULL.
    """
    df = df.copy()
    malformed: dict[str, int] = {}
    bad_any_col = pd.Series(False, index=df.index)

    for col in cols:
        parsed = pd.to_datetime(df[col], format=DATE_FORMAT, errors="coerce")
        bad = df[col].notna() & parsed.isna()
        malformed[col] = int(bad.sum())
        bad_any_col |= bad
        df[col] = parsed

    df["flag_malformed_date"] = bad_any_col
    return df, {"malformed_dates": sum(malformed.values()), "malformed_dates_by_col": malformed}


def parse_numbers(df: pd.DataFrame, cols: list[str]) -> tuple[pd.DataFrame, dict]:
    """
    Text → number with pd.to_numeric(errors='coerce'); same "count what became null" logic.
    Excel: =VALUE(A2) / Text to Columns.  SQL: CAST(col AS REAL).
    """
    df = df.copy()
    malformed = 0
    for col in cols:
        parsed = pd.to_numeric(df[col], errors="coerce")
        malformed += int((df[col].notna() & parsed.isna()).sum())
        df[col] = parsed
    return df, {"malformed_numbers": malformed}


# ── C. remove duplicates ──────────────────────────────────────────────────────

def drop_duplicates_two_pass(
    df: pd.DataFrame, key: list[str]
) -> tuple[pd.DataFrame, dict]:
    """
    Pass 1: exact duplicates (every column identical) → drop_duplicates().
    Pass 2: key duplicates (same business key, content differs slightly) → keep ONE copy.

    Which copy survives pass 2?  The most complete one (fewest nulls); ties → the last
    in the file (the guide's keep='last' = "most recent version").  Why: an injected
    copy may carry a corrupted date that became NaT in step B. Plain keep='last' could keep
    the corrupted copy and throw away the good one.

    Runs AFTER standardize_text: otherwise 'DELIVERED' vs 'delivered' copies look different
    and survive (Issue #16: only 69 of 75 order dups were exact).
    Excel: Data → Remove Duplicates (exact only, case-insensitive).
    SQL: ROW_NUMBER() OVER (PARTITION BY key ORDER BY ...) and keep rn = 1.
    """
    n_start = len(df)
    df = df.drop_duplicates()
    exact_removed = n_start - len(df)

    df = (
        df.assign(_filled=df.notna().sum(axis=1))      # non-null cells per row
          .sort_values("_filled", kind="stable")       # stable: ties keep file order
          .drop_duplicates(subset=key, keep="last")    # last = most complete (then latest)
          .sort_index()                                # restore original row order
          .drop(columns="_filled")
    )
    key_removed = n_start - exact_removed - len(df)

    return df, {"exact_duplicates_removed": exact_removed, "key_duplicates_removed": key_removed}


def drop_null_keys(df: pd.DataFrame, key: list[str]) -> tuple[pd.DataFrame, dict]:
    """
    DROP rows whose primary key is missing. That's the one "drop" case: a row with no
    order_id can't be joined, de-duplicated or loaded into a table with a PRIMARY KEY.
    Excel: filter blanks in the ID column → delete.  SQL: WHERE order_id IS NOT NULL.
    """
    mask = df[key].isna().any(axis=1)
    return df[~mask].copy(), {"rows_dropped_null_key": int(mask.sum())}


# ── helpers for stats ─────────────────────────────────────────────────────────

def _count_flags(df: pd.DataFrame) -> dict:
    """Count True values in every flag_ column (pandas NA counts as not flagged)."""
    return {c: int(df[c].fillna(False).sum()) for c in df.columns if c.startswith("flag_")}


def _finish(df: pd.DataFrame, table: str, rows_in: int, stats: dict, t0: float) -> tuple[pd.DataFrame, dict]:
    """I. Assemble the final stats dict (rows in/out + flag counts) and log a one-line summary."""
    df = df.reset_index(drop=True)
    stats = {"table": table, "rows_in": rows_in, **stats, **_count_flags(df),
             "rows_out": len(df), "seconds": round(time.perf_counter() - t0, 2)}
    log.info("cleaned %-12s rows %7d -> %7d  exact dups %d, key dups %d, malformed dates %d",
             table, rows_in, len(df), stats.get("exact_duplicates_removed", 0),
             stats.get("key_duplicates_removed", 0), stats.get("malformed_dates", 0))
    return df, stats


# ── D–H. one function per table ───────────────────────────────────────────────

def clean_orders(df: pd.DataFrame, payments: pd.DataFrame | None = None) -> tuple[pd.DataFrame, dict]:
    """
    Clean orders (raw or monthly).  `payments` (raw text or cleaned) is optional and only
    used for flag_no_payment; without it that flag is NA (unknown), not False.
    """
    t0, rows_in, stats = time.perf_counter(), len(df), {}

    # A. text: order_status is the only categorical column ('Delivered ' → 'delivered')
    df, s = standardize_text(df, lower_cols=("order_status",)); stats.update(s)
    # D. drop: no order_id = unusable row
    df, s = drop_null_keys(df, ["order_id"]); stats.update(s)
    # B. types BEFORE dedupe, so the most-complete-copy rule sees NaT from bad dates
    df, s = parse_dates(df, ORDER_DATE_COLS); stats.update(s)
    # C. exact, then key duplicates
    df, s = drop_duplicates_two_pass(df, ["order_id"]); stats.update(s)

    purchased = df["order_purchase_timestamp"]
    carrier   = df["order_delivered_carrier_date"]
    delivered = df["order_delivered_customer_date"]
    estimated = df["order_estimated_delivery_date"]
    status    = df["order_status"]

    # D. missing values (no filling in orders, because a fake date would corrupt delivery KPIs):
    #  - order_delivered_customer_date: KEEP null. Missing = not delivered, which is real information.
    #    is_delivered = status says delivered AND we have the date; the conflicting cases are flagged below.
    #  - order_approved_at / order_delivered_carrier_date: KEEP null (few rows; dropping loses real orders/revenue).
    df["is_delivered"] = (status == "delivered") & delivered.notna()

    # G. logic checks: FLAG, never delete (keep the revenue; let the business decide; exclude from delivery KPIs)
    # Comparisons with NaT return False, so a missing date never raises a flag by accident.
    df["flag_unknown_status"]          = status.notna() & ~status.isin(CANONICAL_STATUSES)
    df["flag_pickup_before_purchase"]  = carrier < purchased            # raw: 166
    df["flag_delivered_before_pickup"] = delivered < carrier            # raw: 23
    df["flag_canceled_with_delivery"]  = (status == "canceled") & delivered.notna()   # raw: 6
    df["flag_delivered_no_date"]       = (status == "delivered") & delivered.isna()   # raw: 8 (+ coerced bad dates)
    if payments is not None:
        # anti-join: order_id not in payments.  Excel: XLOOKUP → #N/A.  SQL: LEFT JOIN … WHERE p.order_id IS NULL
        df["flag_no_payment"] = ~df["order_id"].isin(payments["order_id"])           # raw: 1
    else:
        df["flag_no_payment"] = pd.Series(pd.NA, index=df.index, dtype="boolean")

    # H. derived columns
    df["purchase_month"] = purchased.dt.to_period("M").astype(str)          # '2017-11'
    df["delivery_days"]  = (delivered - purchased).dt.days.astype("Int64")  # Int64 = integer that allows NA
    # is_late: compare calendar days (the estimate is a date at 00:00; delivering at 15:00 ON the
    # estimated day is on time). Undelivered orders → NA, not False, so they don't dilute the late rate.
    late = delivered.dt.normalize() > estimated.dt.normalize()
    df["is_late"] = late.astype("boolean").where(delivered.notna() & estimated.notna(), pd.NA)

    return _finish(df, "orders", rows_in, stats, t0)


def clean_items(df: pd.DataFrame, orders: pd.DataFrame | None = None) -> tuple[pd.DataFrame, dict]:
    """
    Clean order_items.  `orders` (cleaned, with datetime purchase timestamp) is optional and
    only used for flag_shipping_limit_far_future; without it that flag is NA.
    """
    t0, rows_in, stats = time.perf_counter(), len(df), {}
    key = ["order_id", "order_item_id"]                  # grain: one row per item line in an order

    df, s = standardize_text(df); stats.update(s)        # no categorical text columns here
    df, s = drop_null_keys(df, key); stats.update(s)
    df, s = parse_dates(df, ["shipping_limit_date"]); stats.update(s)
    df, s = parse_numbers(df, ["order_item_id", "price", "freight_value"]); stats.update(s)
    df["order_item_id"] = df["order_item_id"].astype("Int64")
    df, s = drop_duplicates_two_pass(df, key); stats.update(s)

    # D. price / freight: no nulls in Olist; if one appeared we'd KEEP it null (a guessed price corrupts revenue).

    # G. flags
    df["flag_price_not_positive"] = df["price"] <= 0
    if orders is not None:
        # map() = a lookup: order_id → purchase time.  Excel: XLOOKUP.  SQL: JOIN orders
        purchase = df["order_id"].map(orders.set_index("order_id")["order_purchase_timestamp"])
        days_to_limit = (df["shipping_limit_date"] - purchase).dt.days
        df["flag_shipping_limit_far_future"] = days_to_limit > SHIPPING_LIMIT_MAX_DAYS   # raw: 4 (year 2020)
    else:
        df["flag_shipping_limit_far_future"] = pd.Series(pd.NA, index=df.index, dtype="boolean")

    # H. derived: what the customer paid for this line.  Excel: =F2+G2.  SQL: price + freight_value
    df["total_item_value"] = df["price"] + df["freight_value"]

    return _finish(df, "order_items", rows_in, stats, t0)


def clean_payments(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Clean order_payments (one row per payment method used in an order)."""
    t0, rows_in, stats = time.perf_counter(), len(df), {}
    key = ["order_id", "payment_sequential"]

    df, s = standardize_text(df, lower_cols=("payment_type",)); stats.update(s)
    df, s = drop_null_keys(df, key); stats.update(s)
    df, s = parse_numbers(df, ["payment_sequential", "payment_installments", "payment_value"]); stats.update(s)
    df, s = drop_duplicates_two_pass(df, key); stats.update(s)

    # G. flag first (so the original problem stays visible), then D. fix
    df["flag_installments_was_zero"]    = df["payment_installments"] == 0      # raw: 2
    df["flag_payment_type_not_defined"] = df["payment_type"] == "not_defined"  # raw: 3, keep (unknown method)
    df["flag_zero_payment_value"]       = df["payment_value"] == 0             # raw: 9, likely full vouchers, keep
    # D. FILL: 0 installments is impossible (a payment is at least 1 installment) → set to 1.
    df.loc[df["flag_installments_was_zero"], "payment_installments"] = 1
    stats["installments_zero_set_to_one"] = int(df["flag_installments_was_zero"].sum())
    for c in ("payment_sequential", "payment_installments"):
        df[c] = df[c].astype("Int64")

    return _finish(df, "payments", rows_in, stats, t0)


def clean_reviews(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """
    Clean order_reviews.

    Two different duplicate problems:
      1. Same review_id + order_id twice = a true duplicate (the injected kind) → C. two-pass dedupe.
      2. Same order with several different reviews (raw: 547 orders) = real source behaviour.
         GRAIN DECISION: keep ONE review per order, the latest answered (the customer's final opinion),
         so the "late delivery vs review score" KPI counts each order once.
         Alternative: keep all reviews and pick the latest in SQL with ROW_NUMBER(). That keeps history,
         but every query then has to remember to de-duplicate.
      A review_id shared by several orders (one review covering a multi-order checkout) is KEPT.
    """
    t0, rows_in, stats = time.perf_counter(), len(df), {}

    # A. strips comments, so ' ' and '\r\n' become null. Comments keep their case (free text, not categorical).
    df, s = standardize_text(df); stats.update(s)
    df, s = drop_null_keys(df, ["review_id", "order_id"]); stats.update(s)
    df, s = parse_dates(df, ["review_creation_date", "review_answer_timestamp"]); stats.update(s)
    df, s = parse_numbers(df, ["review_score"]); stats.update(s)
    df["review_score"] = df["review_score"].astype("Int64")
    df, s = drop_duplicates_two_pass(df, ["review_id", "order_id"]); stats.update(s)

    # Grain: latest review per order. sort + drop_duplicates(keep='last') = SQL ROW_NUMBER() … ORDER BY ts DESC → rn = 1
    n = len(df)
    df = (df.sort_values(["order_id", "review_answer_timestamp"], kind="stable")
            .drop_duplicates(subset="order_id", keep="last")
            .sort_index())
    stats["extra_reviews_per_order_removed"] = n - len(df)

    # D. review_comment_title / message: LEAVE null. Optional fields; most customers don't write one.
    #    Filling with 'no comment' would make text analysis count fake comments.
    df["has_comment"] = df["review_comment_message"].notna()

    # G. flag scores outside 1–5
    df["flag_invalid_score"] = ~df["review_score"].between(1, 5).fillna(False)

    return _finish(df, "reviews", rows_in, stats, t0)


def clean_products(df: pd.DataFrame, translation: pd.DataFrame | None = None) -> tuple[pd.DataFrame, dict]:
    """
    Clean products and add English category names.
    `translation` = raw product_category_name_translation (read from data/raw if not given).
    """
    t0, rows_in, stats = time.perf_counter(), len(df), {}

    # E. fix the misspelled source columns (Olist's 'lenght').  Excel: edit header.  SQL: ALTER TABLE … RENAME COLUMN
    df = df.rename(columns={
        "product_name_lenght":        "product_name_length",
        "product_description_lenght": "product_description_length",
    })

    df, s = standardize_text(df, lower_cols=("product_category_name",)); stats.update(s)
    df, s = drop_null_keys(df, ["product_id"]); stats.update(s)
    num_cols = ["product_name_length", "product_description_length", "product_photos_qty",
                "product_weight_g", "product_length_cm", "product_height_cm", "product_width_cm"]
    df, s = parse_numbers(df, num_cols); stats.update(s)
    df, s = drop_duplicates_two_pass(df, ["product_id"]); stats.update(s)

    # F. enrich: merge English names.
    if translation is None:
        translation = read_csv_as_text(RAW / "product_category_name_translation.csv")
    translation, _ = standardize_text(
        translation, lower_cols=("product_category_name", "product_category_name_english"))
    translation = translation.drop_duplicates(subset="product_category_name")
    # how='left' keeps every product (inner would drop the untranslated ones).
    # validate='many_to_one' raises an error if a category appeared twice in the lookup,
    # which would duplicate product rows (join fan-out).
    # Excel: XLOOKUP.  SQL: LEFT JOIN translation USING (product_category_name)
    n_before = len(df)
    df = df.merge(translation, on="product_category_name", how="left", validate="many_to_one")
    assert len(df) == n_before, "merge changed the row count"

    # G. flag before filling, so the original gap stays visible
    df["flag_missing_category"]      = df["product_category_name"].isna()                    # raw: 610
    df["flag_untranslated_category"] = (df["product_category_name"].notna()
                                        & df["product_category_name_english"].isna())        # raw: 13 products
    # D. FILL missing category with 'unknown': keeps those products' revenue in category totals
    #    (dropping them would understate sales). It shows up as its own 'unknown' bar in the report.
    df["product_category_name"] = df["product_category_name"].fillna("unknown")
    # FILL untranslated with the Portuguese name (e.g. 'pc_gamer'): better than 'unknown' because the
    # category stays distinct and recognisable. The real fix belongs in the lookup file.
    df["product_category_name_english"] = df["product_category_name_english"].fillna(df["product_category_name"])
    stats["categories_filled_unknown"]    = int(df["flag_missing_category"].sum())
    stats["categories_filled_portuguese"] = int(df["flag_untranslated_category"].sum())
    # Product dimensions / name length: LEAVE null. Not used by any KPI. Option: fill with the category
    # median (groupby('category')['weight'].transform('median')) if shipping-cost analysis needed them.

    return _finish(df, "products", rows_in, stats, t0)


def _clean_location(df: pd.DataFrame, prefix: str, key: str, table: str) -> tuple[pd.DataFrame, dict]:
    """Shared logic for customers and sellers: zip as 5-character text, tidy city, uppercase state."""
    t0, rows_in, stats = time.perf_counter(), len(df), {}
    zip_col, city_col, state_col = f"{prefix}_zip_code_prefix", f"{prefix}_city", f"{prefix}_state"

    df, s = standardize_text(df, lower_cols=(city_col,)); stats.update(s)
    df, s = drop_null_keys(df, [key]); stats.update(s)
    df, s = drop_duplicates_two_pass(df, [key]); stats.update(s)

    # Zip stays TEXT (read with dtype=str). zfill(5) restores a leading zero if a file ever lost it:
    # '1046' → '01046'.  Excel would show 1046 (a number). Fix there: format as Text or =TEXT(A2,"00000")
    df[zip_col] = df[zip_col].str.zfill(5)
    # 'são paulo' → 'sao paulo', so the same city isn't counted twice
    before = df[city_col].copy()
    df[city_col] = remove_accents(df[city_col])
    stats["city_accents_removed"] = int((before != df[city_col]).fillna(False).sum())
    df[state_col] = df[state_col].str.upper()             # 'sp' → 'SP'.  Excel: =UPPER()

    return _finish(df, table, rows_in, stats, t0)


def clean_customers(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Clean customers. Grain: one row per customer_id (= per order; the real person is customer_unique_id)."""
    return _clean_location(df, "customer", "customer_id", "customers")


def clean_sellers(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Clean sellers. Grain: one row per seller_id."""
    return _clean_location(df, "seller", "seller_id", "sellers")


# ── batch runners (used by the test now, and by the orchestrator in Step 3.2) ─

MONTH_TABLES = ("orders", "order_items", "payments", "reviews")   # the 4 files in every monthly batch


def find_month_file(
    table: str, month: str, folders: tuple[pathlib.Path, ...] = (INCOMING, HOLDBACK, ARCHIVE)
) -> pathlib.Path:
    """
    Locate <table>_<YYYY_MM>.csv in the first folder that has it (read-only).
    Default search: incoming → holdback → archive, so tests still find a month after
    the loader (Step 2.2) has moved it to archive. The loader passes folders=(INCOMING,)
    so it only ever cleans the exact files it is about to load and move.
    """
    for folder in folders:
        path = folder / f"{table}_{month}.csv"
        if path.exists():
            return path
    names = ", ".join(f.name for f in folders)
    raise FileNotFoundError(f"{table}_{month}.csv not found in {names}")


def clean_month(
    month: str, folders: tuple[pathlib.Path, ...] = (INCOMING, HOLDBACK, ARCHIVE)
) -> dict[str, tuple[pd.DataFrame, dict]]:
    """
    Clean one monthly batch (4 files). Payments are cleaned first so orders can flag
    'no payment'; orders before items so items can flag far-future shipping deadlines.
    `folders` = where to look for the files (see find_month_file).
    Returns {table: (clean_df, stats)}.
    """
    raw = {t: read_csv_as_text(find_month_file(t, month, folders)) for t in MONTH_TABLES}
    payments = clean_payments(raw["payments"])
    orders   = clean_orders(raw["orders"], payments=payments[0])
    items    = clean_items(raw["order_items"], orders=orders[0])
    reviews  = clean_reviews(raw["reviews"])
    return {"orders": orders, "order_items": items, "payments": payments, "reviews": reviews}


def clean_reference() -> dict[str, tuple[pd.DataFrame, dict]]:
    """Clean all 7 raw Olist tables in data/raw (the full history + dimension tables)."""
    def r(name: str) -> pd.DataFrame:
        return read_csv_as_text(RAW / f"olist_{name}_dataset.csv")

    payments = clean_payments(r("order_payments"))
    orders   = clean_orders(r("orders"), payments=payments[0])
    return {
        "orders":      orders,
        "order_items": clean_items(r("order_items"), orders=orders[0]),
        "payments":    payments,
        "reviews":     clean_reviews(r("order_reviews")),
        "products":    clean_products(r("products")),
        "customers":   clean_customers(r("customers")),
        "sellers":     clean_sellers(r("sellers")),
    }


def main() -> None:
    """CLI: no argument → raw reference tables; YYYY_MM → that monthly batch. Logs stats only."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    t0 = time.perf_counter()
    results = clean_month(sys.argv[1]) if len(sys.argv) > 1 else clean_reference()
    for table, (_, stats) in results.items():
        log.info("%s stats: %s", table, stats)
    log.info("done in %.1f s", time.perf_counter() - t0)


if __name__ == "__main__":
    main()
