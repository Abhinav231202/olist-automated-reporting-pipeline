-- =============================================================================
-- checks.sql  —  Step 2.4
-- Automated data-quality (DQ) checks. src/checks.py runs every check after each load
-- and stores the result in dq_results (one row per check per run).
--
-- FORMAT (checks.py reads these tags, so keep them exact):
--   -- @check: <unique_name>        starts a new check
--   -- @severity: ERROR | WARNING
--   -- <free comment lines>         what it catches and why
--   SELECT ... AS failed_rows;      ONE statement returning ONE number: the failing rows
--
-- SEVERITY RULE (decides whether the scheduled pipeline stops):
--   ERROR   = structural. The schema or the cleaning step should make this impossible, so
--             anything > 0 means the pipeline itself is broken (bad load, constraint off,
--             table rebuilt). checks.py exits with code 1 and the report is NOT built.
--   WARNING = a real-data issue we KEEP on purpose (clean.py "flag, never delete").
--             Logged and tracked over time, but the report still runs.
--
-- Every check returns 0 when the data is clean, so "pass" always means failed_rows = 0.
-- Excel equivalent of the whole file: a "Checks" sheet with one COUNTIFS per row and
-- conditional formatting that turns red when the count isn't 0.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. ORPHANS (anti-joins): a child row whose parent doesn't exist.
-- LEFT JOIN keeps every child row; when there's no parent, the parent's columns are NULL,
-- so "WHERE parent.key IS NULL" keeps exactly the unmatched rows.
-- Excel: XLOOKUP the key in the parent sheet, then COUNTIF(result, "#N/A").
-- The foreign keys already block orphans (preventive), but only on connections that run
-- PRAGMA foreign_keys = ON. A tool like DB Browser has it OFF by default, so these
-- checks (detective) catch anything that got in another way.
-- -----------------------------------------------------------------------------

-- @check: orphan_items_no_order
-- @severity: ERROR
-- Item lines whose order_id is not in orders.
SELECT COUNT(*) AS failed_rows
FROM order_items oi
LEFT JOIN orders o ON o.order_id = oi.order_id
WHERE o.order_id IS NULL;

-- @check: orphan_payments_no_order
-- @severity: ERROR
-- Same anti-join, written with NOT EXISTS. Both give the same answer; NOT EXISTS stops at
-- the first match and can never duplicate rows, so many people prefer it.
-- (Avoid NOT IN: if the subquery returns a single NULL, NOT IN returns no rows at all.)
SELECT COUNT(*) AS failed_rows
FROM payments p
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.order_id = p.order_id);

-- @check: orphan_reviews_no_order
-- @severity: ERROR
SELECT COUNT(*) AS failed_rows
FROM reviews r
LEFT JOIN orders o ON o.order_id = r.order_id
WHERE o.order_id IS NULL;

-- @check: orphan_orders_no_customer
-- @severity: ERROR
-- Also catches customer_id IS NULL: the FK allows NULL, but NULL never matches in a join,
-- so those orders show up here too.
SELECT COUNT(*) AS failed_rows
FROM orders o
LEFT JOIN customers c ON c.customer_id = o.customer_id
WHERE c.customer_id IS NULL;

-- @check: orphan_items_no_product
-- @severity: ERROR
-- Category KPIs join items to products; an orphan would silently fall out of every category.
SELECT COUNT(*) AS failed_rows
FROM order_items oi
LEFT JOIN products p ON p.product_id = oi.product_id
WHERE p.product_id IS NULL;

-- @check: orphan_items_no_seller
-- @severity: ERROR
SELECT COUNT(*) AS failed_rows
FROM order_items oi
LEFT JOIN sellers s ON s.seller_id = oi.seller_id
WHERE s.seller_id IS NULL;


