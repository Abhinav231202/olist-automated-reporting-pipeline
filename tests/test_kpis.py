"""
test_kpis.py  —  Step 2.5
Runs every vw_kpi_* view, prints a short preview, and proves the numbers are right by
reconciling them against the base tables / each other (not just "the view exists": Issue #35).

Plain Python + exit code (0 = all checks pass, 1 = any failure), like the other tests,
so the orchestrator or a person can run it:   python tests/test_kpis.py
Read-only: it never writes to the database.
"""

import logging
import pathlib
import sys
import time

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import db  # noqa: E402  (import after the path tweak)

log = logging.getLogger("test_kpis")

KPI_VIEWS = [
    "vw_kpi_monthly_revenue", "vw_kpi_running_revenue", "vw_kpi_orders_moving_avg",
    "vw_kpi_top_categories_by_state", "vw_kpi_delivery_by_state", "vw_kpi_repeat_customers",
    "vw_kpi_cohort_retention", "vw_kpi_category_share", "vw_kpi_top_sellers",
    "vw_kpi_second_highest_order",
]
VALID = "order_status NOT IN ('canceled', 'unavailable')"   # same filter as kpis.sql


SOLUTIONS = pathlib.Path(__file__).resolve().parent.parent / "sql" / "practice_kpis_solutions.sql"


def solution_statements() -> list[str]:
    """
    Split practice_kpis_solutions.sql into its SELECT statements, in file order
    (Q1–Q10, then V1, V2a/b/c, the tie demo, V3a/b). Comment lines are skipped and a
    statement ends at a line ending in ';'. Comments never end in ';' in that file.
    """
    statements, current = [], []
    for line in SOLUTIONS.read_text(encoding="utf-8").splitlines():
        code = line.split("--")[0].rstrip()          # drop trailing comments
        if not code:
            continue
        current.append(code)
        if code.endswith(";"):
            statements.append("\n".join(current))
            current = []
    return statements


def check(results: list, name: str, ok: bool, detail: str) -> None:
    """Record one pass/fail line."""
    results.append(ok)
    log.info("%s  %-44s %s", "PASS" if ok else "FAIL", name, detail)


