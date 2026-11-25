-- =============================================================================
-- views.sql  —  Step 2.3
-- Base views that the KPI queries (Step 2.5) and the report (Step 3.1) read from.
--
-- A VIEW is a saved SELECT, not a copy of data. Every time someone queries it, SQLite
-- runs the SELECT against the current tables, so when load.py inserts a new month the
-- views (and every KPI built on them) include it automatically. Nothing to refresh.
--   Excel equivalent: a Power Query query / PivotTable over a table that grows. The
--   difference is that a view needs no "Refresh" click, because it's always live.
--
-- Re-runnable: DROP VIEW IF EXISTS + CREATE VIEW. db.get_connection() applies this file
-- on every connection, so editing a view here takes effect on the next pipeline run.
-- (SQLite has no CREATE OR REPLACE VIEW; Postgres/MySQL do; SQL Server has CREATE OR ALTER.)
-- BEGIN/COMMIT: if one CREATE fails (typo), the whole file rolls back and the OLD views stay.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- vw_order_facts  —  GRAIN: one row per order (row count = orders)
--
-- The fan-out trap (schema_diagram.md §4): an order with 3 items and 2 payments becomes
-- 3 × 2 = 6 rows if you join orders → order_items → payments directly, and every sum is
-- inflated. On this database a naive join gives 95,413 rows instead of 80,445, and item
-- revenue of 13.36M instead of 12.76M (+4.7%).
-- Fix: aggregate items and payments to ONE row per order_id in CTEs FIRST, then join.
-- Each CTE has one row per order, so every join below is 1:1 and can't multiply rows.
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_order_facts;
CREATE VIEW vw_order_facts AS
WITH item_totals AS (                       -- order_items: many rows per order → 1
    SELECT order_id,
           COUNT(*)                   AS item_count,
           COUNT(DISTINCT seller_id)  AS seller_count,
           SUM(price)                 AS items_price,
           SUM(freight_value)         AS items_freight,
           SUM(total_item_value)      AS items_total          -- price + freight
    FROM order_items
    GROUP BY order_id
),
payment_totals AS (                         -- payments: many rows per order → 1
    SELECT order_id,
           COUNT(*)                   AS payment_count,
           SUM(payment_value)         AS payment_total,
           MAX(payment_installments)  AS max_installments
    FROM payments
    GROUP BY order_id
)
SELECT
    o.order_id,
    o.customer_id,
    c.customer_unique_id,                   -- the real person (use this for repeat customers)
    c.customer_city,
    c.customer_state,
    o.order_status,
    o.order_purchase_timestamp,
    o.purchase_month,                       -- 'YYYY-MM'
    o.order_approved_at,
    o.order_delivered_carrier_date,
    o.order_delivered_customer_date,
    o.order_estimated_delivery_date,
    o.is_delivered,
    o.is_late,                              -- NULL when not delivered, so AVG(is_late) ignores those
    o.delivery_days,
    r.review_score,                         -- NULL when the order has no review
    r.has_comment,
    -- LEFT JOIN + no matching items → NULL. COALESCE makes "no items" = 0, so the report
    -- shows 0 instead of a blank and SUM/AVG behave predictably.
    COALESCE(it.item_count, 0)      AS item_count,
    COALESCE(it.seller_count, 0)    AS seller_count,
    COALESCE(it.items_price, 0)     AS items_price,
    COALESCE(it.items_freight, 0)   AS items_freight,
    COALESCE(it.items_total, 0)     AS items_total,
    COALESCE(pt.payment_count, 0)   AS payment_count,
    COALESCE(pt.payment_total, 0)   AS payment_total,
    pt.max_installments,
    -- data-quality flags from clean.py ("flag, never delete"); KPIs decide whether to exclude
    o.flag_malformed_date,
    o.flag_unknown_status,
    o.flag_pickup_before_purchase,
    o.flag_delivered_before_pickup,
    o.flag_canceled_with_delivery,
    o.flag_delivered_no_date,
    o.flag_no_payment,
    r.flag_invalid_score                    AS flag_invalid_review_score
FROM orders o
LEFT JOIN customers      c  ON c.customer_id = o.customer_id   -- many → 1: safe
LEFT JOIN reviews        r  ON r.order_id    = o.order_id      -- 1 → 1 (PK = order_id): safe
LEFT JOIN item_totals    it ON it.order_id   = o.order_id      -- 1 → 1 after the CTE
LEFT JOIN payment_totals pt ON pt.order_id   = o.order_id;     -- 1 → 1 after the CTE
-- Why LEFT JOIN everywhere: an INNER JOIN would silently DROP orders that have no items
-- (669 here: canceled/unavailable orders) or no review, so the row count would no longer
-- equal orders and "% of orders canceled" would be wrong. LEFT JOIN keeps the grain.


-- -----------------------------------------------------------------------------
-- vw_item_facts  —  GRAIN: one row per order item (row count = order_items)
-- For category and seller analysis: revenue lives on the item line, and products/sellers
-- hang off items, not orders. Every join goes from items to a table where the key is
-- unique (orders, products, sellers, customers), so the row count can't grow.
-- customer_state is included because "top categories per state" (KPI #4) needs it.
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_item_facts;
CREATE VIEW vw_item_facts AS
SELECT
    oi.order_id,
    oi.order_item_id,
    oi.product_id,
    p.product_category_name_english    AS category,
    oi.seller_id,
    s.seller_city,
    s.seller_state,
    c.customer_state,
    o.purchase_month,
    o.order_status,
    o.is_delivered,
    o.is_late,
    oi.shipping_limit_date,
    oi.price,
    oi.freight_value,
    oi.total_item_value,
    oi.flag_price_not_positive,
    oi.flag_shipping_limit_far_future,
    oi.flag_malformed_date             AS flag_item_malformed_date,
    p.flag_missing_category,
    p.flag_untranslated_category
FROM order_items oi
LEFT JOIN orders    o ON o.order_id    = oi.order_id     -- many items → 1 order
LEFT JOIN products  p ON p.product_id  = oi.product_id   -- many items → 1 product
LEFT JOIN sellers   s ON s.seller_id   = oi.seller_id    -- many items → 1 seller
LEFT JOIN customers c ON c.customer_id = o.customer_id;  -- 1 order → 1 customer


-- -----------------------------------------------------------------------------
-- Validate before COMMIT. SQLite does NOT check a view's columns at CREATE time:
-- CREATE VIEW v AS SELECT no_such_col FROM orders succeeds, and fails only when queried
-- (found while testing this step). LIMIT 0 makes SQLite compile each view without
-- returning rows; a typo raises an error HERE, inside the transaction, so db.py rolls
-- back and the previous, working views stay in place.
-- Add one line per new view.
-- -----------------------------------------------------------------------------
SELECT * FROM vw_order_facts LIMIT 0;
SELECT * FROM vw_item_facts  LIMIT 0;

COMMIT;
