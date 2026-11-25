-- =============================================================================
-- practice_kpis_solutions.sql  —  Step 2.5 practice pack (SOLUTIONS)
-- Every solution is a standalone SELECT: paste it into DB Browser (Execute SQL) and run it.
-- They're the same logic as the vw_kpi_* views in sql/kpis.sql, minus CREATE VIEW, so you
-- can also check yourself with:  SELECT * FROM vw_kpi_monthly_revenue;  etc.
-- Expected numbers = the 18 loaded months (80,445 orders), measured on Nov 25, 2026.
-- tests/test_kpis.py re-runs every solution and checks it still matches its view.
--
-- Shared rules: revenue = SUM(price) (no freight) · valid = status NOT IN ('canceled',
-- 'unavailable') · customer = customer_unique_id · late = calendar-day is_late.
-- After filtering: 79,404 valid orders, revenue R$ 10,903,867.08.
-- =============================================================================


-- =============================================================================
-- Q1. Monthly revenue and MoM growth %                                   [LAG]
-- =============================================================================
WITH monthly AS (
    SELECT
        purchase_month,
        CAST(substr(purchase_month, 1, 4) AS INTEGER) * 12
          + CAST(substr(purchase_month, 6, 2) AS INTEGER) AS month_idx,
        COUNT(*)         AS orders,
        SUM(items_price) AS revenue
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
    ROUND(revenue, 2) AS revenue,
    CASE WHEN month_idx - prev_month_idx = 1
         THEN ROUND(100.0 * (revenue - prev_revenue) / prev_revenue, 1) END AS mom_growth_pct
FROM with_previous
ORDER BY purchase_month;
-- HOW IT WORKS
--   1. CTE `monthly`: GROUP BY month → 18 rows. Window functions run AFTER GROUP BY, so you
--      aggregate first, then LAG the monthly totals.
--   2. LAG(revenue) OVER (ORDER BY month_idx) = last row's revenue (Excel: the cell above).
--   3. month_idx turns 'YYYY-MM' into a number, so "is the previous row really last month?"
--      becomes month_idx - prev_month_idx = 1. 2017-01 → 2016-10 differs by 3 → NULL.
--   4. 100.0 (not 100) forces decimal division; integer division would truncate.
-- COMMON MISTAKES
--   • LAG on raw order rows instead of monthly totals (gives the previous ORDER's value).
--   • Trusting LAG across a gap: naive MoM for 2017-01 would be +169.8% "growth" vs October.
--   • Dividing by prev_revenue when it's 0 (not possible here, but use NULLIF(prev, 0) in general).
-- EXPECTED (18 rows)
--   2016-10   293 orders   44,507.30   NULL (first month)
--   2017-01   787         120,098.27   NULL (gap)
--   2017-02 1,718         244,959.35  +104.0
--   2017-11 7,423       1,003,862.14   +52.1   ← Black Friday
--   2017-12 5,620         742,183.79   -26.1
--   2018-05 6,833         992,871.75    -0.1
--   Sum of orders = 79,404; sum of revenue = 10,903,867.08.


-- =============================================================================
-- Q2. Running total of revenue                                [SUM() OVER (ORDER BY)]
-- =============================================================================
WITH monthly AS (
    SELECT purchase_month, SUM(items_price) AS revenue
    FROM vw_order_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
    GROUP BY purchase_month
)
SELECT
    purchase_month,
    ROUND(revenue, 2) AS revenue,
    ROUND(SUM(revenue) OVER (ORDER BY purchase_month
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 2) AS running_revenue,
    ROUND(100.0 * SUM(revenue) OVER (ORDER BY purchase_month
                                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                / SUM(revenue) OVER (), 1) AS running_pct_of_total
FROM monthly
ORDER BY purchase_month;
-- HOW IT WORKS
--   SUM(x) OVER ()                  = grand total on every row
--   SUM(x) OVER (ORDER BY month …)  = total from the first row up to this one (running total)
--   'YYYY-MM' text sorts in date order, so ORDER BY purchase_month is safe here.
-- COMMON MISTAKES
--   • Leaving out the frame. With ORDER BY, the default is RANGE BETWEEN UNBOUNDED PRECEDING
--     AND CURRENT ROW: rows with the SAME sort value are summed together, so tied rows all show
--     the total including each other (a jump). Months are unique, so it doesn't bite here, but
--     on daily data with duplicate dates it does. Write ROWS explicitly.
--   • Running total over raw orders and then GROUP BY (mixing grains).
-- EXPECTED: 2016-10 44,507.30 (0.4%) · 2017-04 1,131,749.22 (10.4%) · last row 2018-05
--   10,903,867.08 (100.0%).


-- =============================================================================
-- Q3. 3-month moving average of order count        [ROWS BETWEEN 2 PRECEDING AND CURRENT ROW]
-- =============================================================================
WITH monthly AS (
    SELECT
        purchase_month,
        CAST(substr(purchase_month, 1, 4) AS INTEGER) * 12
          + CAST(substr(purchase_month, 6, 2) AS INTEGER) AS month_idx,
        COUNT(*) AS orders
    FROM vw_order_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
    GROUP BY purchase_month
)
SELECT
    purchase_month,
    orders,
    ROUND(AVG(orders) OVER (ORDER BY month_idx
                            ROWS BETWEEN 2 PRECEDING AND CURRENT ROW), 1)  AS moving_avg_3_rows,
    CASE WHEN COUNT(*) OVER (ORDER BY month_idx RANGE BETWEEN 2 PRECEDING AND CURRENT ROW) = 3
         THEN ROUND(AVG(orders) OVER (ORDER BY month_idx
                                      RANGE BETWEEN 2 PRECEDING AND CURRENT ROW), 1)
    END                                                                    AS moving_avg_3m
FROM monthly
ORDER BY purchase_month;
-- HOW IT WORKS
--   ROWS  BETWEEN 2 PRECEDING AND CURRENT ROW = this row + the 2 rows above (counts ROWS).
--   RANGE BETWEEN 2 PRECEDING AND CURRENT ROW = rows whose ORDER BY value is within 2 of this
--   row's value (counts VALUES). On month_idx that means "the last 3 calendar months", and a
--   missing month just isn't in the window. COUNT(*) over the same window says how many
--   months were really there; we only show the average when it's 3.
--   (RANGE with a number needs SQLite 3.28+, Postgres 11+; MySQL 8 supports it too.)
-- COMMON MISTAKES
--   • Trusting the first two rows: 293.0 and 540.0 are 1- and 2-month averages labelled "3-month".
--   • The gap: for 2017-02 the ROWS window is 2016-10 + 2017-01 + 2017-02 = 932.7, which is not
--     a 3-month average. The RANGE version gives NULL (only 2 of the 3 months exist).
--   • AVG of an integer column is fine in SQLite (returns REAL); in SQL Server AVG(int) is an
--     integer: use AVG(orders * 1.0).
-- EXPECTED
--   month     orders  3_rows   3m
--   2016-10     293    293.0   NULL
--   2017-01     787    540.0   NULL
--   2017-02   1,718    932.7   NULL
--   2017-03   2,617  1,707.3  1,707.3   (first real 3-month average: Jan–Mar)
--   2017-05   3,640  2,878.0  2,878.0   (= (2,617 + 2,377 + 3,640) / 3)
--   2017-11   7,423  5,399.0  5,399.0
--   2018-05   6,833  6,973.3  6,973.3


-- =============================================================================
-- Q4. Top 3 categories per state                           [CTE + DENSE_RANK PARTITION BY]
-- =============================================================================
WITH state_category AS (
    SELECT
        customer_state,
        category,
        ROUND(SUM(price), 2) AS revenue
    FROM vw_item_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
      AND category <> 'unknown'
    GROUP BY customer_state, category
),
ranked AS (
    SELECT
        s.*,
        DENSE_RANK() OVER (PARTITION BY customer_state ORDER BY revenue DESC) AS category_rank
    FROM state_category AS s
)
SELECT customer_state, category_rank, category, revenue
FROM ranked
WHERE category_rank <= 3
ORDER BY customer_state, category_rank, category;
-- HOW IT WORKS
--   1. CTE: revenue per (state, category).
--   2. DENSE_RANK() OVER (PARTITION BY state ORDER BY revenue DESC): a separate 1, 2, 3 … per state.
--   3. Filter rank <= 3 in the OUTER query.
-- COMMON MISTAKES
--   • WHERE DENSE_RANK() OVER (…) <= 3 in the same SELECT: error. SQL's logical order is
--     FROM → WHERE → GROUP BY → HAVING → window functions → SELECT → ORDER BY, so WHERE runs
--     before the rank exists. (Postgres/Snowflake/BigQuery have QUALIFY for this; SQLite doesn't.)
--   • ORDER BY … LIMIT 3: gives the top 3 overall, not per state.
--   • ROW_NUMBER: drops a category tied for 3rd. DENSE_RANK keeps both (4 rows for that state).
--   • Ranking unrounded float sums: equal-in-cents totals can differ in the 10th decimal.
-- EXPECTED: 81 rows = 27 states × 3 (no ties on this data).
--   SP: 1 bed_bath_table 374,947.00 · 2 watches_gifts 345,667.11 · 3 health_beauty 331,866.95
--   RJ: 1 watches_gifts 146,570.10 · MG: 1 health_beauty 112,470.86
--   #1 category by state: health_beauty in 11 states, watches_gifts in 7, sports_leisure in 4.


-- =============================================================================
-- Q5. Late rate by state + avg review late vs on time              [CASE inside AVG]
-- =============================================================================
WITH deliveries AS (
    SELECT customer_state, is_late, review_score
    FROM vw_order_facts
    WHERE is_delivered = 1
      AND is_late IS NOT NULL
      AND flag_pickup_before_purchase  = 0
      AND flag_delivered_before_pickup = 0
),
by_state AS (
    SELECT
        customer_state                                                AS state,
        COUNT(*)                                                      AS delivered_orders,
        SUM(is_late)                                                  AS late_orders,
        ROUND(100.0 * AVG(is_late), 2)                                AS late_rate_pct,
        ROUND(AVG(CASE WHEN is_late = 0 THEN review_score END), 2)    AS avg_review_on_time,
        ROUND(AVG(CASE WHEN is_late = 1 THEN review_score END), 2)    AS avg_review_late
    FROM deliveries
    GROUP BY customer_state
    UNION ALL
    SELECT 'ALL', COUNT(*), SUM(is_late), ROUND(100.0 * AVG(is_late), 2),
           ROUND(AVG(CASE WHEN is_late = 0 THEN review_score END), 2),
           ROUND(AVG(CASE WHEN is_late = 1 THEN review_score END), 2)
    FROM deliveries
)
SELECT *
FROM by_state
ORDER BY (state = 'ALL') DESC, late_rate_pct DESC;
-- HOW IT WORKS
--   • AVG of a 0/1 column = the share of 1s (late rate). ×100 for a percentage.
--   • AVG(CASE WHEN is_late = 1 THEN review_score END): no ELSE → NULL for on-time rows, and
--     AVG skips NULLs, so it's the average for late orders only. "Conditional aggregation":
--     several filtered averages in one GROUP BY. Excel: AVERAGEIFS.
--   • 'ALL' row via UNION ALL because SQLite has no GROUP BY ROLLUP.
--   • (state = 'ALL') DESC: a comparison is 1/0 in SQLite, so the ALL row sorts first.
-- COMMON MISTAKES
--   • ELSE 0 in the CASE: on-time rows then count as review score 0 and drag the average down.
--   • Late rate over ALL orders (incl. not delivered): the denominator is wrong.
--   • Forgetting is_delivered = 1: 6 canceled orders have an is_late value.
--   • COUNT(review_score) vs COUNT(*): orders without a review are in the late rate but not in
--     the review averages. That's intended; say so.
-- EXPECTED (28 rows)
--   ALL  77,408 delivered · 5,834 late · 7.54% · review 4.27 on time vs 2.20 late (−2.07 stars)
--   worst: AL 24.25% (334 delivered) · SE 18.85% · MA 18.38%
--   best:  RO 3.37% · AM 3.45% · AP 3.51% (only 57 deliveries: small samples swing)
--   smallest state: 31 deliveries; largest (SP): 31,683.


-- =============================================================================
-- Q6. Repeat customer rate                              [CTE, COUNT per customer_unique_id]
-- =============================================================================
WITH per_customer AS (
    SELECT customer_unique_id, COUNT(*) AS orders
    FROM vw_order_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
    GROUP BY customer_unique_id
)
SELECT
    COUNT(*)                                                       AS customers,
    SUM(CASE WHEN orders >= 2 THEN 1 ELSE 0 END)                   AS repeat_customers,
    ROUND(100.0 * SUM(CASE WHEN orders >= 2 THEN 1 ELSE 0 END) / COUNT(*), 2) AS repeat_rate_pct
FROM per_customer;
-- HOW IT WORKS
--   Two levels: first one row per person with their order count (CTE), then count the people.
--   (Alternative: SELECT COUNT(*) FROM (… GROUP BY customer_unique_id HAVING COUNT(*) >= 2).)
-- COMMON MISTAKES
--   • customer_id instead of customer_unique_id: Olist creates a new customer_id for every
--     order, so everyone has 1 order → 0.00% repeat. Replace the column and re-run to see it.
--   • COUNT(DISTINCT customer_unique_id) / COUNT(*) on orders: that's a different ratio
--     (customers per order), not "% who came back".
--   • Including canceled orders: a person who canceled and re-ordered isn't a repeat buyer.
-- EXPECTED: 76,843 customers · 2,303 repeat · 3.00%  (with customer_id: 79,404 · 0 · 0.00%)
--   Max orders by one person: 11. Only 1,540 bought on 2+ different DAYS: some "repeat"
--   orders are one checkout split into several orders (worth saying in an interview).


-- =============================================================================
-- Q7. Cohort retention                              [MIN() OVER (PARTITION BY), chained CTEs]
-- =============================================================================
WITH customer_months AS (               -- 1 row per person per month they bought in
    SELECT DISTINCT
        customer_unique_id,
        purchase_month,
        CAST(substr(purchase_month, 1, 4) AS INTEGER) * 12
          + CAST(substr(purchase_month, 6, 2) AS INTEGER) AS month_idx
    FROM vw_order_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
),
with_cohort AS (                        -- add the person's first month to every row
    SELECT
        cm.*,
        MIN(purchase_month) OVER (PARTITION BY customer_unique_id) AS cohort_month,
        MIN(month_idx)      OVER (PARTITION BY customer_unique_id) AS cohort_idx
    FROM customer_months AS cm
),
cohort_activity AS (                    -- people active per cohort per month offset
    SELECT
        cohort_month,
        month_idx - cohort_idx AS months_since_first,
        COUNT(*)               AS active_customers
    FROM with_cohort
    GROUP BY cohort_month, months_since_first
)
SELECT
    cohort_month,
    months_since_first,
    active_customers,
    MAX(CASE WHEN months_since_first = 0 THEN active_customers END)
        OVER (PARTITION BY cohort_month)                           AS cohort_customers,
    ROUND(100.0 * active_customers
          / MAX(CASE WHEN months_since_first = 0 THEN active_customers END)
                OVER (PARTITION BY cohort_month), 2)               AS retention_pct
FROM cohort_activity
ORDER BY cohort_month, months_since_first;
-- HOW IT WORKS
--   1. DISTINCT person-months: 2 orders in one month count once.
--   2. MIN() OVER (PARTITION BY person) = their first month, repeated on all their rows.
--      GROUP BY would collapse the rows and you'd need to JOIN back; the window doesn't.
--   3. months_since_first = calendar-month difference (month_idx), so the 2016 gap is handled.
--   4. Cohort size = active customers at month 0 (window over the aggregated rows).
-- COMMON MISTAKES
--   • Counting orders instead of distinct people.
--   • Subtracting month strings or using julianday/30: months have 28–31 days.
--   • Reading missing cells as 0%: the 2016-10 cohort has no data for Nov–Dec 2016 (not loaded),
--     and late cohorts haven't had time to return (right-censoring).
--   • Cohort from MIN(order date) inside the filtered data: people who first bought in an
--     excluded month (2016-09) look new in 2016-10 (left-censoring).
-- EXPECTED: 160 rows, 18 cohorts, cohort sizes add up to 76,843.
--   2017-01: 753 people → month 1: 3 (0.40%) · month 2: 2 (0.27%) · month 3: 1 (0.13%)
--   all cohorts combined: month 1 = 0.48% · month 2 = 0.35% · month 3 = 0.26%
--   Retention is tiny: Olist customers almost never come back (matches Q6's 3%).


-- =============================================================================
-- Q8. Category % of total revenue                                     [SUM() OVER ()]
-- =============================================================================
WITH category_revenue AS (
    SELECT category, SUM(price) AS revenue
    FROM vw_item_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
    GROUP BY category
)
SELECT
    category,
    ROUND(revenue, 2)                                   AS revenue,
    ROUND(100.0 * revenue / SUM(revenue) OVER (), 2)    AS pct_of_total,
    ROUND(100.0 * SUM(revenue) OVER (ORDER BY revenue DESC, category
                                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                / SUM(revenue) OVER (), 2)              AS cumulative_pct
FROM category_revenue
ORDER BY revenue DESC, category;
-- HOW IT WORKS
--   OVER () = the window is the whole result → grand total on every row, so the division
--   happens in one SELECT. Old way: revenue / (SELECT SUM(price) FROM … same WHERE …).
--   cumulative_pct = running total sorted by revenue = Pareto curve.
-- COMMON MISTAKES
--   • Integer division (revenue is REAL here, but COUNT-based shares need 100.0 * …).
--   • Dividing by a total that has a different filter than the numerator.
--   • Dropping 'unknown' and still calling it "% of total" (it wouldn't add up to 100).
-- EXPECTED: 74 categories. 1 watches_gifts 942,810.00 (8.65%) · 2 health_beauty 921,770.78
--   (8.45%) · 3 bed_bath_table 848,477.68 (7.78%). 'unknown' is #18 (1.55%).
--   80% of revenue is reached at category #17 (of 74). Shares add to 100.05 (rounding).


-- =============================================================================
-- Q9. Top 10% of sellers                                                 [NTILE(10)]
-- =============================================================================
WITH seller_revenue AS (
    SELECT seller_id, SUM(price) AS revenue
    FROM vw_item_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
    GROUP BY seller_id
),
bucketed AS (
    SELECT
        seller_id,
        revenue,
        NTILE(10) OVER (ORDER BY revenue DESC, seller_id)                   AS revenue_decile,
        100.0 * revenue / SUM(revenue) OVER ()                              AS pct_of_total,
        100.0 * SUM(revenue) OVER (ORDER BY revenue DESC, seller_id
                                   ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
              / SUM(revenue) OVER ()                                        AS cumulative_pct
    FROM seller_revenue
)
SELECT seller_id, ROUND(revenue, 2) AS revenue,
       ROUND(pct_of_total, 3) AS pct_of_total, ROUND(cumulative_pct, 2) AS cumulative_pct
FROM bucketed
WHERE revenue_decile = 1
ORDER BY revenue DESC, seller_id;
-- HOW IT WORKS
--   NTILE(10) sorts the rows and deals them into 10 equal-sized buckets: 2,526 sellers →
--   buckets 1–6 get 253, buckets 7–10 get 252 (extras go to the first buckets).
--   All window columns are computed in the CTE, BEFORE WHERE keeps decile 1.
-- COMMON MISTAKES
--   • Computing SUM() OVER () in the outer query after WHERE: the "total" is then only the top
--     10%'s revenue, every share looks 1.5× too big (÷ 0.6676), and the last cumulative_pct
--     shows 100% instead of 66.76%.
--   • NTILE is row-based: tied sellers can be split across buckets. Add a tie-breaker
--     (seller_id) so the result doesn't change between runs, or use PERCENT_RANK() <= 0.1 if
--     ties must stay together.
--   • "Top 10% of revenue" ≠ "top 10% of sellers": read the question.
-- EXPECTED: 253 sellers (of 2,526). #1 earns 196,508.85 (1.80%). The top 10% together earn
--   66.76% of all revenue (cumulative_pct on the last row). Decile 10: 252 sellers, 0.1%.


-- =============================================================================
-- Q10. Each customer's second-highest order                              [ROW_NUMBER]
-- =============================================================================
WITH ranked AS (
    SELECT
        customer_unique_id,
        order_id,
        items_price AS order_value,
        ROW_NUMBER() OVER (PARTITION BY customer_unique_id
                           ORDER BY items_price DESC, order_purchase_timestamp, order_id) AS value_rank,
        COUNT(*)         OVER (PARTITION BY customer_unique_id) AS customer_orders,
        MAX(items_price) OVER (PARTITION BY customer_unique_id) AS highest_order_value
    FROM vw_order_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
)
SELECT customer_unique_id, customer_orders,
       ROUND(highest_order_value, 2) AS highest_order_value,
       order_id AS second_order_id,
       ROUND(order_value, 2)         AS second_highest_value
FROM ranked
WHERE value_rank = 2
ORDER BY second_highest_value DESC, customer_unique_id;
-- HOW IT WORKS
--   ROW_NUMBER restarts at 1 for each person (PARTITION BY) and counts down from their biggest
--   order. Row 2 = second-highest ORDER. COUNT/MAX OVER the same partition add context
--   without a GROUP BY.
-- COMMON MISTAKES
--   • No tie-breaker after items_price: two equal orders swap between runs (non-deterministic).
--   • Expecting one-order customers to show up with NULL: they have no row 2. If the report
--     needs them, LEFT JOIN this result to the customer list.
--   • Mixing up "second-highest ORDER" (ROW_NUMBER = 2) with "second-highest distinct VALUE"
--     (DENSE_RANK = 2): see variation V2 and the note below.
-- EXPECTED: 2,303 rows = the 2,303 repeat customers from Q6. 279 (12.1%) have row 2 equal to
--   row 1 (same value twice). Top: 4,799.00 / 2,589.00; 1,649.99 / 1,349.99.
--   With DENSE_RANK = 2 instead: 2,064 rows for only 2,052 customers. 251 customers have no
--   second distinct value, and 12 get TWO rows (two orders tied at the 2nd value).


-- =============================================================================
-- CLASSIC INTERVIEW VARIATIONS
-- =============================================================================

-- -----------------------------------------------------------------------------
-- V1. ROW_NUMBER vs RANK vs DENSE_RANK with ties
-- -----------------------------------------------------------------------------
SELECT
    seller_state,
    COUNT(*)                                             AS sellers,
    ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC, seller_state) AS row_num,
    RANK()       OVER (ORDER BY COUNT(*) DESC)               AS rnk,
    DENSE_RANK() OVER (ORDER BY COUNT(*) DESC)               AS dense_rnk
FROM sellers
GROUP BY seller_state
ORDER BY sellers DESC, seller_state;
-- A window function can wrap an aggregate: COUNT(*) is computed by GROUP BY first, then ranked.
-- EXPECTED (bottom of the list; 23 states):
--   state sellers row_num rnk dense_rnk
--   MS       5      14    14     14
--   RN       5      15    14     14     ← tie: RANK and DENSE_RANK repeat, ROW_NUMBER doesn't
--   MT       4      16    16     15     ← RANK skips 15 (two states took 14); DENSE_RANK doesn't skip
--   RO       2      17    17     16
--   SE       2      18    17     16
--   AC…PI    1   19–23    19     17     ← 5 states tied with 1 seller
--   Last values: ROW_NUMBER 23 (= row count) · RANK 19 · DENSE_RANK 17 (= distinct values).
-- WHEN TO USE WHICH
--   ROW_NUMBER  exactly one row per position: de-duplication (keep rn = 1), pagination,
--               "the 2nd order". Needs a tie-breaker to be repeatable.
--   RANK        competition/leaderboard ranking (1, 2, 2, 4): "how many are ahead of me + 1".
--   DENSE_RANK  "top N distinct values" (1, 2, 2, 3): top 3 categories, Nth-highest salary.
-- Excel: RANK.EQ = RANK; no built-in DENSE_RANK (=SUMPRODUCT((B$2:B$24>B2)/COUNTIF(B$2:B$24,B$2:B$24))+1);
--   ROW_NUMBER = sort + =ROW()-1.  pandas: rank(method='first' | 'min' | 'dense').


-- -----------------------------------------------------------------------------
-- V2. The second-highest order value: three ways
-- -----------------------------------------------------------------------------
-- (a) subquery with MAX: "the biggest value that's smaller than the biggest"
SELECT MAX(items_price) AS second_highest
FROM vw_order_facts
WHERE order_status NOT IN ('canceled', 'unavailable')
  AND items_price < (SELECT MAX(items_price)
                     FROM vw_order_facts
                     WHERE order_status NOT IN ('canceled', 'unavailable'));

-- (b) DENSE_RANK = 2 (generalises to the Nth highest: change 2 to N)
SELECT DISTINCT items_price AS second_highest
FROM (
    SELECT items_price,
           DENSE_RANK() OVER (ORDER BY items_price DESC) AS value_rank
    FROM vw_order_facts
    WHERE order_status NOT IN ('canceled', 'unavailable')
)
WHERE value_rank = 2;

-- (c) LIMIT / OFFSET: sort, skip 1 row, take 1 (SQL Server: OFFSET 1 ROWS FETCH NEXT 1 ROWS ONLY)
SELECT items_price AS second_highest
FROM vw_order_facts
WHERE order_status NOT IN ('canceled', 'unavailable')
ORDER BY items_price DESC
LIMIT 1 OFFSET 1;
-- EXPECTED: all three return 6,735.00 (the highest is 13,440.00, one order, no tie).
-- The tie trap: if the top value appears twice, (c) returns the TOP value again, because it
-- skips a ROW, not a value. (a) and (b) still return the real second value. (c) is fixed by
-- SELECT DISTINCT items_price … LIMIT 1 OFFSET 1. Proof on 3 made-up values:
WITH v(x) AS (VALUES (100), (100), (90))
SELECT
    (SELECT x FROM v ORDER BY x DESC LIMIT 1 OFFSET 1)                        AS offset_way,    -- 100 ✗
    (SELECT MAX(x) FROM v WHERE x < (SELECT MAX(x) FROM v))                   AS max_way,       --  90 ✓
    (SELECT DISTINCT x FROM (SELECT x, DENSE_RANK() OVER (ORDER BY x DESC) AS r FROM v)
     WHERE r = 2)                                                             AS dense_rank_way; -- 90 ✓
-- Also: if there IS no second value (one distinct value), (a) returns NULL, (b) and (c) return
-- no rows. Interviewers like asking for NULL in that case → (a), or wrap (b) in a scalar subquery.


-- -----------------------------------------------------------------------------
-- V3. WHERE vs HAVING
-- -----------------------------------------------------------------------------
-- (a) HAVING filters GROUPS, after aggregation: "the category's average price is above R$200"
SELECT category, COUNT(*) AS items, ROUND(AVG(price), 2) AS avg_price
FROM vw_item_facts
WHERE order_status NOT IN ('canceled', 'unavailable')
GROUP BY category
HAVING COUNT(*) >= 100 AND AVG(price) > 200
ORDER BY avg_price DESC;

-- (b) WHERE filters ROWS, before aggregation: "only items that each cost above R$200"
SELECT category, COUNT(*) AS items_over_200, ROUND(AVG(price), 2) AS avg_price
FROM vw_item_facts
WHERE order_status NOT IN ('canceled', 'unavailable')
  AND price > 200
GROUP BY category
HAVING COUNT(*) >= 100
ORDER BY items_over_200 DESC;
-- EXPECTED: (a) 7 categories (computers first: 1,116.97 avg over 170 items)
--           (b) 25 categories: many cheap categories still sell 100+ items above R$200.
-- WHY DIFFERENT: (b) throws away cheap items BEFORE counting, so COUNT(*) and AVG only see
-- expensive items. (a) keeps every item and judges the group's average.
-- Rules: WHERE can't use aggregates (WHERE AVG(price) > 200 is an error: groups don't exist
-- yet). HAVING can filter on non-aggregates, but put row filters in WHERE: fewer rows to group.
-- Logical order: FROM → WHERE → GROUP BY → HAVING → window functions → SELECT → ORDER BY → LIMIT.
-- Excel: WHERE = filter the data before the PivotTable; HAVING = filter the PivotTable's values.
-- pandas: WHERE = df[df.price > 200].groupby(…); HAVING = df.groupby(…).filter(lambda g: …)
--   or .agg(…) then a boolean mask on the result.
