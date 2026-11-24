"""
explore.py  —  Step 1.3
Profiles every raw Olist table (data/raw/) and one sample month from data/incoming/
BEFORE any cleaning, so we can turn "the data is messy" into specific numbers.

Per table: shape, dtypes, null count & %, unique counts, duplicate rows, duplicate keys,
value_counts for categorical columns, describe() for numeric columns, date min/max
(+ unparseable dates), and disguised blanks (whitespace-only / "N/A"-style strings).
Across tables: orphan keys (child rows whose key has no parent) and business-logic checks.
For the sample month: a reconciliation against data/mess_log.csv.

Output: reports/data_profile.txt   (progress messages go to the log, not the report)
Run:    python src/explore.py
"""

import logging
import pathlib
import time

import pandas as pd

# ── paths & constants ─────────────────────────────────────────────────────────
ROOT     = pathlib.Path(__file__).resolve().parent.parent   # DA-Project/
RAW      = ROOT / "data" / "raw"
INCOMING = ROOT / "data" / "incoming"
MESS_LOG = ROOT / "data" / "mess_log.csv"
OUT_FILE = ROOT / "reports" / "data_profile.txt"

SAMPLE_MONTH = "2017_11"     # busiest month (Black Friday) — has every mess type injected
DATE_FORMAT  = "%Y-%m-%d %H:%M:%S"   # the format every Olist timestamp uses
TOP_N_VALUES = 30            # max rows shown per value_counts

# Strings that LOOK like "missing" but are not real nulls in the file.
# Note: pandas' read_csv already turns most of these into NaN by default (na_values),
# which hides them — that's why we re-read each file as raw text to count them.
PLACEHOLDER_TOKENS = {"N/A", "n/a", "NA", "null", "NULL", "None", "none", "nan", "NaN", "-", "?"}

ORDER_DATES = [
    "order_purchase_timestamp", "order_approved_at", "order_delivered_carrier_date",
    "order_delivered_customer_date", "order_estimated_delivery_date",
]

# One spec per table: which columns are keys, dates and categories.
# "keys" is a list of candidate keys — each one is checked for duplicates.
TABLES = {
    "orders":       {"file": "olist_orders_dataset.csv",
                     "keys": [["order_id"]], "dates": ORDER_DATES, "cats": ["order_status"]},
    "order_items":  {"file": "olist_order_items_dataset.csv",
                     "keys": [["order_id", "order_item_id"]], "dates": ["shipping_limit_date"],
                     "cats": ["order_item_id"]},
    "payments":     {"file": "olist_order_payments_dataset.csv",
                     "keys": [["order_id", "payment_sequential"]], "dates": [],
                     "cats": ["payment_type", "payment_installments"]},
    "reviews":      {"file": "olist_order_reviews_dataset.csv",
                     "keys": [["review_id"], ["order_id"], ["review_id", "order_id"]],
                     "dates": ["review_creation_date", "review_answer_timestamp"],
                     "cats": ["review_score"]},
    "customers":    {"file": "olist_customers_dataset.csv",
                     "keys": [["customer_id"], ["customer_unique_id"]], "dates": [],
                     "cats": ["customer_state"]},
    "products":     {"file": "olist_products_dataset.csv",
                     "keys": [["product_id"]], "dates": [], "cats": ["product_category_name"]},
    "sellers":      {"file": "olist_sellers_dataset.csv",
                     "keys": [["seller_id"]], "dates": [], "cats": ["seller_state"]},
    "geolocation":  {"file": "olist_geolocation_dataset.csv",
                     "keys": [["geolocation_zip_code_prefix"]], "dates": [],
                     "cats": ["geolocation_state"]},
    "category_translation": {"file": "product_category_name_translation.csv",
                     "keys": [["product_category_name"]], "dates": [], "cats": []},
}
FACT_TABLES = ["orders", "order_items", "payments", "reviews"]   # the ones split into monthly files