-- -----------------------------------------------------------------------------
-- 2. DUPLICATE PRIMARY KEYS: failed_rows = number of key values that appear more than once.
-- GROUP BY the key, keep groups with HAVING COUNT(*) > 1, then count the groups.
-- Excel: COUNTIF(key_column, key) > 1 in a helper column; Conditional Formatting →
-- Duplicate Values.  pandas: df.duplicated(subset=key, keep=False).sum()
-- The PRIMARY KEY makes this impossible in SQLite, so why check? If the table is ever
-- rebuilt without the PK, or moved to a warehouse that does NOT enforce primary keys
-- (Snowflake, BigQuery, Redshift accept duplicates), this is the only thing that notices.
-- -----------------------------------------------------------------------------

-- @check: dup_pk_orders
-- @severity: ERROR
SELECT COUNT(*) AS failed_rows
FROM (SELECT order_id FROM orders GROUP BY order_id HAVING COUNT(*) > 1);

-- @check: dup_pk_order_items
-- @severity: ERROR
-- Composite key: the PAIR must be unique (order_item_id restarts at 1 in every order).
SELECT COUNT(*) AS failed_rows
FROM (SELECT order_id, order_item_id FROM order_items
      GROUP BY order_id, order_item_id HAVING COUNT(*) > 1);

-- @check: dup_pk_payments
-- @severity: ERROR
SELECT COUNT(*) AS failed_rows
FROM (SELECT order_id, payment_sequential FROM payments
      GROUP BY order_id, payment_sequential HAVING COUNT(*) > 1);

-- @check: dup_pk_reviews
-- @severity: ERROR
-- Grain is one review per ORDER (review_id repeats on purpose, see schema.sql).
SELECT COUNT(*) AS failed_rows
FROM (SELECT order_id FROM reviews GROUP BY order_id HAVING COUNT(*) > 1);

-- @check: dup_pk_customers
-- @severity: ERROR
SELECT COUNT(*) AS failed_rows
FROM (SELECT customer_id FROM customers GROUP BY customer_id HAVING COUNT(*) > 1);

-- @check: dup_pk_products
-- @severity: ERROR
SELECT COUNT(*) AS failed_rows
FROM (SELECT product_id FROM products GROUP BY product_id HAVING COUNT(*) > 1);

-- @check: dup_pk_sellers
-- @severity: ERROR
SELECT COUNT(*) AS failed_rows
FROM (SELECT seller_id FROM sellers GROUP BY seller_id HAVING COUNT(*) > 1);

-- @check: dup_pk_geolocation
-- @severity: ERROR
-- 1M raw rows were reduced to one row per zip prefix in the loader (Step 2.2).
SELECT COUNT(*) AS failed_rows
FROM (SELECT geolocation_zip_code_prefix FROM geolocation
      GROUP BY geolocation_zip_code_prefix HAVING COUNT(*) > 1);


-- -----------------------------------------------------------------------------
-- 3. VALUE RULES: business rules that clean.py flags but deliberately does NOT block
-- (schema.sql has no CHECK on them). So they're WARNINGs: known, kept, monitored.
-- -----------------------------------------------------------------------------

-- @check: items_price_not_positive
-- @severity: WARNING
-- A free or negative item would understate revenue. Excel: COUNTIF(price, "<=0").
-- NULL price is NOT counted (NULL <= 0 is NULL, not TRUE), same as COUNTIF ignoring blanks.
SELECT COUNT(*) AS failed_rows
FROM order_items
WHERE price <= 0;

-- @check: reviews_score_out_of_range
-- @severity: WARNING
-- Valid scores are 1–5. NULL is counted on purpose: a review row with no score is also bad.
-- Excel: COUNTIFS(score, "<1") + COUNTIFS(score, ">5") + COUNTBLANK(score)
SELECT COUNT(*) AS failed_rows
FROM reviews
WHERE review_score NOT BETWEEN 1 AND 5
   OR review_score IS NULL;


