-- =============================================================================
-- practice_kpis.sql  —  Step 2.5 practice pack (QUESTIONS ONLY)
-- Open db/olist.db in DB Browser for SQLite (Open Database READ ONLY), tab "Execute SQL".
-- Write your query under each question, run it, then compare with
-- sql/practice_kpis_solutions.sql (solution + explanation + expected numbers).
--
-- Rules used by every KPI (same as sql/kpis.sql):
--   revenue      = SUM(price)  (product revenue, no freight)
--                  vw_order_facts.items_price = that order's SUM(price)
--   valid order  = order_status NOT IN ('canceled', 'unavailable')
--   month        = purchase_month ('YYYY-MM'); loaded months: 2016-10, then 2017-01 … 2018-05
--                  (2016-11 and 2016-12 are NOT loaded: watch out for LAG)
--   customer     = customer_unique_id (NOT customer_id)
--   late         = is_late (0/1, calendar-day comparison; NULL if not delivered)
--
-- Views to use (never join the raw fact tables: fan-out, Issue #33):
--   vw_order_facts  1 row per order: order_id, customer_unique_id, customer_state, order_status,
--                   purchase_month, order_purchase_timestamp, is_delivered, is_late,
--                   review_score, items_price, flag_pickup_before_purchase,
--                   flag_delivered_before_pickup, …
--   vw_item_facts   1 row per item: order_id, category, seller_id, seller_state,
--                   customer_state, purchase_month, order_status, price, …
--
-- Tip: a month you can subtract:
--   CAST(substr(purchase_month,1,4) AS INTEGER)*12 + CAST(substr(purchase_month,6,2) AS INTEGER)
-- =============================================================================


-- Q1. Monthly revenue and month-over-month growth %.                       [LAG]
--     Use: vw_order_facts. Output: purchase_month, orders, revenue, mom_growth_pct.
--     Must NOT compare 2017-01 with 2016-10 (they aren't consecutive months).


-- Q2. Running total of revenue by month.                                   [SUM() OVER (ORDER BY)]
--     Use: vw_order_facts. Output: purchase_month, revenue, running_revenue.
--     Bonus: running total as % of the whole period.


-- Q3. 3-month moving average of order count.                    [ROWS BETWEEN 2 PRECEDING AND CURRENT ROW]
--     Use: vw_order_facts. Output: purchase_month, orders, moving_avg_3.
--     Then ask: what do the first two rows show? What happens around the 2016 gap?
--     Bonus: fix it with a RANGE frame on the month number.


-- Q4. Top 3 product categories by revenue in each customer state.      [CTE + DENSE_RANK PARTITION BY]
--     Use: vw_item_facts. Exclude category 'unknown'.
--     Output: customer_state, category_rank, category, revenue. (27 states)


-- Q5. Late-delivery rate by customer state, and average review score for late vs on-time orders.
--     [CASE inside AVG, GROUP BY]
--     Use: vw_order_facts. Delivered orders only (is_delivered = 1, is_late IS NOT NULL),
--     excluding flag_pickup_before_purchase = 1 and flag_delivered_before_pickup = 1.
--     Output: state, delivered_orders, late_orders, late_rate_pct, avg_review_on_time,
--     avg_review_late, worst state first. Bonus: add a national 'ALL' row.


-- Q6. Repeat customer rate: % of customers with 2+ valid orders.          [CTE, COUNT per customer]
--     Use: vw_order_facts (customer_unique_id).
--     Output: one row: customers, repeat_customers, repeat_rate_pct.
--     Then run it again with customer_id instead. What happens, and why?


-- Q7. Cohort retention: for each first-purchase month, what % of its customers bought again
--     1, 2, 3 … months later?                                     [MIN() OVER (PARTITION BY), chained CTEs]
--     Use: vw_order_facts.
--     Output: cohort_month, months_since_first, active_customers, cohort_customers, retention_pct.


-- Q8. Each category's % of total revenue (+ cumulative %).                 [SUM() OVER ()]
--     Use: vw_item_facts (keep 'unknown' so it adds up to 100%).
--     Output: category, revenue, pct_of_total, cumulative_pct, highest first.
--     How many categories make up 80% of revenue?


-- Q9. Top 10% of sellers by revenue, and their share of total revenue.     [NTILE(10)]
--     Use: vw_item_facts. Output: seller_id, revenue, pct_of_total, cumulative_pct.


-- Q10. Each customer's second-highest order (by order value = items_price). [ROW_NUMBER]
--      Use: vw_order_facts. Customers with 1 order don't appear.
--      Output: customer_unique_id, customer_orders, highest_order_value, second_highest_value.
--      What if a customer's two biggest orders have the same value?


-- ─── Classic interview variations ───────────────────────────────────────────

-- V1. Rank customer states by NUMBER OF SELLERS (table: sellers, column seller_state) three ways
--     in one query: ROW_NUMBER, RANK, DENSE_RANK. Look at the states with 5, 2 and 1 sellers.
--     What's the last number each function gives?


-- V2. The second-highest ORDER VALUE in the whole database (vw_order_facts.items_price, valid
--     orders). Solve it three ways: (a) subquery with MAX, (b) DENSE_RANK, (c) LIMIT/OFFSET.
--     Which ones break when the top value is tied?


-- V3. WHERE vs HAVING (vw_item_facts, valid orders, categories with at least 100 items):
--     (a) categories whose AVERAGE item price is above R$200
--     (b) categories with at least 100 items that EACH cost above R$200
--     Why do they give different counts?