# How each profiling check is done in pandas / Excel / SQL — printed at the top of the report.
CHECK_LEGEND = [
    ("Size",             "df.shape",                         "status-bar count; =COUNTA(A:A)-1",          "SELECT COUNT(*) FROM t"),
    ("Column types",     "df.dtypes",                        "=ISNUMBER(A2) / =ISTEXT(A2); text sits left","SQLite: typeof(col); others: INFORMATION_SCHEMA.COLUMNS"),
    ("Nulls (count, %)", "df.isna().sum(), .mean()*100",     "=COUNTBLANK(G:G); Filter -> (Blanks)",      "COUNT(*) - COUNT(col)"),
    ("Unique counts",    "df.nunique()",                     "=COUNTA(UNIQUE(A2:A99999))",                "COUNT(DISTINCT col)"),
    ("Duplicate rows",   "df.duplicated().sum()",            "Data -> Remove Duplicates (all columns)",   "GROUP BY all cols HAVING COUNT(*) > 1"),
    ("Duplicate keys",   "df.duplicated(subset=[key]).sum()","=COUNTIF(A:A,A2)>1",                        "GROUP BY key HAVING COUNT(*) > 1"),
    ("Category values",  "value_counts(dropna=False)",       "PivotTable / Filter dropdown",              "SELECT col, COUNT(*) ... GROUP BY col"),
    ("Numeric ranges",   "df.describe()",                    "=MIN/=MAX/=AVERAGE; Descriptive Statistics","MIN(), MAX(), AVG()"),
    ("Date range/valid", "to_datetime(errors='coerce')",     "=MIN/=MAX on dates; =ISNUMBER(A2)",         "MIN(col), MAX(col); date(col) IS NULL"),
    ("Disguised blanks", "s.str.strip() == ''",              "=LEN(TRIM(A2))=0",                          "TRIM(col) = ''"),
    ("Orphan keys",      "~child[k].isin(parent[k])",        "XLOOKUP key into parent -> #N/A",           "LEFT JOIN parent ... WHERE parent.k IS NULL"),
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)


# ── loading ───────────────────────────────────────────────────────────────────

