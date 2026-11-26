-- =====================================================================================
-- Step 2.6: SQL interview drill SOLUTIONS (all tested read-only on db/olist.db, Nov 25, 2026)
-- Scope: 18 loaded months (80,445 orders). Each solution: query → expected result → how it works →
-- common mistake → the one-line interview answer.
-- Run one query at a time in DB Browser: click inside it, Ctrl+Enter.
-- =====================================================================================


-- =====================================================================================
-- Q1. FIND DUPLICATES
-- =====================================================================================
-- (a) Top 5 most-repeated people
SELECT customer_unique_id,
       COUNT(*) AS times_seen
FROM customers
GROUP BY customer_unique_id
HAVING COUNT(*) > 1
ORDER BY times_seen DESC, customer_unique_id
LIMIT 5;
-- EXPECTED: 8d50f5ea… 17 · 3e43e610… 9 · then three people with 7

-- (b) How many people are duplicated, and how many extra rows
SELECT COUNT(*)              AS duplicated_people,
       SUM(times_seen)       AS rows_involved,
       SUM(times_seen - 1)   AS extra_rows
FROM (
    SELECT customer_unique_id, COUNT(*) AS times_seen
    FROM customers
    GROUP BY customer_unique_id
    HAVING COUNT(*) > 1
);
-- EXPECTED: 2,997 people · 6,342 rows · 3,345 extra rows   (99,441 rows − 96,096 people = 3,345 ✔)
--
-- HOW: GROUP BY the column that SHOULD be unique; HAVING COUNT(*) > 1 keeps only the repeats.
-- MISTAKE: WHERE COUNT(*) > 1 → error. WHERE runs before grouping, so aggregates aren't available yet.
-- NOTE: here these "duplicates" are by design (customer_id is per order), which is why repeat-customer KPIs use
--       customer_unique_id. Always ask "duplicate of WHAT?": exact rows, or a business key.
-- INTERVIEW LINE: "GROUP BY the key, HAVING COUNT(*) > 1. For exact duplicate rows, GROUP BY every column."


-- =====================================================================================
-- Q2. DELETE DUPLICATES, KEEPING ONE
-- =====================================================================================
-- Step 1: PREVIEW (read-only safe). Number each person's rows; rn = 1 is the one we keep.
WITH ranked AS (
    SELECT customer_id,
           customer_unique_id,
           ROW_NUMBER() OVER (PARTITION BY customer_unique_id
                              ORDER BY customer_id) AS rn
    FROM customers
)
SELECT COUNT(*)                                 AS rows_that_would_be_deleted,
       (SELECT COUNT(*) FROM customers) - COUNT(*) AS rows_kept
FROM ranked
WHERE rn > 1;
-- EXPECTED: 3,345 would be deleted · 96,096 kept (= one per real person)

-- Step 2: the DELETE you'd run on a writable copy (DON'T run it on the pipeline DB; it's read-only anyway):
--
--   DELETE FROM customers
--   WHERE customer_id IN (
--       SELECT customer_id
--       FROM (SELECT customer_id,
--                    ROW_NUMBER() OVER (PARTITION BY customer_unique_id ORDER BY customer_id) AS rn
--             FROM customers)
--       WHERE rn > 1
--   );
--
-- Classic alternative (works in most databases, no window function):
--   DELETE FROM customers
--   WHERE customer_id NOT IN (SELECT MIN(customer_id) FROM customers GROUP BY customer_unique_id);
--
-- HOW: ROW_NUMBER restarts at 1 for each key (PARTITION BY); ORDER BY decides WHICH copy is #1 (the keeper).
-- MISTAKE: deleting without previewing, or without a deterministic ORDER BY (a random copy survives).
--          In our cleaning we kept the most COMPLETE copy, not just the first (Q23 story).
-- SAFETY HABIT (say it in interviews): "I'd run the SELECT version first, check the count, and wrap the DELETE in a
--          transaction (BEGIN … check … COMMIT or ROLLBACK)."
-- INTERVIEW LINE: "ROW_NUMBER over the key, delete where rn > 1, previewed first and inside a transaction."