-- -----------------------------------------------------------------------------
-- 4. DATE LOGIC
-- julianday() turns a timestamp into a number of days, so the comparison doesn't depend
-- on the text format (Excel stores dates as day numbers too, so this is just  =B2<A2).
-- -----------------------------------------------------------------------------

-- @check: delivered_no_delivery_date
-- @severity: WARNING
-- Status says delivered but there's no delivery date. Known causes: 8 real source errors
-- + dates destroyed by malformed text in the feed (flag_malformed_date). Excel:
-- COUNTIFS(status, "delivered", delivered_date, "")
SELECT COUNT(*) AS failed_rows
FROM orders
WHERE order_status = 'delivered'
  AND order_delivered_customer_date IS NULL;

-- @check: delivered_before_purchase
-- @severity: WARNING
-- Physically impossible, but it's a SOURCE error, not a pipeline error, so WARNING.
-- If either date is NULL the comparison is NULL, so the row isn't counted (not checkable).
SELECT COUNT(*) AS failed_rows
FROM orders
WHERE julianday(order_delivered_customer_date) < julianday(order_purchase_timestamp);


-- -----------------------------------------------------------------------------
-- 5. RECONCILIATION: what the customer paid vs what the items + freight cost.
-- Step 1: aggregate EACH side to one row per order in its own CTE (avoids fan-out:
--         joining items and payments directly multiplies rows, see views.sql).
-- Step 2: compare with a tolerance. Money is REAL (floating point): 0.1 + 0.2 is stored as
--         0.30000000000000004, so "=" calls thousands of identical totals different.
--         Measured on this DB: "<>" flags 16,089 orders, > R$0.01 flags 319, > R$1 flags 216.
--         R$1 also absorbs rounding of installment interest, which isn't what we're after.
-- Excel: SUMIFS each sheet by order_id into helper columns, then =ABS(paid-items)>1
--        (never =paid=items on calculated money in Excel either).
-- -----------------------------------------------------------------------------

-- @check: payment_vs_items_mismatch
-- @severity: WARNING
-- Only orders that have BOTH items and payments: with a missing side, ABS(NULL - x) is NULL
-- and the row would vanish from the count silently. Missing sides have their own checks
-- (orders_paid_no_items, orders_no_payment_all_months).
WITH item_totals AS (
    SELECT order_id, SUM(total_item_value) AS items_total     -- price + freight
    FROM order_items
    GROUP BY order_id
),
payment_totals AS (
    SELECT order_id, SUM(payment_value) AS payment_total
    FROM payments
    GROUP BY order_id
)
SELECT COUNT(*) AS failed_rows
FROM item_totals it
JOIN payment_totals pt ON pt.order_id = it.order_id           -- 1:1 after the CTEs
WHERE ABS(pt.payment_total - it.items_total) > 1.00;          -- R$1 tolerance, not =

-- @check: orders_paid_no_items
-- @severity: WARNING
-- Orders with a payment but no item lines (mostly canceled / unavailable). These explain
-- most of the gap between total payments and total item value.
SELECT COUNT(*) AS failed_rows
FROM orders o
WHERE EXISTS     (SELECT 1 FROM payments    p WHERE p.order_id = o.order_id)
  AND NOT EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.order_id);


-- -----------------------------------------------------------------------------
-- 6. NO PAYMENT, across ALL loaded months.
-- clean.py's flag_no_payment only compared an order with payments in the SAME monthly
-- batch. A payment file arriving in a later batch, or a batch loaded without its payments
-- file, makes that flag wrong. The database holds every month, so it can check properly.
-- -----------------------------------------------------------------------------

-- @check: orders_no_payment_all_months
-- @severity: WARNING
SELECT COUNT(*) AS failed_rows
FROM orders o
WHERE NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id);