def load_table(path: pathlib.Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Read one CSV twice:
      df   — the normal pandas read (type inference, default NaN handling). This is
             what an analyst would see, so we profile dtypes/nulls on it.
      text — every cell as a literal string (dtype=str, keep_default_na=False), so
             nothing is converted to NaN. Used to find disguised blanks like ' ' or 'N/A'
             that the normal read either keeps as text or silently turns into NaN.
    """
    df = pd.read_csv(path, low_memory=False)
    text = pd.read_csv(path, dtype=str, keep_default_na=False)
    return df, text


# ── per-table profiling ───────────────────────────────────────────────────────

def fmt(obj) -> str:
    """Render a DataFrame/Series as plain text, indented for readability."""
    return "\n".join("    " + line for line in obj.to_string().splitlines())


def column_summary(df: pd.DataFrame) -> pd.DataFrame:
    """One row per column: dtype, non-null count, nulls, null %, distinct values."""
    return pd.DataFrame({
        "dtype":    df.dtypes.astype(str),
        "non_null": df.notna().sum(),
        "nulls":    df.isna().sum(),              # = Excel COUNTBLANK = SQL COUNT(*)-COUNT(col)
        "null_%":   (df.isna().mean() * 100).round(2),
        "unique":   df.nunique(),                 # = SQL COUNT(DISTINCT col)
    })


def duplicate_key_lines(df: pd.DataFrame, keys: list[list[str]]) -> list[str]:
    """
    For each candidate key: how many rows repeat a key already seen, and how many
    distinct key values are affected. keep=False marks EVERY copy (not just the extras).
    """
    lines = []
    for key in keys:
        extra = int(df.duplicated(subset=key).sum())
        affected = int(df.loc[df.duplicated(subset=key, keep=False), key].drop_duplicates().shape[0])
        lines.append(f"    {'+'.join(key):<40} extra rows={extra:>8,}   distinct keys affected={affected:>7,}")
    return lines


def value_count_lines(df: pd.DataFrame, cols: list[str]) -> list[str]:
    """
    value_counts(dropna=False) per categorical column. Labels are shown with repr()
    so hidden whitespace is visible: 'delivered ' vs 'delivered'.
    """
    lines = []
    for col in cols:
        vc = df[col].value_counts(dropna=False)
        pct = (vc / len(df) * 100).round(2)
        table = pd.DataFrame({"count": vc, "%": pct})
        table.index = [repr(v) for v in table.index]
        shown = f" (top {TOP_N_VALUES} of {len(vc)})" if len(vc) > TOP_N_VALUES else ""
        lines += [f"  value_counts: {col}{shown}", fmt(table.head(TOP_N_VALUES)), ""]
    return lines


def date_lines(df: pd.DataFrame, cols: list[str]) -> list[str]:
    """
    Parse each date column with errors='coerce': valid strings -> Timestamp, anything
    unparseable -> NaT. 'unparseable' = cells that had a value but failed to parse.
    """
    rows, bad_examples = [], []
    for col in cols:
        parsed = pd.to_datetime(df[col], format=DATE_FORMAT, errors="coerce")
        bad = df[col].notna() & parsed.isna()
        rows.append({"column": col, "non_null": int(df[col].notna().sum()),
                     "unparseable": int(bad.sum()), "min": parsed.min(), "max": parsed.max()})
        if bad.any():
            bad_examples.append(f"    {col}: {sorted(df.loc[bad, col].unique().tolist())}")
    lines = [fmt(pd.DataFrame(rows).set_index("column"))]
    if bad_examples:
        lines += ["  unparseable values found:"] + bad_examples
    return lines


def disguised_blank_lines(text: pd.DataFrame) -> list[str]:
    """
    On the raw-text read, count per column:
      empty      — truly empty cells ('' -> pandas reads these as NaN, so isna() sees them)
      whitespace — non-empty but only spaces, e.g. ' '   (isna() does NOT see these)
      placeholder— 'N/A', 'null', '-' ... (pandas silently turns most into NaN on a normal read)
    Only columns with a whitespace or placeholder hit are printed.
    """
    rows = []
    for col in text.columns:
        s = text[col]
        stripped = s.str.strip()
        rows.append({"column": col,
                     "empty": int((s == "").sum()),
                     "whitespace": int(((s != "") & (stripped == "")).sum()),
                     "placeholder": int(stripped.isin(PLACEHOLDER_TOKENS).sum())})
    table = pd.DataFrame(rows).set_index("column")
    hits = table[(table["whitespace"] > 0) | (table["placeholder"] > 0)]
    if hits.empty:
        return ["    none found (no whitespace-only or placeholder strings)"]
    return [fmt(hits)]


def profile_table(name: str, df: pd.DataFrame, text: pd.DataFrame, spec: dict) -> list[str]:
    """Run every per-table check and return the report lines for one table."""
    lines = ["", "=" * 100, f"TABLE: {name}   ({spec['file']})", "=" * 100,
             f"  shape: {df.shape[0]:,} rows x {df.shape[1]} columns"]

    typos = [c for c in df.columns if "lenght" in c]
    if typos:
        lines.append(f"  misspelled column names: {typos}")

    lines += ["", "  columns (dtype / nulls / unique):", fmt(column_summary(df)), ""]
    lines.append(f"  exact duplicate rows: {int(df.duplicated().sum()):,}")
    lines += ["  duplicate keys:"] + duplicate_key_lines(df, spec["keys"]) + [""]

    if spec["cats"]:
        lines += value_count_lines(df, spec["cats"])

    numeric = df.select_dtypes("number")
    if not numeric.empty:
        lines += ["  describe() — numeric columns:", fmt(numeric.describe().T.round(2)), ""]

    if spec["dates"]:
        lines += ["  date columns:"] + date_lines(df, spec["dates"]) + [""]

    lines += ["  disguised blanks (raw text read):"] + disguised_blank_lines(text)
    return lines


# ── cross-table checks ────────────────────────────────────────────────────────

def orphan_lines(dfs: dict[str, pd.DataFrame], relations: list[tuple]) -> list[str]:
    """
    For each (child, child_col, parent, parent_col): rows in the child whose key is not
    in the parent. isin() is the pandas anti-join (= SQL LEFT JOIN ... WHERE p.key IS NULL).
    Nulls in the child key are skipped — a missing key is a null problem, not an orphan.
    """
    rows = []
    for child, ccol, parent, pcol in relations:
        keys = dfs[child][ccol].dropna()
        orphan = ~keys.isin(dfs[parent][pcol])
        rows.append({"relationship": f"{child}.{ccol} -> {parent}.{pcol}",
                     "orphan_rows": int(orphan.sum()),
                     "distinct_orphan_keys": int(keys[orphan].nunique())})
    return [fmt(pd.DataFrame(rows).set_index("relationship"))]


def business_logic_lines(dfs: dict[str, pd.DataFrame]) -> list[str]:
    """
    Check 10 of the playbook: 'ask why'. Each check separates expected behaviour
    (e.g. canceled orders with no delivery date) from real errors.
    """
    o = dfs["orders"].copy()
    for c in ORDER_DATES:
        o[c] = pd.to_datetime(o[c], format=DATE_FORMAT, errors="coerce")
    items, pay, rev, prod, geo = (dfs[t] for t in
                                  ("order_items", "payments", "reviews", "products", "geolocation"))
    lines = []

    no_delivery = o["order_delivered_customer_date"].isna()
    lines += [f"  orders with no customer delivery date: {int(no_delivery.sum()):,} — by status:",
              fmt(o.loc[no_delivery, "order_status"].value_counts()), ""]
    lines.append(f"  status 'delivered' but NO delivery date:       "
                 f"{int((no_delivery & (o.order_status == 'delivered')).sum()):,}")
    lines.append(f"  status 'canceled' but HAS a delivery date:     "
                 f"{int((~no_delivery & (o.order_status == 'canceled')).sum()):,}")
    lines.append(f"  delivered to customer BEFORE purchase:         "
                 f"{int((o.order_delivered_customer_date < o.order_purchase_timestamp).sum()):,}")
    lines.append(f"  handed to carrier BEFORE purchase:             "
                 f"{int((o.order_delivered_carrier_date < o.order_purchase_timestamp).sum()):,}")
    lines.append(f"  approved BEFORE purchase:                      "
                 f"{int((o.order_approved_at < o.order_purchase_timestamp).sum()):,}")
    lines.append(f"  delivered to customer BEFORE carrier pickup:   "
                 f"{int((o.order_delivered_customer_date < o.order_delivered_carrier_date).sum()):,}")
    lines.append("")

    lines.append(f"  items with price <= 0:                         {int((items.price <= 0).sum()):,}")
    lines.append(f"  items with freight_value = 0 (free shipping?): {int((items.freight_value == 0).sum()):,}")
    lines.append(f"  payments with installments = 0:                {int((pay.payment_installments == 0).sum()):,}")
    lines.append(f"  payments with payment_type 'not_defined':      {int((pay.payment_type == 'not_defined').sum()):,}")
    lines.append(f"  payments with payment_value = 0:               {int((pay.payment_value == 0).sum()):,}")
    lines.append("")

    # Payment total vs item total per order (a Step 2.4 check, previewed here).
    # Aggregate BOTH sides to one row per order first, then join — avoids join fan-out.
    item_tot = (items.price + items.freight_value).groupby(items.order_id).sum()
    pay_tot = pay.groupby("order_id")["payment_value"].sum()
    both = pd.concat({"items": item_tot, "paid": pay_tot}, axis=1, join="inner")
    diff = (both.paid - both["items"]).abs()
    lines.append(f"  orders where payments differ from items+freight by > R$1: {int((diff > 1).sum()):,}"
                 f"  (of {len(both):,} orders with both; max diff R${diff.max():,.2f})")
    lines.append("")

    per_order = rev.groupby("order_id")["review_id"].nunique()
    per_review = rev.groupby("review_id")["order_id"].nunique()
    lines.append(f"  orders with > 1 review:                        {int((per_order > 1).sum()):,}")
    lines.append(f"  review_ids reused across > 1 order:            {int((per_review > 1).sum()):,}")
    lines.append("")

    no_cat = prod.product_category_name.isna()
    lines.append(f"  products with no category:                     {int(no_cat.sum()):,}"
                 f"  (of these, also missing name length: {int(prod.loc[no_cat, 'product_name_lenght'].isna().sum()):,})")
    missing_tr = sorted(set(prod.product_category_name.dropna())
                        - set(dfs["category_translation"].product_category_name))
    lines.append(f"  categories with no English translation:        {missing_tr}")
    lines.append("")

    # City spelling: how many distinct names collapse once case, spaces and accents are normalised.
    city = geo.geolocation_city.astype(str)
    norm = (city.str.lower().str.strip().str.normalize("NFKD")
            .str.encode("ascii", "ignore").str.decode("ascii"))
    lines.append(f"  geolocation: {len(geo):,} rows, {geo.geolocation_zip_code_prefix.nunique():,} distinct zip prefixes")
    lines.append(f"  geolocation city names: {city.nunique():,} raw spellings -> {norm.nunique():,} "
                 "after lower/strip/remove accents")
    return lines


def mess_reconciliation_lines(sample: dict[str, pd.DataFrame],
                              sample_text: dict[str, pd.DataFrame]) -> list[str]:
    """
    Compare what make_batches.py says it injected (mess_log.csv) with what the
    profiling actually detects in the sample month. Proves the checks can see the mess.
    """
    mess = pd.read_csv(MESS_LOG)
    mess = mess[mess["month"] == SAMPLE_MONTH]
    rows = []
    for _, m in mess.iterrows():
        t, kind = m["table"], m["mess_type"]
        df, text = sample[t], sample_text[t]
        key = TABLES[t]["keys"][0]
        if kind == "duplicate_rows":
            found = (f"exact dup rows={int(df.duplicated().sum())}, "
                     f"dup keys={int(df.duplicated(subset=key).sum())}")
        elif kind == "status_casing":
            s = df["order_status"]
            found = f"non-canonical values={int((s != s.str.strip().str.lower()).sum())}"
        elif kind == "malformed_dates":
            cols = ["order_approved_at", "order_delivered_customer_date"]
            seen = sum(int((df[c].notna() & pd.to_datetime(df[c], format=DATE_FORMAT,
                                                             errors="coerce").isna()).sum()) for c in cols)
            true = sum(int(((text[c] != "") & pd.to_datetime(text[c], format=DATE_FORMAT,
                                                              errors="coerce").isna()).sum()) for c in cols)
            found = f"unparseable in raw text={true}; visible after default read_csv={seen}"
        elif kind == "blank_strings":
            cols = ["review_comment_title", "review_comment_message"]
            ws = sum(int(((text[c] != "") & (text[c].str.strip() == "")).sum()) for c in cols)
            found = f"whitespace-only cells={ws}"
        else:
            found = "n/a"
        rows.append({"file": m["file"], "mess_type": kind, "injected": int(m["affected_rows"]),
                     "detected": found})
    return [fmt(pd.DataFrame(rows).set_index("file"))]


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    """Load raw + sample-month tables, profile them, write reports/data_profile.txt."""
    start = time.perf_counter()
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    raw, raw_text = {}, {}
    for name, spec in TABLES.items():
        log.info("Loading raw %s …", name)
        raw[name], raw_text[name] = load_table(RAW / spec["file"])

    sample, sample_text = {}, {}
    for name in FACT_TABLES:
        path = INCOMING / f"{name}_{SAMPLE_MONTH}.csv"
        log.info("Loading sample %s", path.name)
        sample[name], sample_text[name] = load_table(path)

    lines = ["OLIST DATA PROFILE  —  Step 1.3  (generated by src/explore.py)",
             f"Raw tables: data/raw/ ({len(TABLES)} files)   Sample month: data/incoming/*_{SAMPLE_MONTH}.csv",
             "", "HOW EACH CHECK MAPS ACROSS TOOLS", "-" * 100]
    for check, pandas_way, excel_way, sql_way in CHECK_LEGEND:
        lines.append(f"  {check:<17} pandas: {pandas_way:<34} Excel: {excel_way:<45} SQL: {sql_way}")

    lines += ["", "#" * 100, "PART 1 — RAW TABLES (data/raw)", "#" * 100]
    for name, spec in TABLES.items():
        log.info("Profiling raw %s", name)
        lines += profile_table(name, raw[name], raw_text[name], spec)

    lines += ["", "=" * 100, "ORPHAN KEYS — raw (child key not found in parent)", "=" * 100]
    lines += orphan_lines(raw, [
        ("order_items", "order_id",   "orders", "order_id"),
        ("payments",    "order_id",   "orders", "order_id"),
        ("reviews",     "order_id",   "orders", "order_id"),
        ("orders",      "customer_id", "customers", "customer_id"),
        ("order_items", "product_id", "products", "product_id"),
        ("order_items", "seller_id",  "sellers", "seller_id"),
        ("products",    "product_category_name", "category_translation", "product_category_name"),
        ("customers",   "customer_zip_code_prefix", "geolocation", "geolocation_zip_code_prefix"),
        ("sellers",     "seller_zip_code_prefix",   "geolocation", "geolocation_zip_code_prefix"),
        # reverse direction: parents with no children
        ("orders",      "order_id",   "order_items", "order_id"),
        ("orders",      "order_id",   "payments", "order_id"),
        ("orders",      "order_id",   "reviews", "order_id"),
    ])

    lines += ["", "=" * 100, "BUSINESS-LOGIC CHECKS — raw (is it an error or expected?)", "=" * 100]
    lines += business_logic_lines(raw)

    lines += ["", "#" * 100, f"PART 2 — SAMPLE MONTH {SAMPLE_MONTH} (data/incoming, mess injected)", "#" * 100]
    for name in FACT_TABLES:
        spec = {**TABLES[name], "file": f"{name}_{SAMPLE_MONTH}.csv"}
        log.info("Profiling sample %s", spec["file"])
        lines += profile_table(f"{name}_{SAMPLE_MONTH}", sample[name], sample_text[name], spec)

    # Sample tables are keyed by their plain names in `sample`; add raw dimensions for lookups.
    lookup = {**{f"{t}_m": df for t, df in sample.items()}, **raw}
    lines += ["", "=" * 100, f"ORPHAN KEYS — sample month {SAMPLE_MONTH}", "=" * 100]
    lines += orphan_lines(lookup, [
        ("order_items_m", "order_id",    "orders_m", "order_id"),
        ("payments_m",    "order_id",    "orders_m", "order_id"),
        ("reviews_m",     "order_id",    "orders_m", "order_id"),
        ("orders_m",      "customer_id", "customers", "customer_id"),
        ("order_items_m", "product_id",  "products", "product_id"),
        ("order_items_m", "seller_id",   "sellers", "seller_id"),
        ("orders_m",      "order_id",    "order_items_m", "order_id"),
        ("orders_m",      "order_id",    "payments_m", "order_id"),
    ])

    lines += ["", "=" * 100, f"MESS LOG RECONCILIATION — {SAMPLE_MONTH} (injected vs detected)", "=" * 100]
    lines += mess_reconciliation_lines(sample, sample_text)

    elapsed = time.perf_counter() - start
    lines += ["", f"Profile generated in {elapsed:.1f} s"]
    OUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    log.info("Wrote %s (%d lines) in %.1f s", OUT_FILE.relative_to(ROOT), len(lines), elapsed)


if __name__ == "__main__":
    main()
