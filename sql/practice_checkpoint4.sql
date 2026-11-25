-- Checkpoint 4: SQL practice (run in DB Browser for SQLite, database opened READ ONLY)
-- Write your query under each question. Expected answers: checkpoint_review.md -> Checkpoint 4 -> section 9.

-- Q1. Row count of each fact table (orders, order_items, payments, reviews) in ONE result. (UNION ALL)


-- Q2. How many orders have more than one item? What's the most items in a single order? (GROUP BY ... HAVING)


-- Q3. Top 5 customer states by number of orders. (JOIN customers)


-- Q4. Reproduce the fan-out bug: join orders -> order_items -> payments; COUNT(*) and SUM(total_item_value).


-- Q5. Orders with NO items (anti-join), and their top 3 order statuses. (LEFT JOIN ... IS NULL)


-- Q6. From load_log: number of files and total rows loaded per table.


-- Q7. Late-delivery % by customer state: delivered orders only, states with >= 1000 orders, worst 3. (vw_order_facts)


-- Q8. Orders where payments and items differ by more than R$1, WITHOUT fan-out. (CTEs: aggregate first, then join)


-- Q9. How many real customers (customer_unique_id) ordered more than once? Out of how many in total?


-- Q10. Top 3 product categories by revenue. (vw_item_facts)


-- Bonus: Q3 again, plus each state's % of all orders using SUM(COUNT(*)) OVER ().
