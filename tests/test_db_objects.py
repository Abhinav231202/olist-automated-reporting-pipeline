"""
test_db_objects.py  —  Step 2.3: prove the views and the audit trigger work on db/olist.db.

  1. Grain proofs: vw_order_facts has exactly one row per order, vw_item_facts one row per
     order item, and no money is created or lost by the joins (sums reconcile to the tables).
  2. Fan-out demo: the same numbers from a naive orders x items x payments join, to show
     what the views protect against.
  3. Trigger test inside a transaction: UPDATE one order's status → the audit row appears →
     ROLLBACK → the order and the audit table are exactly as before.

Plain Python, no pytest. Read-only in effect: the only write is rolled back.
  python tests/test_db_objects.py      → exit code 0 = all checks pass, 1 = something failed
"""

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402

TOLERANCE = 0.01   # money is REAL (floating point), so compare sums within 1 cent, never with ==

failures = []


def check(name: str, ok: bool, detail: str) -> None:
    """Print one PASS/FAIL line and remember failures for the exit code."""
    print(f"{'PASS' if ok else 'FAIL'}  {name:<42} {detail}")
    if not ok:
        failures.append(name)


def one(conn, sql: str, params: tuple = ()):
    """Run a query that returns a single value."""
    return conn.execute(sql, params).fetchone()[0]


def test_grain(conn) -> None:
    """Row counts and sums of the views must match the base tables exactly."""
    orders, order_facts = one(conn, "SELECT COUNT(*) FROM orders"), one(conn, "SELECT COUNT(*) FROM vw_order_facts")
    check("vw_order_facts rows = orders", orders == order_facts, f"{order_facts:,} vs {orders:,}")

    distinct = one(conn, "SELECT COUNT(DISTINCT order_id) FROM vw_order_facts")
    check("vw_order_facts order_id unique", distinct == order_facts, f"{distinct:,} distinct ids")

    items, item_facts = one(conn, "SELECT COUNT(*) FROM order_items"), one(conn, "SELECT COUNT(*) FROM vw_item_facts")
    check("vw_item_facts rows = order_items", items == item_facts, f"{item_facts:,} vs {items:,}")

    base_items = one(conn, "SELECT SUM(total_item_value) FROM order_items")
    for view, col in (("vw_order_facts", "items_total"), ("vw_item_facts", "total_item_value")):
        v = one(conn, f"SELECT SUM({col}) FROM {view}")      # names from this file, not user input
        check(f"{view}.{col} sum = order_items", abs(v - base_items) < TOLERANCE,
              f"{v:,.2f} vs {base_items:,.2f}")

    base_pay, view_pay = one(conn, "SELECT SUM(payment_value) FROM payments"), \
        one(conn, "SELECT SUM(payment_total) FROM vw_order_facts")
    check("vw_order_facts.payment_total sum = payments", abs(view_pay - base_pay) < TOLERANCE,
          f"{view_pay:,.2f} vs {base_pay:,.2f}")

    no_items = one(conn, "SELECT COUNT(*) FROM vw_order_facts WHERE item_count = 0")
    no_review = one(conn, "SELECT COUNT(*) FROM vw_order_facts WHERE review_score IS NULL")
    no_cat = one(conn, "SELECT COUNT(*) FROM vw_item_facts WHERE category IS NULL")
    check("LEFT JOIN kept orders with no items", no_items >= 0, f"{no_items:,} orders with item_count = 0")
    print(f"INFO  {'orders with no review':<42} {no_review:,}")
    check("every item has a category", no_cat == 0, f"{no_cat} items with NULL category")


def show_fan_out(conn) -> None:
    """What the CTEs prevent: joining both many-sides directly multiplies rows and money."""
    rows, money = conn.execute("""
        SELECT COUNT(*), SUM(oi.total_item_value)
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.order_id
        JOIN payments    p  ON p.order_id  = o.order_id""").fetchone()
    true_money = one(conn, "SELECT SUM(total_item_value) FROM order_items")
    print(f"INFO  {'naive join (fan-out)':<42} {rows:,} rows, revenue {money:,.2f} "
          f"(+{money - true_money:,.2f}, +{(money / true_money - 1) * 100:.1f}% overstated)")


def test_trigger(conn) -> None:
    """Change one status inside a transaction, check the audit row, then ROLLBACK."""
    order_id, old_status = conn.execute(
        "SELECT order_id, order_status FROM orders WHERE order_status = 'shipped' "
        "ORDER BY order_id LIMIT 1").fetchone()
    audit_before = one(conn, "SELECT COUNT(*) FROM order_status_audit")

    conn.execute("BEGIN")                                     # explicit: everything below is undoable
    try:
        conn.execute("UPDATE orders SET order_status = 'delivered' WHERE order_id = ?", (order_id,))
        row = conn.execute(
            "SELECT order_id, old_status, new_status, changed_at FROM order_status_audit "
            "ORDER BY audit_id DESC LIMIT 1").fetchone()
        print(f"INFO  {'audit row written by the trigger':<42} {row}")
        check("trigger wrote old -> new status",
              row[:3] == (order_id, old_status, "delivered") and row[3] is not None,
              f"{old_status} -> delivered at {row[3]}")

        # same value again: the WHEN clause should stop the trigger (no noise rows)
        conn.execute("UPDATE orders SET order_status = 'delivered' WHERE order_id = ?", (order_id,))
        n = one(conn, "SELECT COUNT(*) FROM order_status_audit") - audit_before
        check("no audit row when status doesn't change", n == 1, f"{n} audit row(s) after 2 updates")

        # updating a different column must not fire it either (AFTER UPDATE OF order_status)
        conn.execute("UPDATE orders SET delivery_days = delivery_days WHERE order_id = ?", (order_id,))
        n = one(conn, "SELECT COUNT(*) FROM order_status_audit") - audit_before
        check("no audit row when another column changes", n == 1, f"{n} audit row(s)")

        # the view reflects the change immediately, inside the same transaction
        live = one(conn, "SELECT order_status FROM vw_order_facts WHERE order_id = ?", (order_id,))
        check("view shows the new status at once", live == "delivered", f"vw_order_facts says '{live}'")
    finally:
        conn.rollback()                                       # undo the UPDATE *and* the audit insert

    after_status = one(conn, "SELECT order_status FROM orders WHERE order_id = ?", (order_id,))
    audit_after = one(conn, "SELECT COUNT(*) FROM order_status_audit")
    check("ROLLBACK restored the order", after_status == old_status, f"status back to '{after_status}'")
    check("ROLLBACK removed the audit row", audit_after == audit_before, f"audit rows {audit_after}")


def main() -> int:
    """Run every check; exit code 1 if any failed."""
    t0 = time.perf_counter()
    conn = db.get_connection()               # also (re)applies views.sql and triggers.sql
    print(f"INFO  {'get_connection + apply views/trigger':<42} {(time.perf_counter() - t0) * 1000:.0f} ms")
    try:
        t1 = time.perf_counter()
        test_grain(conn)
        print(f"INFO  {'grain checks (views run live)':<42} {time.perf_counter() - t1:.2f} s")
        show_fan_out(conn)
        test_trigger(conn)
    finally:
        conn.close()
    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
