"""
test_clean.py  —  Step 1.4, checklist J: prove the cleaning worked.

Runs clean.py on one or more monthly batches and reconciles the stats against
data/mess_log.csv (the "answer key" of injected issues). Plain Python, no pytest.

  python tests/test_clean.py                    → 2017_11 and 2018_07 (one incoming, one holdback)
  python tests/test_clean.py 2017_05 2018_08    → any months you like

Exit code 0 = every injected issue caught and every post-condition holds; 1 = something failed.
That exit code lets the orchestrator (Step 3.2) or a scheduler stop on failure.
"""

import pathlib
import sys

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))      # make `import clean` work without installing a package

import clean  # noqa: E402  (import after the path tweak, on purpose)

MESS_LOG = ROOT / "data" / "mess_log.csv"
DEFAULT_MONTHS = ["2017_11", "2018_07"]
KEYS = {
    "orders":      ["order_id"],
    "order_items": ["order_id", "order_item_id"],
    "payments":    ["order_id", "payment_sequential"],
    "reviews":     ["review_id", "order_id"],
}


def caught_count(mess_type: str, table: str, results: dict) -> int:
    """Translate one mess_log row into the matching number from clean.py's stats."""
    stats = results[table][1]
    if mess_type == "duplicate_rows":
        # exact + key-based together = every injected copy (some copies were altered after duplication)
        return stats["exact_duplicates_removed"] + stats["key_duplicates_removed"]
    if mess_type == "status_casing":
        return stats["order_status_values_standardized"]
    if mess_type == "malformed_dates":
        return stats["malformed_dates"]
    if mess_type == "blank_strings":
        return stats["whitespace_cells_to_null"]
    raise ValueError(f"no rule for mess_type={mess_type}")


def post_conditions(results: dict) -> list[tuple[str, bool]]:
    """Checks on the OUTPUT, independent of the mess log: is the clean data actually clean?"""
    orders = results["orders"][0]
    checks = []
    for table, key in KEYS.items():
        df = results[table][0]
        checks.append((f"{table}: no duplicate keys", not df.duplicated(subset=key).any()))
        text_cols = [c for c in df.columns if pd.api.types.is_string_dtype(df[c])]
        no_blank = all(not (df[c].str.strip() == "").any() for c in text_cols)
        checks.append((f"{table}: no blank/whitespace-only strings left", no_blank))
    checks.append(("orders: only the 8 canonical statuses",
                   set(orders["order_status"].dropna()) <= clean.CANONICAL_STATUSES))
    checks.append(("orders: all 5 date columns are datetime",
                   all(pd.api.types.is_datetime64_any_dtype(orders[c]) for c in clean.ORDER_DATE_COLS)))
    checks.append(("reviews: one review per order",
                   not results["reviews"][0]["order_id"].duplicated().any()))
    checks.append(("orders: derived columns present",
                   {"is_delivered", "delivery_days", "is_late", "purchase_month"} <= set(orders.columns)))
    checks.append(("order_items: total_item_value = price + freight",
                   bool((results["order_items"][0].eval("total_item_value - price - freight_value").abs() < 1e-9).all())))
    return checks


def test_month(month: str, mess: pd.DataFrame) -> bool:
    """Clean one month, print injected vs caught per mess type, run post-conditions. True = all passed."""
    results = clean.clean_month(month)
    rows = []
    for _, m in mess[mess["month"] == month].iterrows():
        caught = caught_count(m["mess_type"], m["table"], results)
        injected = int(m["affected_rows"])
        # blank_strings: the source also has a few REAL whitespace-only comments, so caught may exceed injected
        ok = caught >= injected if m["mess_type"] == "blank_strings" else caught == injected
        rows.append({"table": m["table"], "mess_type": m["mess_type"], "injected": injected,
                     "caught": caught, "result": "PASS" if ok else "FAIL"})
    report = pd.DataFrame(rows)

    print(f"\n=== {month} ===")
    print(report.to_string(index=False))
    checks = post_conditions(results)
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")

    return bool((report["result"] == "PASS").all()) and all(ok for _, ok in checks)


def main() -> int:
    """Run every requested month; return a process exit code."""
    months = sys.argv[1:] or DEFAULT_MONTHS
    mess = pd.read_csv(MESS_LOG, dtype={"month": str})
    passed = [test_month(m, mess) for m in months]
    print(f"\n{sum(passed)}/{len(passed)} months passed")
    return 0 if all(passed) else 1


if __name__ == "__main__":
    sys.exit(main())
