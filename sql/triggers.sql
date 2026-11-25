-- =============================================================================
-- triggers.sql  —  Step 2.3
-- Audit trail: every change to orders.order_status is written to order_status_audit,
-- by the DATABASE itself. No Python code has to remember to do it, and it fires for
-- every UPDATE from any tool (the loader, a future upsert, someone in DB Browser).
--   Excel equivalent: Track Changes / Version History (closest); there is no
--   "run this formula when a cell changes" without VBA (Worksheet_Change event).
--
-- Re-runnable: DROP TRIGGER IF EXISTS + CREATE TRIGGER. Applied by db.get_connection().
-- =============================================================================

BEGIN;

DROP TRIGGER IF EXISTS trg_orders_status_audit;

-- AFTER UPDATE OF order_status: fires only when an UPDATE touches that column.
-- FOR EACH ROW: once per changed order (SQLite only supports row-level triggers;
--   SQL Server only has statement-level ones, using the `inserted` / `deleted` tables).
-- WHEN ... IS NOT ...: skip "updates" that set the same value, so re-loading identical
--   data doesn't fill the audit table with noise. IS NOT (not <>) because it treats
--   NULLs as values: NULL <> 'shipped' is NULL (unknown, so it wouldn't fire), but
--   NULL IS NOT 'shipped' is TRUE.
-- OLD / NEW: the row before and after the update.
CREATE TRIGGER trg_orders_status_audit
AFTER UPDATE OF order_status ON orders
FOR EACH ROW
WHEN OLD.order_status IS NOT NEW.order_status
BEGIN
    INSERT INTO order_status_audit (order_id, old_status, new_status, changed_at)
    VALUES (NEW.order_id, OLD.order_status, NEW.order_status, datetime('now', 'localtime'));
END;

COMMIT;