-- @check: no_payment_flag_disagrees
-- @severity: WARNING
-- Compares the two answers ROW BY ROW: the batch-scoped flag vs the all-months truth.
-- Comparing only the totals could hide errors that cancel out (one false 1, one false 0).
-- flag IS NULL = "never checked" (batch had no payments file), also counted.
SELECT COUNT(*) AS failed_rows
FROM orders o
WHERE o.flag_no_payment IS NULL
   OR o.flag_no_payment <> CASE WHEN EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id)
                                THEN 0      -- has a payment → flag should be 0
                                ELSE 1 END; -- no payment    → flag should be 1


-- -----------------------------------------------------------------------------
-- 7. FLAG TOTALS: every flag_ column clean.py sets, summed, so dq_results shows how many
-- known issues are in the database after each run (a trend: does a new month bring more?).
-- SUM of a 0/1 column = count of 1s (Excel: =SUM(flag) or COUNTIF(flag, 1)).
-- COALESCE: SUM over an empty table is NULL, and dq_results.failed_rows is NOT NULL.
-- -----------------------------------------------------------------------------

-- @check: flag_orders_malformed_date
-- @severity: WARNING
SELECT COALESCE(SUM(flag_malformed_date), 0) AS failed_rows FROM orders;

-- @check: flag_orders_unknown_status
-- @severity: WARNING
SELECT COALESCE(SUM(flag_unknown_status), 0) AS failed_rows FROM orders;

-- @check: flag_orders_pickup_before_purchase
-- @severity: WARNING
SELECT COALESCE(SUM(flag_pickup_before_purchase), 0) AS failed_rows FROM orders;

-- @check: flag_orders_delivered_before_pickup
-- @severity: WARNING
SELECT COALESCE(SUM(flag_delivered_before_pickup), 0) AS failed_rows FROM orders;

-- @check: flag_orders_canceled_with_delivery
-- @severity: WARNING
SELECT COALESCE(SUM(flag_canceled_with_delivery), 0) AS failed_rows FROM orders;

-- @check: flag_orders_delivered_no_date
-- @severity: WARNING
SELECT COALESCE(SUM(flag_delivered_no_date), 0) AS failed_rows FROM orders;

-- @check: flag_orders_no_payment
-- @severity: WARNING
SELECT COALESCE(SUM(flag_no_payment), 0) AS failed_rows FROM orders;

-- @check: flag_items_malformed_date
-- @severity: WARNING
SELECT COALESCE(SUM(flag_malformed_date), 0) AS failed_rows FROM order_items;

-- @check: flag_items_price_not_positive
-- @severity: WARNING
SELECT COALESCE(SUM(flag_price_not_positive), 0) AS failed_rows FROM order_items;

-- @check: flag_items_shipping_limit_far_future
-- @severity: WARNING
SELECT COALESCE(SUM(flag_shipping_limit_far_future), 0) AS failed_rows FROM order_items;

-- @check: flag_payments_installments_was_zero
-- @severity: WARNING
SELECT COALESCE(SUM(flag_installments_was_zero), 0) AS failed_rows FROM payments;

-- @check: flag_payments_type_not_defined
-- @severity: WARNING
SELECT COALESCE(SUM(flag_payment_type_not_defined), 0) AS failed_rows FROM payments;

-- @check: flag_payments_zero_value
-- @severity: WARNING
SELECT COALESCE(SUM(flag_zero_payment_value), 0) AS failed_rows FROM payments;

-- @check: flag_reviews_malformed_date
-- @severity: WARNING
SELECT COALESCE(SUM(flag_malformed_date), 0) AS failed_rows FROM reviews;

-- @check: flag_reviews_invalid_score
-- @severity: WARNING
SELECT COALESCE(SUM(flag_invalid_score), 0) AS failed_rows FROM reviews;

-- @check: flag_products_missing_category
-- @severity: WARNING
SELECT COALESCE(SUM(flag_missing_category), 0) AS failed_rows FROM products;

-- @check: flag_products_untranslated_category
-- @severity: WARNING
SELECT COALESCE(SUM(flag_untranslated_category), 0) AS failed_rows FROM products;
