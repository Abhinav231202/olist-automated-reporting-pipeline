-- =====================================================================================
-- Checkpoint 4: SQL practice SOLUTIONS (all tested on db/olist.db)
-- Run each one in DB Browser (Execute SQL tab): select the query, then press Ctrl+Enter.
-- Try each question yourself first; read the explanation after.
-- =====================================================================================


-- Q1. Row count of each fact table in ONE result.
-- Skill: UNION ALL stacks result sets with the same columns. UNION would also remove duplicate rows (slower, not needed).
-- Common mistake: ending with "UNION ALL" (it expects another SELECT after it).
SELECT 'orders'      AS table_name, COUNT(*) AS row_count FROM orders
UNION ALL
SELECT 'order_items',               COUNT(*)              FROM order_items
UNION ALL
SELECT 'payments',                  COUNT(*)              FROM payments
UNION ALL
SELECT 'reviews',                   COUNT(*)              FROM reviews;
-- Expected: orders 80,445 · order_items 91,224 · payments 84,238 · reviews 79,776


-- Q2a. How many orders have more than one item?
-- Skill: GROUP BY makes one group per order; HAVING filters GROUPS (WHERE filters ROWS, before grouping).
-- The inner query lists the qualifying orders; the outer query counts them (a "subquery in FROM").
SELECT COUNT(*) AS orders_with_multiple_items
FROM (
    SELECT order_id
    FROM order_items
    GROUP BY order_id
    HAVING COUNT(*) > 1
);
-- Expected: 8,019

-- Q2b. The most items in a single order.
SELECT MAX(item_count) AS max_items_in_one_order
FROM (
    SELECT order_id, COUNT(*) AS item_count
    FROM order_items
    GROUP BY order_id
);
-- Expected: 21


-- Q3. Top 5 customer states by number of orders.
-- Skill: JOIN a fact (orders) to a dimension (customers) to slice by an attribute (state).
-- Logical order: FROM/JOIN -> GROUP BY -> SELECT -> ORDER BY -> LIMIT.
SELECT c.customer_state,
       COUNT(*) AS orders
FROM orders AS o
JOIN customers AS c
  ON c.customer_id = o.customer_id
GROUP BY c.customer_state
ORDER BY orders DESC
LIMIT 5;
-- Expected: SP 32,931 · RJ 10,670 · MG 9,548 · RS 4,544 · PR 4,083


-- Q4. Reproduce the FAN-OUT bug (deliberately wrong!).
-- Joining orders to TWO one-to-many tables (items AND payments) multiplies rows:
-- an order with 2 items and 2 payments appears 2 x 2 = 4 times, so every sum is inflated.
SELECT COUNT(*)                           AS rows_after_join,      -- should be 80,445 orders...
       ROUND(SUM(i.total_item_value), 2)  AS inflated_revenue      -- ...and R$ 12,758,298.80
FROM orders AS o
JOIN order_items AS i ON i.order_id = o.order_id
JOIN payments    AS p ON p.order_id = o.order_id;
-- Expected: 95,413 rows · R$ 13,357,783.42  (+4.7% overstated)  -> compare with Q8 for the fix


