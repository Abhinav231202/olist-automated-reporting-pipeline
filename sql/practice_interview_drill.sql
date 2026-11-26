-- =====================================================================================
-- Step 2.6: SQL interview drill (8 classic questions + 1 bonus) on YOUR Olist database
-- Run in DB Browser for SQLite with the database opened READ ONLY. Every question can be answered read-only.
-- Write your query (or explanation) under each question, then check: sql/practice_interview_drill_solutions.sql
-- Tip: say your approach OUT LOUD before typing; interviewers grade the thinking as much as the query.
-- =====================================================================================

-- Q1. FIND DUPLICATES
--     In `customers`, the same person (customer_unique_id) appears on several rows.
--     (a) List the 5 people who appear most often, with how many times.
--     (b) How many people are duplicated, and how many extra rows does that create?


-- Q2. DELETE DUPLICATES, KEEPING ONE
--     Write the query that would delete duplicate customers, keeping ONE row per customer_unique_id
--     (keep the smallest customer_id). The DB is read-only, so first write a SELECT that PREVIEWS how many rows
--     would be deleted. Then write the DELETE you'd run on a real (writable) table.


-- Q3. RANK vs DENSE_RANK vs ROW_NUMBER (with ties)
--     Count sellers per seller_state and rank the states with all three functions side by side.
--     Find the rows where the three numbers differ, and explain why.


-- Q4. CONSECUTIVE MONTHS ("gaps and islands")
--     How many real customers (customer_unique_id) placed orders in 3 or more CONSECUTIVE months?
--     What's the longest streak? (Use vw_order_facts.purchase_month, 'YYYY-MM'.)


-- Q5. WHERE vs HAVING
--     Among DELIVERED orders, which customer states have at least 1,000 orders AND an average review below 4.1?
--     Explain which condition goes in WHERE, which in HAVING, and why.


-- Q6. UNION vs UNION ALL
--     Combine the list of customer states and seller states. How many rows with UNION? With UNION ALL? Why the difference?
--     Bonus: which customer states have no sellers at all? (EXCEPT)


-- Q7. LEFT JOIN: filter in ON vs in WHERE
--     Join orders to reviews with LEFT JOIN, keeping only 5-star reviews.
--     Write it twice: the filter (review_score = 5) in the ON clause, then in the WHERE clause.
--     Compare the row counts and explain.


-- Q8. CORRELATED SUBQUERY vs WINDOW FUNCTION
--     How many sellers earn more revenue (SUM of price) than the average seller in their own state?
--     Solve it twice: with a correlated subquery, and with AVG() OVER (PARTITION BY ...). Same answer? Which is better?


-- BONUS. NOT IN vs NOT EXISTS (the NULL trap)
--     Using small VALUES lists, show a case where NOT IN and NOT EXISTS give different answers, and explain why.