def main() -> int:
    """Load every KPI view, preview it, run the reconciliation checks, return the exit code."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 12)

    conn = db.get_connection()
    kpi, results = {}, []
    try:
        # ── 1. run + preview every view ─────────────────────────────────────────
        for view in KPI_VIEWS:
            t = time.perf_counter()
            kpi[view] = pd.read_sql(f"SELECT * FROM {view}", conn)   # names from our own list
            log.info("\n=== %s: %d rows, %.2f s", view, len(kpi[view]), time.perf_counter() - t)
            log.info(kpi[view].head(5).to_string(index=False))

        # ── 2. reconciliation checks ────────────────────────────────────────────
        log.info("\n=== checks")
        base_rev = conn.execute(
            f"SELECT SUM(oi.price) FROM order_items oi JOIN orders o USING (order_id) WHERE o.{VALID}"
        ).fetchone()[0]
        base_orders = conn.execute(f"SELECT COUNT(*) FROM orders WHERE {VALID}").fetchone()[0]

        m = kpi["vw_kpi_monthly_revenue"]
        check(results, "#1 revenue = base tables", abs(m.revenue.sum() - base_rev) < 1,
              f"{m.revenue.sum():,.2f} vs {base_rev:,.2f}")
        check(results, "#1 orders = base table", m.orders.sum() == base_orders,
              f"{m.orders.sum():,} vs {base_orders:,}")
        gap = m[m.purchase_month == "2017-01"].iloc[0]
        check(results, "#1 no MoM across the 2016-10 -> 2017-01 gap",
              pd.isna(gap.mom_growth_pct) and "gap" in gap.mom_note, str(gap.mom_note))
        check(results, "#1 MoM only missing on first + gap month",
              m.mom_growth_pct.isna().sum() == 2, f"{m.mom_growth_pct.isna().sum()} NULLs")

        r = kpi["vw_kpi_running_revenue"]
        check(results, "#2 last running total = total revenue",
              abs(r.running_revenue.iloc[-1] - base_rev) < 1 and r.running_pct_of_total.iloc[-1] == 100,
              f"{r.running_revenue.iloc[-1]:,.2f}")

        ma = kpi["vw_kpi_orders_moving_avg"]
        check(results, "#3 3m avg NULL until 3 calendar months",
              ma.moving_avg_3m.isna().sum() == 3,   # 2016-10, 2017-01, 2017-02
              f"NULL in {list(ma[ma.moving_avg_3m.isna()].purchase_month)}")
        row = ma[ma.purchase_month == "2017-05"].iloc[0]
        manual = ma[ma.purchase_month.isin(["2017-03", "2017-04", "2017-05"])].orders.mean()
        check(results, "#3 2017-05 avg = mean of Mar-May by hand",
              abs(row.moving_avg_3m - round(manual, 1)) < 0.05, f"{row.moving_avg_3m} vs {manual:.1f}")

        t4 = kpi["vw_kpi_top_categories_by_state"]
        check(results, "#4 every state has ranks 1..3, max rank 3",
              t4.groupby("customer_state").category_rank.max().le(3).all()
              and t4.customer_state.nunique() == 27, f"{t4.customer_state.nunique()} states, {len(t4)} rows")

        d = kpi["vw_kpi_delivery_by_state"]
        all_row, states = d[d.state == "ALL"].iloc[0], d[d.state != "ALL"]
        check(results, "#5 state rows add up to the ALL row",
              states.delivered_orders.sum() == all_row.delivered_orders
              and states.late_orders.sum() == all_row.late_orders,
              f"{all_row.delivered_orders:,} delivered, {all_row.late_orders:,} late")

        rc = kpi["vw_kpi_repeat_customers"].iloc[0]
        n_people = conn.execute(
            f"SELECT COUNT(DISTINCT c.customer_unique_id) FROM orders o "
            f"JOIN customers c USING (customer_id) WHERE o.{VALID}").fetchone()[0]
        check(results, "#6 customers = distinct customer_unique_id",
              rc.customers == n_people and rc.repeat_rate_if_customer_id_pct == 0,
              f"{rc.customers:,}; customer_id rate = {rc.repeat_rate_if_customer_id_pct}%")

        co = kpi["vw_kpi_cohort_retention"]
        m0 = co[co.months_since_first == 0]
        check(results, "#7 month 0 = 100%, cohort sizes = customers",
              (m0.retention_pct == 100).all() and m0.cohort_customers.sum() == rc.customers,
              f"{len(m0)} cohorts, {m0.cohort_customers.sum():,} people")

        cs = kpi["vw_kpi_category_share"]
        check(results, "#8 shares add to 100, revenue = #1",
              abs(cs.pct_of_total.sum() - 100) < 0.1 and abs(cs.revenue.sum() - base_rev) < 1,
              f"{cs.pct_of_total.sum():.2f}%, {len(cs)} categories")

        ts = kpi["vw_kpi_top_sellers"]
        n_sellers = int(ts.total_sellers.iloc[0])
        check(results, "#9 top decile = ceil(sellers / 10) rows",
              len(ts) == -(-n_sellers // 10), f"{len(ts)} of {n_sellers:,} sellers")

        sh = kpi["vw_kpi_second_highest_order"]
        check(results, "#10 one row per repeat customer",
              len(sh) == rc.repeat_customers and (sh.second_highest_value <= sh.highest_order_value).all(),
              f"{len(sh):,} rows, {int(sh.tied_with_highest.sum()):,} tied with highest")

        # ── 3. the practice solutions must match the views (and their stated answers) ──
        log.info("\n=== practice_kpis_solutions.sql")
        sols = solution_statements()
        for i, view in enumerate(KPI_VIEWS):
            sol = pd.read_sql(sols[i], conn)
            cols = [c for c in sol.columns if c in kpi[view].columns]
            a = sol[cols].sort_values(cols).reset_index(drop=True)
            b = kpi[view][cols].sort_values(cols).reset_index(drop=True)
            check(results, f"Q{i + 1} solution = {view}", len(cols) == len(sol.columns) and a.equals(b),
                  f"{len(sol)} rows, {len(cols)} columns compared")

        v1 = pd.read_sql(sols[10], conn).iloc[-1]
        check(results, "V1 last ROW_NUMBER / RANK / DENSE_RANK",
              (v1.row_num, v1.rnk, v1.dense_rnk) == (23, 19, 17), f"{v1.row_num} / {v1.rnk} / {v1.dense_rnk}")
        v2 = [pd.read_sql(sols[k], conn).iloc[0, 0] for k in (11, 12, 13)]
        check(results, "V2 three ways agree (6,735.00)", v2 == [6735.0] * 3, str(v2))
        tie = pd.read_sql(sols[14], conn).iloc[0].tolist()
        check(results, "V2 tie demo: OFFSET wrong, others right", tie == [100, 90, 90], str(tie))
        v3 = [len(pd.read_sql(sols[k], conn)) for k in (15, 16)]
        check(results, "V3 HAVING vs WHERE counts (7 vs 25)", v3 == [7, 25], str(v3))
    finally:
        conn.close()

    failed = results.count(False)
    log.info("\n%d checks, %d failed", len(results), failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