-- =====================================================================================
-- Q3. RANK vs DENSE_RANK vs ROW_NUMBER (with ties)
-- =====================================================================================
-- Tiny demo first: the clearest way to explain it
WITH v(name, score) AS (VALUES ('A',10), ('B',10), ('C',8), ('D',7))
SELECT name, score,
       ROW_NUMBER() OVER (ORDER BY score DESC, name) AS row_number,
       RANK()       OVER (ORDER BY score DESC)       AS rank,
       DENSE_RANK() OVER (ORDER BY score DESC)       AS dense_rank
FROM v;
-- EXPECTED: A 10 → 1,1,1 · B 10 → 2,1,1 · C 8 → 3,3,2 · D 7 → 4,4,3

-- On your data: sellers per state
WITH s AS (
    SELECT seller_state, COUNT(*) AS sellers
    FROM sellers
    GROUP BY seller_state
)
SELECT seller_state, sellers,
       ROW_NUMBER() OVER (ORDER BY sellers DESC, seller_state) AS row_num,
       RANK()       OVER (ORDER BY sellers DESC)               AS rnk,
       DENSE_RANK() OVER (ORDER BY sellers DESC)               AS dense_rnk
FROM s
ORDER BY sellers DESC, seller_state;
-- EXPECTED (where they differ, at the bottom):
--   MS 5 → 14,14,14 · RN 5 → 15,14,14   (tie: RANK and DENSE_RANK both 14)
--   MT 4 → 16,16,15                     (RANK skips to 16; DENSE_RANK continues at 15)
--   RO 2 / SE 2 → 17,17,16 / 18,17,16
--   AC, AM, MA, PA, PI (1 seller each) → ROW_NUMBER 19–23 · RANK 19 · DENSE_RANK 17
--
-- HOW: ROW_NUMBER = always unique (ties broken by the extra ORDER BY column) · RANK = ties share, then a GAP
--      (1,1,3) · DENSE_RANK = ties share, NO gap (1,1,2).
-- WHEN: "top 3 per group, ties included" → DENSE_RANK · "exactly one row per group" (dedupe, latest record) →
--       ROW_NUMBER · "competition ranking, like sports" → RANK.
-- MISTAKE: ROW_NUMBER without a tie-breaker → which tied row gets #1 can change between runs.
-- INTERVIEW LINE: "For 10, 10, 8: ROW_NUMBER 1-2-3, RANK 1-1-3, DENSE_RANK 1-1-2."


-- =====================================================================================
-- Q4. CONSECUTIVE MONTHS ("gaps and islands")
-- =====================================================================================
WITH months AS (                       -- one row per person per month they ordered
    SELECT DISTINCT
           customer_unique_id,
           CAST(substr(purchase_month, 1, 4) AS INTEGER) * 12
         + CAST(substr(purchase_month, 6, 2) AS INTEGER) AS m      -- month as a number: consecutive = +1
    FROM vw_order_facts
),
islands AS (                           -- the trick: m − ROW_NUMBER is CONSTANT within a run of consecutive months
    SELECT customer_unique_id, m,
           m - ROW_NUMBER() OVER (PARTITION BY customer_unique_id ORDER BY m) AS grp
    FROM months
),
streaks AS (
    SELECT customer_unique_id, COUNT(*) AS streak
    FROM islands
    GROUP BY customer_unique_id, grp
)
SELECT COUNT(DISTINCT customer_unique_id) AS customers_3plus_consecutive,
       MAX(streak)                        AS longest_streak
