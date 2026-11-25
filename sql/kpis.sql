-- =============================================================================
-- kpis.sql  —  Step 2.5
-- The 10 KPI views the monthly report (Step 3.1) reads: pd.read_sql("SELECT * FROM vw_kpi_...").
-- Built ONLY on the base views (vw_order_facts = 1 row per order, vw_item_facts = 1 row per
-- item line), which already solved fan-out (Issue #33), so no KPI joins raw fact tables.
--
-- Applied by db.get_connection() on every connection, after views.sql (these views read
-- those views). Re-runnable: DROP VIEW IF EXISTS + CREATE VIEW, inside BEGIN/COMMIT.
--
-- ─── DEFINITIONS (say these out loud in an interview; every number depends on them) ───
--
-- REVENUE  = SUM(price) of the order's items = product revenue, EXCLUDING freight.
--            Why: freight is passed through to the carrier, so it isn't the marketplace's
--            sales. payment_value isn't used either: it includes freight + installment interest.
--            (Price + freight = items_total; KPI #1 shows it as a second column for comparison.)
--            Money is REAL (floating point) → ROUND(…, 2) only in the final SELECT.
--
-- VALID ORDER = order_status NOT IN ('canceled', 'unavailable')  — the order never happened.
--            Kept: delivered, shipped, invoiced, processing, approved, created (booked sales).
--            Excluded on this DB: 461 canceled + 580 unavailable = 1,041 orders (R$ 72,036).
--            order_status has 0 NULLs (checked). If it had any, NOT IN would silently drop them
--            (NULL NOT IN (…) is NULL, not TRUE).
--
-- MONTH    = purchase_month ('YYYY-MM') = when the customer ordered, not when it was delivered.
--
-- LATE     = calendar-day comparison: delivered DATE > estimated DATE (clean.py's is_late).
--            Delivering at 15:00 ON the promised day is on time. (Timestamp comparison would
--            say 7,827 late instead of 6,535 in the raw data; see journal open items.)
--            Delivery KPIs use only: is_delivered = 1 AND is_late IS NOT NULL, and exclude the
--            two timestamp-logic flags (pickup before purchase, delivered before pickup: 60
--            delivered orders), whose dates can't be trusted.
--            flag_malformed_date is NOT excluded: when the corrupted date was the delivery date,
--            is_late is already NULL (397 orders); when it was approved_at (383 orders), is_late
--            is still correct, and dropping them would lose good data.
--
-- CUSTOMER = customer_unique_id (the person). customer_id is generated per order, so with it
--            nobody would ever be a repeat customer (KPI #6 shows that as a proof).
--
-- SCOPE    = the 18 months loaded so far: 2016-10, 2017-01 … 2018-05 (80,445 orders).
--            The 3 holdback months (2018-06/07/08) aren't loaded yet; the views include them
--            automatically after the Step 3.4 demo, because views are live.
--
-- THE GAP  = 2016-11 and 2016-12 were excluded as thin months (Issue #10), so the loaded months
--            go 2016-10 → 2017-01. LAG() returns "the previous ROW", not "the previous MONTH",
--            so a naive MoM would compare January 2017 with October 2016 and call it +x% growth.
--            Fix: month_idx = year * 12 + month (a month number you can subtract). MoM is only
--            calculated when month_idx - previous month_idx = 1; otherwise NULL + a note.
--            Same idea for the 3-month average (KPI #3: RANGE frame on month_idx).
--
-- ORDER BY inside a view: SQLite keeps it, so SELECT * returns rows in report order. Standard
-- SQL says a view's row order isn't guaranteed: anyone querying the view should still ORDER BY.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- KPI #1  vw_kpi_monthly_revenue  —  monthly revenue and month-over-month growth %
-- GRAIN: one row per month.  Skill: LAG()
--
-- LAG(x) OVER (ORDER BY m) = the value of x in the previous row (Excel: the cell above, =B2).
-- Two-step pattern: aggregate per month in a CTE first, THEN apply the window function,
-- because a window function runs after GROUP BY (you can't LAG raw order rows into months).
-- Excel: =(B3-B2)/B2 dragged down · pandas: df.revenue.pct_change()
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_kpi_monthly_revenue;
CREATE VIEW vw_kpi_monthly_revenue AS
WITH monthly AS (
    SELECT
        purchase_month,
        -- 'YYYY-MM' → a number where consecutive months differ by exactly 1 (2017-01 → 2017*12+1)
        CAST(substr(purchase_month, 1, 4) AS INTEGER) * 12
          + CAST(substr(purchase_month, 6, 2) AS INTEGER)      AS month_idx,
        COUNT(*)                                               AS orders,
        SUM(items_price)                                       AS revenue,
        SUM(items_total)                                       AS revenue_incl_freight
    FROM vw_order_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
    GROUP BY purchase_month
),
with_previous AS (
    SELECT
        m.*,
        LAG(revenue)   OVER (ORDER BY month_idx) AS prev_revenue,
        LAG(month_idx) OVER (ORDER BY month_idx) AS prev_month_idx
    FROM monthly AS m
)
SELECT
    purchase_month,
    orders,
    ROUND(revenue, 2)                                          AS revenue,
    ROUND(revenue_incl_freight, 2)                             AS revenue_incl_freight,
    ROUND(revenue / orders, 2)                                 AS avg_order_value,
    -- only compare with the previous row when it really IS the previous calendar month
    CASE WHEN month_idx - prev_month_idx = 1
         THEN ROUND(prev_revenue, 2) END                       AS prev_month_revenue,
    CASE WHEN month_idx - prev_month_idx = 1
         THEN ROUND(100.0 * (revenue - prev_revenue) / prev_revenue, 1) END AS mom_growth_pct,
    CASE WHEN prev_month_idx IS NULL            THEN 'first month loaded'
         WHEN month_idx - prev_month_idx > 1    THEN 'previous month not loaded (gap)'
    END                                                        AS mom_note
FROM with_previous
ORDER BY purchase_month;


-- -----------------------------------------------------------------------------
-- KPI #2  vw_kpi_running_revenue  —  cumulative revenue since the first loaded month
-- GRAIN: one row per month.  Skill: SUM() OVER (ORDER BY …)
--
-- Adding ORDER BY inside OVER turns a total into a running total: each row sums from the
-- first row up to itself. The frame is written out (ROWS BETWEEN UNBOUNDED PRECEDING AND
-- CURRENT ROW) because the DEFAULT frame with ORDER BY is RANGE, which treats rows with the
-- same ORDER BY value as one block (ties get the same, jumped total). Months are unique here,
-- so both give the same answer, but ROWS is the habit that's always right.
-- The gap doesn't matter here: a running total has no "previous month" comparison.
-- Excel: =SUM($B$2:B2) dragged down · pandas: df.revenue.cumsum()
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_kpi_running_revenue;
CREATE VIEW vw_kpi_running_revenue AS
WITH monthly AS (
    SELECT purchase_month, SUM(items_price) AS revenue
    FROM vw_order_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
    GROUP BY purchase_month
)
SELECT
    purchase_month,
    ROUND(revenue, 2)                                                    AS revenue,
    ROUND(SUM(revenue) OVER (ORDER BY purchase_month
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 2) AS running_revenue,
    -- running total as a share of the whole period: "by 2017-12 we had x% of all revenue"
    ROUND(100.0 * SUM(revenue) OVER (ORDER BY purchase_month
                                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                / SUM(revenue) OVER (), 1)                               AS running_pct_of_total
FROM monthly
ORDER BY purchase_month;


-- -----------------------------------------------------------------------------
-- KPI #3  vw_kpi_orders_moving_avg  —  3-month moving average of order count
-- GRAIN: one row per month.  Skill: window FRAME (ROWS vs RANGE)
--
-- ROWS BETWEEN 2 PRECEDING AND CURRENT ROW = this row + the 2 rows above it (Excel:
-- =AVERAGE(B2:B4) dragged down; pandas: rolling(3).mean()). Two traps:
--   1. The first two rows average only 1 and 2 months, but still look like "3-month" numbers
--      (pandas rolling(3) gives NaN there; SQL silently averages fewer rows).
--   2. Across the gap, "3 rows" = 2016-10, 2017-01, 2017-02, which isn't 3 consecutive months.
-- So two columns:
--   moving_avg_3_rows  the ROWS frame, as asked (shown for teaching; don't chart it blindly)
--   moving_avg_3m      a RANGE frame on month_idx: RANGE BETWEEN 2 PRECEDING = rows whose
--                      month_idx is within 2 of this one, i.e. 3 CALENDAR months. Missing
--                      months simply aren't in the window. NULL unless all 3 months are loaded.
-- The WINDOW clause names a window once and reuses it (SQLite 3.28+, Postgres, MySQL 8).
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_kpi_orders_moving_avg;
CREATE VIEW vw_kpi_orders_moving_avg AS
WITH monthly AS (
    SELECT
        purchase_month,
        CAST(substr(purchase_month, 1, 4) AS INTEGER) * 12
          + CAST(substr(purchase_month, 6, 2) AS INTEGER) AS month_idx,
        COUNT(*)                                          AS orders
    FROM vw_order_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
    GROUP BY purchase_month
)
SELECT
    purchase_month,
    orders,
    ROUND(AVG(orders) OVER w_rows, 1)                     AS moving_avg_3_rows,
    COUNT(*)          OVER w_cal                          AS calendar_months_in_window,
    CASE WHEN COUNT(*) OVER w_cal = 3
         THEN ROUND(AVG(orders) OVER w_cal, 1) END        AS moving_avg_3m
FROM monthly
WINDOW
    w_rows AS (ORDER BY month_idx ROWS  BETWEEN 2 PRECEDING AND CURRENT ROW),
    w_cal  AS (ORDER BY month_idx RANGE BETWEEN 2 PRECEDING AND CURRENT ROW)
ORDER BY purchase_month;


-- -----------------------------------------------------------------------------
-- KPI #4  vw_kpi_top_categories_by_state  —  top 3 product categories per customer state
-- GRAIN: one row per (state, category) in the top 3.  Skill: CTE + DENSE_RANK() PARTITION BY
--
-- PARTITION BY customer_state restarts the ranking for each state (GROUP BY would collapse
-- the rows; PARTITION BY keeps them and adds a column).
-- Why a CTE: window functions are computed AFTER WHERE, so "WHERE DENSE_RANK() … <= 3" is an
-- error. Rank in a CTE, filter in the outer query. (Postgres/Snowflake: QUALIFY; not SQLite.)
-- DENSE_RANK, not ROW_NUMBER: if two categories tie for 3rd, both are shown (top 3 can return
-- 4 rows). RANK would also show both, but then skip the next number (1, 2, 2, 4).
-- Ranked on ROUND(revenue, 2): float sums that are equal in cents can differ in the 10th
-- decimal (Issue #36), which would hide a real tie.
-- 'unknown' category (1,501 items without a category) is excluded: it isn't something a
-- manager can act on. KPI #8 keeps it, so its percentages add up to 100.
-- Revenue from vw_item_facts (item grain): category lives on the item, not on the order.
-- Excel: PivotTable rows = state, category, value = SUM(price), filter Top 3 per state
-- pandas: g.groupby('state')['revenue'].rank(method='dense', ascending=False) <= 3
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_kpi_top_categories_by_state;
CREATE VIEW vw_kpi_top_categories_by_state AS
WITH state_category AS (
    SELECT
        customer_state,
        category,
        COUNT(DISTINCT order_id)   AS orders,      -- DISTINCT: an order can have 2 items of one category
        ROUND(SUM(price), 2)       AS revenue
    FROM vw_item_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
      AND category <> 'unknown'
    GROUP BY customer_state, category
),
ranked AS (
    SELECT
        s.*,
        DENSE_RANK() OVER (PARTITION BY customer_state ORDER BY revenue DESC) AS category_rank,
        SUM(revenue) OVER (PARTITION BY customer_state)                       AS state_revenue
    FROM state_category AS s
)
SELECT
    customer_state,
    category_rank,
    category,
    orders,
    revenue,
    ROUND(100.0 * revenue / state_revenue, 1) AS pct_of_state_revenue
FROM ranked
WHERE category_rank <= 3
ORDER BY customer_state, category_rank, category;


-- -----------------------------------------------------------------------------
-- KPI #5  vw_kpi_delivery_by_state  —  late-delivery rate by state; avg review late vs on time
-- GRAIN: one row per customer state + one 'ALL' row.  Skill: CASE inside AVG, GROUP BY
--
-- AVG(is_late) on a 0/1 column = the share of 1s = late rate (Excel: =AVERAGE(flag_col)).
-- AVG(CASE WHEN is_late = 1 THEN review_score END): no ELSE → NULL for on-time rows, and AVG
-- ignores NULLs, so this averages late orders only. That's conditional aggregation: one pass,
-- several filtered averages side by side (Excel: AVERAGEIFS; pandas: groupby + mean on a mask).
-- Orders without a review (review_score NULL) drop out of the review averages automatically;
-- they still count in the late rate.
-- The 'ALL' row: SQLite has no GROUP BY ROLLUP (Postgres/SQL Server/MySQL do), so the national
-- total is a second SELECT glued on with UNION ALL (not UNION: no need to de-duplicate).
-- Small states (e.g. RR, AP: a few dozen deliveries) swing a lot; the report should show the
-- delivered_orders column next to the rate.
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_kpi_delivery_by_state;
CREATE VIEW vw_kpi_delivery_by_state AS
WITH deliveries AS (                 -- one row per trustworthy delivered order
    SELECT customer_state, is_late, review_score
    FROM vw_order_facts
    WHERE is_delivered = 1                     -- 6 canceled orders have is_late but weren't delivered
      AND is_late IS NOT NULL
      AND flag_pickup_before_purchase  = 0     -- timestamps that contradict each other
      AND flag_delivered_before_pickup = 0
),
by_state AS (
    SELECT
        customer_state                                         AS state,
        COUNT(*)                                               AS delivered_orders,
        SUM(is_late)                                           AS late_orders,
        ROUND(100.0 * AVG(is_late), 2)                         AS late_rate_pct,
        ROUND(AVG(CASE WHEN is_late = 0 THEN review_score END), 2) AS avg_review_on_time,
        ROUND(AVG(CASE WHEN is_late = 1 THEN review_score END), 2) AS avg_review_late
    FROM deliveries
    GROUP BY customer_state
    UNION ALL
    SELECT
        'ALL',
        COUNT(*),
        SUM(is_late),
        ROUND(100.0 * AVG(is_late), 2),
        ROUND(AVG(CASE WHEN is_late = 0 THEN review_score END), 2),
        ROUND(AVG(CASE WHEN is_late = 1 THEN review_score END), 2)
    FROM deliveries
)
SELECT
    b.*,
    ROUND(avg_review_on_time - avg_review_late, 2) AS review_drop_when_late
FROM by_state AS b
-- 'ALL' first, then worst state first. (A compound SELECT can only ORDER BY its output
-- columns, which is one reason the UNION ALL sits in a CTE and the sort happens out here.)
ORDER BY (state = 'ALL') DESC, late_rate_pct DESC;


-- -----------------------------------------------------------------------------
-- KPI #6  vw_kpi_repeat_customers  —  repeat customer rate
-- GRAIN: one row (a scorecard).  Skill: CTE + COUNT per customer_unique_id
--
-- Step 1 (CTE): orders per real person. Step 2: count people with 2+ orders.
-- SUM(orders >= 2): in SQLite a comparison is 0/1, so SUM counts the TRUEs (portable SQL:
-- SUM(CASE WHEN orders >= 2 THEN 1 ELSE 0 END); Excel: =COUNTIF(C:C,">=2")).
-- The last column proves why customer_unique_id matters: per customer_id, every "customer"
-- has exactly 1 order, so the repeat rate would be 0%.
-- Caveat: 18 months is short for repeat behaviour, and Olist is mostly one-off purchases.
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_kpi_repeat_customers;
CREATE VIEW vw_kpi_repeat_customers AS
WITH valid_orders AS (
    SELECT order_id, customer_id, customer_unique_id
    FROM vw_order_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
),
per_customer AS (
    SELECT customer_unique_id, COUNT(*) AS orders
    FROM valid_orders
    GROUP BY customer_unique_id
),
per_customer_id AS (                 -- the WRONG grain, kept only as a comparison
    SELECT customer_id, COUNT(*) AS orders
    FROM valid_orders
    GROUP BY customer_id
)
SELECT
    COUNT(*)                                         AS customers,
    SUM(orders >= 2)                                 AS repeat_customers,
    ROUND(100.0 * SUM(orders >= 2) / COUNT(*), 2)    AS repeat_rate_pct,
    SUM(orders)                                      AS orders,
    ROUND(1.0 * SUM(orders) / COUNT(*), 3)           AS orders_per_customer,
    MAX(orders)                                      AS max_orders_one_customer,
    (SELECT ROUND(100.0 * SUM(orders >= 2) / COUNT(*), 2)
     FROM per_customer_id)                           AS repeat_rate_if_customer_id_pct
FROM per_customer;


-- -----------------------------------------------------------------------------
-- KPI #7  vw_kpi_cohort_retention  —  retention by first-purchase month (cohort)
-- GRAIN: one row per (cohort_month, months_since_first) that has any customer.
-- Skill: MIN() OVER (PARTITION BY …), chained CTEs
--
-- 1. customer_months: which months each person bought in (DISTINCT: 2 orders in one month = 1).
-- 2. with_cohort: MIN(month) OVER (PARTITION BY person) writes the person's FIRST month on
--    every one of their rows, without collapsing them (GROUP BY would collapse; then you'd
--    have to join back). That first month is the cohort.
-- 3. months_since_first = month_idx - cohort_idx (calendar months, so the gap is handled).
-- 4. retention % = people active in month N / people in the cohort (month 0 = 100%).
--    Cohort size: a window over the aggregated rows (a window function can read GROUP BY output).
-- Long format (one row per cell). The report pivots it into the classic triangle
-- (Excel: PivotTable rows = cohort, columns = months_since_first; pandas: pivot_table).
-- Caveats: the 2016-10 cohort's months 1–2 (Nov–Dec 2016) weren't loaded, so they're missing,
-- not 0%. Late cohorts have few months to come back in (2018-05 has only month 0).
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_kpi_cohort_retention;
CREATE VIEW vw_kpi_cohort_retention AS
WITH customer_months AS (
    SELECT DISTINCT
        customer_unique_id,
        purchase_month,
        CAST(substr(purchase_month, 1, 4) AS INTEGER) * 12
          + CAST(substr(purchase_month, 6, 2) AS INTEGER) AS month_idx
    FROM vw_order_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
),
with_cohort AS (
    SELECT
        cm.*,
        MIN(purchase_month) OVER (PARTITION BY customer_unique_id) AS cohort_month,
        MIN(month_idx)      OVER (PARTITION BY customer_unique_id) AS cohort_idx
    FROM customer_months AS cm
),
cohort_activity AS (
    SELECT
        cohort_month,
        month_idx - cohort_idx           AS months_since_first,
        COUNT(*)                         AS active_customers   -- 1 row per person per month
    FROM with_cohort
    GROUP BY cohort_month, months_since_first
)
SELECT
    cohort_month,
    months_since_first,
    active_customers,
    cohort_customers,
    ROUND(100.0 * active_customers / cohort_customers, 2) AS retention_pct
FROM (
    SELECT
        a.*,
        -- cohort size = active customers in month 0, copied onto every row of that cohort.
        -- (Alternative: self-join cohort_activity to its own month-0 row. Same result, but
        -- the CTE is then used twice, so SQLite materializes it up front: the LIMIT 0 check
        -- below took 650 ms instead of < 1 ms. Measured in Step 2.5.)
        MAX(CASE WHEN months_since_first = 0 THEN active_customers END)
            OVER (PARTITION BY cohort_month) AS cohort_customers
    FROM cohort_activity AS a
)
ORDER BY cohort_month, months_since_first;


-- -----------------------------------------------------------------------------
-- KPI #8  vw_kpi_category_share  —  each category's % of total revenue
-- GRAIN: one row per category.  Skill: SUM() OVER ()
--
-- OVER () with nothing inside = "the window is the whole result", so SUM(revenue) OVER ()
-- puts the grand total on every row and you can divide by it in the same SELECT. Without it
-- you'd need a subquery: revenue / (SELECT SUM(revenue) FROM …).
-- Excel: =B2/SUM($B$2:$B$75) · pandas: df.revenue / df.revenue.sum()
-- cumulative_pct (SUM OVER ORDER BY revenue DESC) gives the Pareto view: "the top N
-- categories make 80% of revenue". The 100.0 forces decimal division (integer 5/2 = 2 in SQL).
-- RANK here (not DENSE_RANK) because it's a leaderboard: two at #3 → the next one is #5.
-- Reconciliation: SUM(revenue) here = SUM(revenue) in KPI #1 (same filter, same price column).
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_kpi_category_share;
CREATE VIEW vw_kpi_category_share AS
WITH category_revenue AS (
    SELECT
        category,
        COUNT(DISTINCT order_id) AS orders,
        COUNT(*)                 AS items,
        SUM(price)               AS revenue
    FROM vw_item_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
    GROUP BY category
)
SELECT
    RANK() OVER (ORDER BY revenue DESC)                       AS revenue_rank,
    category,
    orders,
    items,
    ROUND(revenue, 2)                                         AS revenue,
    ROUND(100.0 * revenue / SUM(revenue) OVER (), 2)          AS pct_of_total,
    ROUND(100.0 * SUM(revenue) OVER (ORDER BY revenue DESC, category
                                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                / SUM(revenue) OVER (), 2)                    AS cumulative_pct
FROM category_revenue
ORDER BY revenue DESC, category;


-- -----------------------------------------------------------------------------
-- KPI #9  vw_kpi_top_sellers  —  top 10% of sellers by revenue
-- GRAIN: one row per seller in the top decile.  Skill: NTILE(10)
--
-- NTILE(10) OVER (ORDER BY revenue DESC) deals the sorted rows into 10 buckets as evenly as
-- possible; bucket 1 = the top 10%. With 2,5xx sellers the buckets differ by at most 1 row
-- (the first buckets get the extra rows). NTILE splits by ROW COUNT, not by value: two sellers
-- with the same revenue can land in different buckets, so seller_id is added to the ORDER BY
-- to make the split repeatable.
-- Alternative: PERCENT_RANK() <= 0.1 / CUME_DIST() (value-based; ties stay together).
-- Excel: =PERCENTRANK.INC() or sort and take the top ROUNDUP(COUNT/10) rows
-- pandas: pd.qcut(df.revenue.rank(method='first', ascending=False), 10, labels=False)
-- The window columns are computed in the CTE, BEFORE the WHERE keeps bucket 1, so the total
-- and cumulative share use ALL sellers (a window in the outer query would only see bucket 1).
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_kpi_top_sellers;
CREATE VIEW vw_kpi_top_sellers AS
WITH seller_revenue AS (
    SELECT
        seller_id,
        seller_state,
        COUNT(DISTINCT order_id) AS orders,
        SUM(price)               AS revenue
    FROM vw_item_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
    GROUP BY seller_id, seller_state            -- seller_state: 1 per seller, so no extra rows
),
bucketed AS (
    SELECT
        s.*,
        NTILE(10) OVER (ORDER BY revenue DESC, seller_id)              AS revenue_decile,
        ROW_NUMBER() OVER (ORDER BY revenue DESC, seller_id)           AS seller_rank,
        COUNT(*)  OVER ()                                              AS total_sellers,
        100.0 * revenue / SUM(revenue) OVER ()                         AS pct_of_total,
        100.0 * SUM(revenue) OVER (ORDER BY revenue DESC, seller_id
                                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
              / SUM(revenue) OVER ()                                   AS cumulative_pct
    FROM seller_revenue AS s
)
SELECT
    seller_rank,
    seller_id,
    seller_state,
    orders,
    ROUND(revenue, 2)        AS revenue,
    ROUND(pct_of_total, 3)   AS pct_of_total,
    ROUND(cumulative_pct, 2) AS cumulative_pct,   -- last row = the top 10%'s share of revenue
    total_sellers
FROM bucketed
WHERE revenue_decile = 1
ORDER BY seller_rank;


-- -----------------------------------------------------------------------------
-- KPI #10  vw_kpi_second_highest_order  —  each repeat customer's second-highest order
-- GRAIN: one row per customer with 2+ valid orders.  Skill: ROW_NUMBER() PARTITION BY
--
-- ROW_NUMBER numbers each person's orders 1, 2, 3… from the highest value down; keep row 2.
-- Customers with 1 order have no row 2, so they drop out (that's correct: no second order).
-- Order value = items_price (same revenue definition as everywhere else).
-- Ties: ROW_NUMBER always gives distinct numbers, so if a person's two biggest orders are
-- equal, row 2 has the SAME value as row 1 ("the second-highest ORDER"). For "the second-highest
-- distinct VALUE", use DENSE_RANK() = 2 instead (see practice_kpis_solutions.sql, variation A).
-- tied_with_highest shows which rows that applies to. Tie-breakers in ORDER BY (time, then id)
-- make ROW_NUMBER deterministic: without them, re-running could pick a different tied order.
-- pandas: df.sort_values('value', ascending=False).groupby('cust').nth(1)
-- Excel: =LARGE(IF(cust=A2, value), 2) (array formula) or MAXIFS + a helper column
-- -----------------------------------------------------------------------------
DROP VIEW IF EXISTS vw_kpi_second_highest_order;
CREATE VIEW vw_kpi_second_highest_order AS
WITH ranked AS (
    SELECT
        customer_unique_id,
        order_id,
        purchase_month,
        items_price                                                         AS order_value,
        ROW_NUMBER() OVER (PARTITION BY customer_unique_id
                           ORDER BY items_price DESC, order_purchase_timestamp, order_id) AS value_rank,
        COUNT(*)         OVER (PARTITION BY customer_unique_id)             AS customer_orders,
        MAX(items_price) OVER (PARTITION BY customer_unique_id)             AS highest_order_value
    FROM vw_order_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
)
SELECT
    customer_unique_id,
    customer_orders,
    ROUND(highest_order_value, 2)                    AS highest_order_value,
    order_id                                         AS second_order_id,
    purchase_month                                   AS second_order_month,
    ROUND(order_value, 2)                            AS second_highest_value,
    (ROUND(order_value, 2) = ROUND(highest_order_value, 2)) AS tied_with_highest   -- 1 / 0
FROM ranked
WHERE value_rank = 2
ORDER BY second_highest_value DESC, customer_unique_id;


-- -----------------------------------------------------------------------------
-- Validate before COMMIT (same reason as views.sql, Issue #35): SQLite only checks a view's
-- SQL when it's queried. LIMIT 0 compiles each view without running it, so a typo fails here,
-- inside the transaction, db.py rolls back, and the previous KPI views stay.
-- -----------------------------------------------------------------------------
SELECT * FROM vw_kpi_monthly_revenue          LIMIT 0;
SELECT * FROM vw_kpi_running_revenue          LIMIT 0;
SELECT * FROM vw_kpi_orders_moving_avg        LIMIT 0;
SELECT * FROM vw_kpi_top_categories_by_state  LIMIT 0;
SELECT * FROM vw_kpi_delivery_by_state        LIMIT 0;
SELECT * FROM vw_kpi_repeat_customers         LIMIT 0;
SELECT * FROM vw_kpi_cohort_retention         LIMIT 0;
SELECT * FROM vw_kpi_category_share           LIMIT 0;
SELECT * FROM vw_kpi_top_sellers              LIMIT 0;
SELECT * FROM vw_kpi_second_highest_order     LIMIT 0;

COMMIT;