-- Q5a. Orders with NO items (anti-join).
-- Skill: LEFT JOIN keeps every order; where there's no matching item, the item columns are NULL.
-- Filtering on "IS NULL" keeps only the orders with no match. (Excel: XLOOKUP -> #N/A)
SELECT COUNT(*) AS orders_without_items
FROM orders AS o
LEFT JOIN order_items AS i
  ON i.order_id = o.order_id
WHERE i.order_id IS NULL;
-- Expected: 669

-- Q5b. Their top 3 statuses (are they errors, or expected?).
SELECT o.order_status,
       COUNT(*) AS orders
FROM orders AS o
LEFT JOIN order_items AS i
  ON i.order_id = o.order_id
WHERE i.order_id IS NULL
GROUP BY o.order_status
ORDER BY orders DESC
LIMIT 3;
-- Expected: unavailable 574 · canceled 87 · created 5  -> mostly expected (never fulfilled)

-- Alternative anti-join with NOT EXISTS (same answer; often clearer to read):
SELECT COUNT(*) AS orders_without_items
FROM orders AS o
WHERE NOT EXISTS (SELECT 1 FROM order_items AS i WHERE i.order_id = o.order_id);


-- Q6. From load_log: files and rows loaded per table.
-- Skill: querying the pipeline's own audit table. This is how you PROVE what was loaded.
SELECT table_name,
       COUNT(*)         AS files_loaded,
       SUM(rows_loaded) AS rows_loaded
FROM load_log
GROUP BY table_name
ORDER BY table_name;
-- Expected: each fact table 18 files (orders 80,445 · order_items 91,224 · payments 84,238 · reviews 79,776);
--           each dimension 1 (customers 99,441 · products 32,951 · sellers 3,095 · geolocation 19,015)


-- Q7. Late-delivery % by state (delivered orders, states with >= 1,000 orders), worst 3.
-- Skill: is_late is 1/0, so AVG(is_late) = the share of late orders. Undelivered orders are NULL,
-- and AVG ignores NULLs, which is exactly why cleaning set them to NULL, not 0.
-- 100.0 (not 100) forces decimal maths; integer division would round to 0.
SELECT customer_state,
       COUNT(*)                        AS delivered_orders,
       ROUND(100.0 * AVG(is_late), 1)  AS late_pct
FROM vw_order_facts
WHERE is_delivered = 1
GROUP BY customer_state
HAVING COUNT(*) >= 1000
ORDER BY late_pct DESC
LIMIT 3;
-- Expected: CE 16.4% · RJ 14.0% · BA 13.9%

-- Overall late rate in the loaded months (scope: 18 months, holdback not loaded):
SELECT ROUND(100.0 * AVG(is_late), 1) AS overall_late_pct
FROM vw_order_facts;
-- Expected: 7.5%  (vs 6.8% on the whole raw dataset: different scope)


-- Q8. Orders where payments and items differ by more than R$1, WITHOUT fan-out.  (The fix for Q4.)
-- Skill: CTEs (WITH ...) aggregate each table to ONE row per order FIRST, then join 1:1.
-- ABS(...) > 1 = tolerance: money as REAL has tiny rounding differences, so never compare with "=".
WITH item_totals AS (
    SELECT order_id, SUM(total_item_value) AS items_total
    FROM order_items
    GROUP BY order_id
),
payment_totals AS (
    SELECT order_id, SUM(payment_value) AS paid_total
    FROM payments
    GROUP BY order_id
)
SELECT COUNT(*) AS orders_with_mismatch
FROM item_totals AS it
JOIN payment_totals AS pt
  ON pt.order_id = it.order_id
WHERE ABS(it.items_total - pt.paid_total) > 1;
-- Expected: 216  (mostly installment interest / vouchers: tracked as a WARNING in Step 2.4)

-- See a few of them:
WITH item_totals AS (SELECT order_id, SUM(total_item_value) AS items_total FROM order_items GROUP BY order_id),
     payment_totals AS (SELECT order_id, SUM(payment_value) AS paid_total FROM payments GROUP BY order_id)
SELECT it.order_id,
       ROUND(it.items_total, 2)                  AS items_total,
       ROUND(pt.paid_total, 2)                   AS paid_total,
       ROUND(pt.paid_total - it.items_total, 2)  AS difference
FROM item_totals AS it
JOIN payment_totals AS pt ON pt.order_id = it.order_id
WHERE ABS(it.items_total - pt.paid_total) > 1
ORDER BY ABS(pt.paid_total - it.items_total) DESC
LIMIT 5;


-- Q9. Real customers (customer_unique_id) who ordered more than once, out of how many.
-- Skill: count PEOPLE with customer_unique_id. customer_id is new for every order (you'd get ~0 repeats).
SELECT
    (SELECT COUNT(*)
     FROM (SELECT customer_unique_id
           FROM vw_order_facts
           GROUP BY customer_unique_id
           HAVING COUNT(*) > 1))                          AS repeat_customers,
    (SELECT COUNT(DISTINCT customer_unique_id)
     FROM vw_order_facts)                                 AS total_customers;
-- Expected: 2,372 repeat customers out of 77,804 (about 3%)

-- Try the WRONG key to see the trap (should be 0):
SELECT COUNT(*) AS repeat_by_customer_id
FROM (SELECT customer_id FROM orders GROUP BY customer_id HAVING COUNT(*) > 1);


-- Q10. Top 3 product categories by revenue.
-- Skill: use the ITEM-grain view (vw_item_facts) for category analysis. Category lives on the item, not the order.
SELECT category,
       ROUND(SUM(total_item_value), 0) AS revenue
FROM vw_item_facts
GROUP BY category
ORDER BY revenue DESC
LIMIT 3;
-- Expected: health_beauty ≈ 1,055,957 · watches_gifts ≈ 1,024,230 · bed_bath_table ≈ 1,009,741


-- BONUS. Q3 plus each state's % of all orders (window function preview for Step 2.5).
-- SUM(COUNT(*)) OVER () = the grand total across all groups, computed AFTER grouping, without collapsing rows.
SELECT c.customer_state,
       COUNT(*)                                           AS orders,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct_of_all_orders
FROM orders AS o
JOIN customers AS c ON c.customer_id = o.customer_id
GROUP BY c.customer_state
ORDER BY orders DESC
LIMIT 5;
-- Expected: SP 32,931 ≈ 40.9% · RJ ≈ 13.3% · MG ≈ 11.9% · RS ≈ 5.6% · PR ≈ 5.1%