FROM streaks
WHERE streak >= 3;
-- EXPECTED: 9 customers · longest streak 7 months
--   (streak lengths overall: 1 month 78,443 · 2 months 364 · 3 months 6 · 4, 5 and 7 months: 1 each)
--
-- HOW (say it like this): "Turn months into numbers. Within a run of consecutive months, the month number and the
--   row number both go up by 1, so their difference stays the same. That difference labels each run (an 'island');
--   GROUP BY it and count."  Example: months 5,6,7,9 → row numbers 1,2,3,4 → differences 4,4,4,5 → runs of 3 and 1.
-- MISTAKE: using the 'YYYY-MM' text directly (2017-12 → 2018-01 isn't "+1" as text) → convert to year*12 + month.
-- ALTERNATIVE: LAG(m) and flag where m − LAG(m) = 1. Fine for "has 2 in a row", messier for "3+ in a row".
-- INSIGHT: only 9 of ~77k customers ordered 3 months running. Consistent with the 3% repeat rate: Olist is mostly one-time buyers.


-- =====================================================================================
-- Q5. WHERE vs HAVING
-- =====================================================================================
SELECT customer_state,
       COUNT(*)                  AS delivered_orders,
       ROUND(AVG(review_score), 2) AS avg_review
FROM vw_order_facts
WHERE is_delivered = 1                               -- ROW filter: before grouping
GROUP BY customer_state
HAVING COUNT(*) >= 1000                              -- GROUP filters: after grouping (use aggregates)
   AND AVG(review_score) < 4.1
ORDER BY avg_review;
-- EXPECTED (8 states): BA 3.87 · CE 3.87 · RJ 3.90 · PE 4.01 · ES 4.05 · GO 4.07 · DF 4.08 · SC 4.09
--
-- HOW: logical order FROM → WHERE → GROUP BY → HAVING → SELECT → ORDER BY → LIMIT.
--      WHERE filters individual rows (can't see aggregates); HAVING filters groups (can use COUNT/AVG/SUM).
-- MISTAKE 1: "WHERE AVG(review_score) < 4.1" → error.
-- MISTAKE 2: putting a plain row condition in HAVING. It works, but it filters late, after grouping all rows.
-- INSIGHT: BA, CE and RJ also have the highest late-delivery rates (Checkpoint 4 Q7). Lateness and low reviews go together.
-- INTERVIEW LINE: "WHERE filters rows before grouping, HAVING filters groups after."


-- =====================================================================================
-- Q6. UNION vs UNION ALL
-- =====================================================================================
SELECT
    (SELECT COUNT(*) FROM (SELECT customer_state FROM customers
                           UNION
                           SELECT seller_state   FROM sellers))    AS union_rows,
    (SELECT COUNT(*) FROM (SELECT customer_state FROM customers
                           UNION ALL
                           SELECT seller_state   FROM sellers))    AS union_all_rows;
-- EXPECTED: UNION 27 · UNION ALL 102,536 (99,441 customers + 3,095 sellers)

-- Bonus: customer states with no sellers (EXCEPT = "in the first list but not the second")
SELECT customer_state FROM customers
EXCEPT
SELECT seller_state FROM sellers;
-- EXPECTED: 4 states: AL, AP, RR, TO (and the reverse, seller states with no customers: none)
--
-- HOW: UNION stacks AND removes duplicates (sorts/hashes = slower). UNION ALL just stacks (fast, keeps everything).
-- WHEN: UNION ALL by default (e.g. adding a TOTAL row in KPI #5, Step 2.5); UNION only when you want distinct values.
-- MISTAKE: using UNION to "be safe" and silently losing legitimate duplicate rows (e.g. two equal payments).
-- RELATED: INTERSECT (in both) · EXCEPT (in first, not in second). Columns must match in number and type.
-- INTERVIEW LINE: "UNION de-duplicates and is slower; UNION ALL keeps every row. I default to UNION ALL."


-- =====================================================================================
-- Q7. LEFT JOIN: filter in ON vs in WHERE
-- =====================================================================================
-- Filter in ON: keeps EVERY order; review columns are filled only for 5-star reviews
SELECT COUNT(*)          AS rows_returned,
       COUNT(r.order_id) AS orders_with_5_star
FROM orders AS o
LEFT JOIN reviews AS r
  ON  r.order_id = o.order_id
  AND r.review_score = 5;
-- EXPECTED: 80,445 rows (all orders) · 44,999 matched a 5-star review

-- Filter in WHERE: throws away every row where the review isn't 5 stars, INCLUDING the unmatched (NULL) rows
SELECT COUNT(*) AS rows_returned
FROM orders AS o
LEFT JOIN reviews AS r
  ON r.order_id = o.order_id
WHERE r.review_score = 5;
-- EXPECTED: 44,999 rows. The LEFT JOIN quietly became an INNER JOIN.
--
-- HOW: ON decides which right-side rows MATCH (unmatched left rows survive with NULLs). WHERE runs after the join
--      and removes rows. NULL = 5 is not TRUE, so all unmatched orders disappear.
-- WHEN: "all orders, plus their 5-star review if any" → ON · "only orders with a 5-star review" → WHERE (or INNER JOIN).
-- MISTAKE: filtering the RIGHT table in WHERE and then wondering why the LEFT JOIN lost rows. Very common in real reports.
--          (Filtering the LEFT table, e.g. o.order_status = 'delivered', belongs in WHERE: no difference there.)
-- INTERVIEW LINE: "A filter on the right table in WHERE turns a LEFT JOIN into an INNER JOIN; put it in ON to keep all left rows."


-- =====================================================================================
-- Q8. CORRELATED SUBQUERY vs WINDOW FUNCTION
-- =====================================================================================
-- (a) Correlated subquery: for EACH seller, compute their state's average
WITH seller_rev AS (
    SELECT i.seller_id, s.seller_state, SUM(i.price) AS revenue
    FROM order_items AS i
    JOIN sellers AS s ON s.seller_id = i.seller_id
    GROUP BY i.seller_id, s.seller_state
)
SELECT COUNT(*) AS sellers_above_state_avg
FROM seller_rev AS a
WHERE a.revenue > (SELECT AVG(b.revenue)
                   FROM seller_rev AS b
                   WHERE b.seller_state = a.seller_state);   -- refers to the OUTER row → "correlated"
-- EXPECTED: 536

-- (b) Window function: compute every state's average in one pass
WITH seller_rev AS (
    SELECT i.seller_id, s.seller_state, SUM(i.price) AS revenue
    FROM order_items AS i
    JOIN sellers AS s ON s.seller_id = i.seller_id
    GROUP BY i.seller_id, s.seller_state
),
with_avg AS (
    SELECT seller_id, seller_state, revenue,
           AVG(revenue) OVER (PARTITION BY seller_state) AS state_avg
    FROM seller_rev
)
SELECT COUNT(*) AS sellers_above_state_avg
FROM with_avg
WHERE revenue > state_avg;
-- EXPECTED: 536 (same answer; about 3× faster here: 0.08 s vs 0.22 s)
--
-- HOW: a correlated subquery re-runs once PER OUTER ROW (it references the outer row: a.seller_state). The window
--      computes each partition's average once and attaches it to every row.
-- WHY THE CTE IN (b): you can't filter on a window result in WHERE (windows run after WHERE), so compute it, then filter outside.
-- WHEN: window functions for "compare each row to its group" (faster, readable, and you can SHOW the average too).
--       Correlated subqueries / EXISTS for "does a matching row exist?" checks (they can stop at the first match).
-- MISTAKE: a correlated subquery over a large table without an index → very slow (N × N).
-- INTERVIEW LINE: "Same result. The correlated version runs per row; the window computes each group once, so I prefer the window."


-- =====================================================================================
-- BONUS. NOT IN vs NOT EXISTS (the NULL trap)
-- =====================================================================================
WITH a(x) AS (VALUES (1), (2), (3)),
     b(y) AS (VALUES (1), (NULL))
SELECT
    (SELECT COUNT(*) FROM a WHERE x NOT IN (SELECT y FROM b))                  AS not_in_result,
    (SELECT COUNT(*) FROM a WHERE NOT EXISTS (SELECT 1 FROM b WHERE b.y = a.x)) AS not_exists_result;
-- EXPECTED: NOT IN → 0 rows · NOT EXISTS → 2 rows (x = 2 and 3)
--
-- HOW: "2 NOT IN (1, NULL)" means 2 <> 1 AND 2 <> NULL. And 2 <> NULL is UNKNOWN, so the whole condition is never TRUE
--      → NOT IN returns nothing as soon as the list contains one NULL. NOT EXISTS only checks for a matching row, so NULLs don't matter.
-- WHERE YOU USED IT: the Step 2.4 orphan checks use NOT EXISTS / LEFT JOIN … IS NULL, not NOT IN.
-- INTERVIEW LINE: "NOT IN breaks if the subquery has a NULL; NOT EXISTS is NULL-safe, so I use it for anti-joins."
