# Project Guide — Automated E-Commerce Reporting Pipeline (Python + SQL)

**Timeline:** 3 days, about 6–8 hours a day
**Stack:** Python (pandas, matplotlib, sqlite3, openpyxl, logging, schedule) + SQLite
**Dataset:** Olist Brazilian E-Commerce (public, ~100k real orders, 2016–2018)

---

## 0. The big picture

### The business story (the "why" of the project)
**Tell it as a scenario you designed, never as a real job.** It's the Situation in your project-level STAR.
> "A common analytics problem: an e-commerce operations team gets new order files every month and builds its
> performance report by hand in Excel: downloading, cleaning, copy-pasting, recalculating KPIs and rebuilding charts.
> It's slow, error-prone and depends on one person. I designed this scenario and built the automated version end to
> end on real data (Olist, ~100k orders, 9 tables): new files arrive → Python cleans and validates them → they load
> incrementally into SQL → SQL runs data-quality checks and KPIs → Python generates the Excel report, on a schedule."

Any "before" time (e.g. "about 3 hours by hand") is an **estimate**. Say "I estimated", not "it took".
See `checkpoint_review.md` for how to turn this into STAR answers.

### How it fits together

| # | Stage | Tool | What you build | What you learn | Behavioral question it answers |
|---|---|---|---|---|---|
| 1 | Gather data | Browser / Kaggle | Download Olist, understand the 9 tables and how they connect | Data sourcing, data models | "How do you get familiar with a new dataset?" |
| 2 | Simulate data feed | Python | Script that splits the data into monthly files that "arrive" over time | pandas basics, file handling | "Describe a process you automated" (sets up the before/after) |
| 3 | Explore / profile | Python | Profiling script that reports nulls, duplicates, types, ranges | pandas inspection functions | "What's the first thing you do with new data?" |
| 4 | Clean | Python | Cleaning module: text, dates, duplicates, nulls, derived columns, stats | Data cleaning ⭐ | "How do you handle messy or inconsistent data?" |
| 5 | Design schema | SQL | Tables, keys, indexes, pipeline log tables | Database design | "How do you structure data for analysis?" |
| 6 | Load incrementally | Python + SQL | Loader that only loads new files, never duplicates, handles a 1M-row file in chunks | Idempotency, transactions, large data ⭐ | "Tell me about working with large datasets" |
| 7 | SQL automation | SQL | Views that auto-refresh KPIs, a trigger for auditing | Views, triggers, stored procedures (concept) | "How did you use SQL to automate?" |
| 8 | Data-quality checks | SQL + Python | Automated checks that flag bad data each run | Anti-joins, validation | "How do you make sure your numbers are right?" |
| 9 | KPI queries | SQL | 10 business queries using window functions and CTEs | RANK, LAG, running totals, cohorts ⭐ | Technical SQL round |
| 10 | SQL drill | SQL | Practice file of classic interview questions | Interview patterns | Technical SQL round |
| 11 | Report | Python | Excel report with charts and a plain-English summary | matplotlib, openpyxl | "How do you explain results to non-technical people?" |
| 12 | Orchestrate | Python | One command runs everything, with logging and error handling | Automation design ⭐ | "How did you use Python to automate?" |
| 13 | Schedule | Python / OS | Daily scheduled run (Task Scheduler or cron) | Scheduling | "How would this run in production?" |
| 14 | Demo test | — | Prove that new files flow through, re-runs don't duplicate, bad files are caught | Testing | "How did you test it?" |
| 15 | Package | Docs | README, STAR stories, findings memo, resume bullets | Storytelling | Every behavioral question |

### Daily schedule

| Day | Steps | Theme |
|---|---|---|
| Day 1 | 1.1 → 1.5 | Setup, data, exploration, cleaning (Python) |
| Day 2 | 2.1 → 2.7 | Database, loading, SQL automation, KPIs, SQL drill |
| Day 3 | 3.1 → 3.6 | Report, automation, scheduling, demo, interview packaging |

### Paths on Windows
This guide writes paths with forward slashes (`data/raw/`), which is how they appear in Python code.
In File Explorer the same folder is `DA-Project\data\raw`. The code uses `pathlib`, so both work.

### How to work with Claude Code (read once)
1. Open a terminal in your `DA-Project` folder (`C:\Projects\DA-Project`) and type `claude`.
2. **The first message of every session:** "Read CLAUDE.md and PROJECT_GUIDE.md. We're on Step X.X."
3. Paste the step's prompt (in the 💬 boxes below).
4. Read every explanation. If you can't explain a line, ask: "Explain this line like I'm in an interview."
5. When a step works, say: **"Step X.X done — update interview_notes.md."**
6. Commit to Git after each step: `git add .` then `git commit -m "Step X.X: <what>"` (two separate lines: PowerShell 5.1 does not support `&&`). The commit history shows your process.

---

# DAY 1 — Data, exploration, cleaning (Python)

## Step 1.1 — Setup and gather the data (45 min · do this yourself, no Claude Code needed)

### Where the data comes from
- **Source:** Kaggle → search **"Brazilian E-Commerce Public Dataset by Olist"**
  (https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
- You need a free Kaggle account. Click **Download** to get a zip file (~45 MB).
- Optional, and a nice automation touch: use the Kaggle API instead.
  `pip install kaggle` → get an API token from Kaggle account settings →
  `kaggle datasets download -d olistbr/brazilian-ecommerce -p data/raw --unzip`

### What to do
1. Create the project folder at **`C:\Projects\DA-Project`**. Keep it outside OneDrive (sync can lock the SQLite database and log files) and use no spaces anywhere in the path (spaces break Task Scheduler commands). Use this structure:
```
DA-Project/
├── CLAUDE.md  PROJECT_GUIDE.md  interview_notes.md  README.md  requirements.txt
├── data/raw/        ← the 9 Kaggle CSVs (never modified)
├── data/incoming/   ← monthly files "arriving" (pipeline input)
├── data/holdback/   ← last 3 months, saved for the live demo
├── data/archive/    ← incoming files moved here AFTER the pipeline processes them
├── data/failed/     ← bad files the pipeline couldn't process
├── db/  sql/  src/  reports/  logs/
```
Word copies (`PROJECT_GUIDE.docx`, `interview_notes.docx`) are for reading only. Claude Code works with the `.md` files.
2. Unzip the Kaggle download. It extracts to a folder called `archive`. **Move all 9 CSVs into `data/raw/`**,
   then delete the extracted `archive` folder and the zip (re-downloadable any time). This avoids a clash
   with the pipeline's own `data/archive/` folder, which is for processed files.
   *Note: Windows Explorer can't create `data\raw` in one go. Create `data` first, then `raw` inside it.*
3. `pip install pandas matplotlib openpyxl schedule` and save them in `requirements.txt`.
4. `git init`, create a GitHub repo, make a first commit. Add `data/` and `db/` to `.gitignore`
   (never commit raw data to GitHub).
5. Fill in the **Problem statement** at the top of `interview_notes.md`.

### Understand the 9 tables

| File | Approx. rows | What it is | Type |
|---|---|---|---|
| olist_orders_dataset.csv | ~99k | One row per order: status and 5 timestamps (purchase → delivery) | Fact (center) |
| olist_order_items_dataset.csv | ~113k | One row per item in an order: product, seller, price, freight | Fact |
| olist_order_payments_dataset.csv | ~104k | Payment rows (an order can have several): type, installments, value | Fact |
| olist_order_reviews_dataset.csv | ~99k | Review score (1–5) and comments | Fact |
| olist_customers_dataset.csv | ~99k | Customer city, state, zip | Dimension |
| olist_products_dataset.csv | ~33k | Product category, size, weight | Dimension |
| olist_sellers_dataset.csv | ~3k | Seller city, state | Dimension |
| olist_geolocation_dataset.csv | ~1M | Zip code → lat/long (many duplicates) | Reference (your "large file") |
| product_category_name_translation.csv | ~71 | Portuguese → English category names | Lookup |

**How they connect:** `orders` is the center. `order_id` links to items, payments and reviews.
`customer_id` links to customers. Items link to products (`product_id`) and sellers (`seller_id`).

⚠️ **Key quirk, and a great interview point:** `customer_id` is a *new ID for every order*.
The real person is `customer_unique_id`. If you count repeat customers using `customer_id`,
you get roughly 0% repeat customers, which is wrong. Remember this one.

**Behavioral angle:** *"When I get a new dataset, I first understand the grain of each table (what one
row represents) and how the tables join, before I touch any numbers. On this project that caught that
customer_id was per-order, not per-person. Using it would have made the repeat-customer KPI wrong."*

✅ **Done when:** 9 CSVs in `data/raw/`, Git repo created, problem statement written.

---

## Step 1.2 — Simulate a monthly data feed (45 min)

**What & why:** Automation only makes sense if *new data keeps arriving*. Olist is one big download,
so you split it into monthly files, the way a company would get a new export every month.
You also add a small, documented amount of realistic mess, and save the last 3 months for the live demo.

**What the script does:**
1. Reads orders and finds each order's purchase month.
2. For each month, writes `orders_YYYY_MM.csv`, `order_items_YYYY_MM.csv`,
   `payments_YYYY_MM.csv`, `reviews_YYYY_MM.csv` into `data/incoming/`.
3. Puts the last 3 months into `data/holdback/` instead.
4. Injects mess into the batches: ~1% duplicate rows, random casing and spaces in `order_status`
   ("Delivered ", "DELIVERED"), a few malformed dates, some blank strings instead of nulls.
5. Writes `data/mess_log.csv` recording exactly what was injected, so you can later prove
   your cleaning caught it.

**Key functions:** `pd.read_csv(parse_dates=[...])`, `.dt.to_period('M')`, `groupby` loop,
`isin`, `sample(frac=, random_state=)`, `pd.concat`, `to_csv(index=False)`, `pathlib.Path.mkdir(parents=True, exist_ok=True)`.

**Be honest in interviews:** *"The dataset is public. I simulated a monthly feed and injected
documented data issues to mimic a production environment, so I could test that the pipeline handles them."*
Interviewers respect this. Never claim it was a real company's data.

💬 **Prompt:**
> Read CLAUDE.md and PROJECT_GUIDE.md. We're on Step 1.2. Write `src/make_batches.py` that splits
> orders, order_items, payments and reviews by order purchase month into `data/incoming/` as
> `<table>_YYYY_MM.csv`. Put the last 3 months in `data/holdback/`. Inject a small, documented amount of
> mess into the batches (≈1% duplicate rows, inconsistent casing/whitespace in order_status, a few malformed
> date strings, blank strings instead of nulls) using a fixed random seed, and log every injection to
> `data/mess_log.csv`. Explain each pandas function briefly as you use it.

✅ **Done when:** monthly files exist in `incoming/`, 3 months in `holdback/`, `mess_log.csv` exists.

---

## Step 1.3 — Explore / profile the data (1 hr)

> 📘 **Read first:** `analyst_playbook.md`, Phase 1. It has the same 10 profiling checks done in Excel, Python and SQL,
> plus interview answers. Try 2–3 of the Excel checks on `olist_orders_dataset.csv` before running this step.

**What & why:** Before cleaning, measure the problems. Profiling turns "the data is messy" into
"3.2% of delivery dates are missing and 610 products have no category". Specific numbers make
interview answers credible.

**What to check for every table:**

| Check | Function | Why |
|---|---|---|
| Size | `df.shape` | Know the volume |
| Column types | `df.dtypes`, `df.info()` | Dates read as text? IDs read as numbers? |
| Missing values | `df.isna().sum()`, `/len(df)*100` | Count and % per column |
| Duplicate rows | `df.duplicated().sum()` | Exact copies |
| Duplicate keys | `df.duplicated(subset=['order_id']).sum()` | Is the "unique" ID unique? |
| Category values | `df['col'].value_counts(dropna=False)` | Spot "Delivered" vs "delivered " |
| Number ranges | `df.describe()` | Negative prices? Zero installments? Outliers? |
| Date ranges | `.min()`, `.max()` | Impossible dates? |
| Unique counts | `df.nunique()` | Grain check |

**Real Olist quirks you'll find (verify the exact numbers yourself):**
- Missing delivery dates on some orders (often canceled or undelivered, so that's expected, not an error)
- A few hundred products with no category
- Misspelled columns: `product_name_lenght`, `product_description_lenght`
- Duplicate `review_id`s
- Payment rows with `payment_installments = 0` and a `not_defined` payment type
- A couple of categories missing from the translation file
- Geolocation: ~1M rows with heavy duplication and inconsistent city spellings

💬 **Prompt:**
> Step 1.3. Before writing code, ask me what data issues I expect to find. Then write `src/explore.py`
> that profiles every table in data/raw and one sample month from data/incoming: shape, dtypes, null
> count and % per column, duplicate rows, duplicate keys, value_counts for categorical columns, describe()
> for numeric columns, date min/max. Save the output to `reports/data_profile.txt`. Then summarize the top
> 10 data issues and, for each, whether it's a real error or expected behavior (e.g. canceled orders
> having no delivery date).

✅ **Done when:** `data_profile.txt` exists and you can name the top 5 issues from memory.

---

## Step 1.4 — Clean the data (2.5 hrs) ⭐ the biggest interview topic

**What & why:** Turn raw, inconsistent files into analysis-ready data, where **every decision is
deliberate and recorded**. In interviews, the *reasoning* matters more than the code.

### The cleaning checklist (in this order)

**A. Standardize text**
- What: strip spaces, make case consistent, turn empty strings into real nulls.
- How: `df['order_status'].str.strip().str.lower()`, `df.replace('', np.nan)`
- Why: "Delivered " and "delivered" would otherwise count as two different statuses.

**B. Fix data types**
- What: dates to datetime, numbers to numeric, IDs stay as strings (zip codes lose leading zeros if read as numbers!).
- How: `pd.to_datetime(col, errors='coerce')`, `pd.to_numeric(col, errors='coerce')`, `read_csv(dtype={'zip': str})`
- Why: you can't calculate delivery days on text. `errors='coerce'` turns bad values into NaT
  instead of crashing. **Count how many became NaT.** That's your "malformed dates fixed" metric.

**C. Remove duplicates**
- What: exact duplicate rows first, then duplicate *keys* (the same order_id twice).
- How: `df.drop_duplicates()`, `df.drop_duplicates(subset=['order_id'], keep='last')`
- Why & decision: for key duplicates, decide which copy to keep. `keep='last'` keeps the most
  recent version. For reviews, keep the latest review per order. **Write down why.**

**D. Handle missing values. Decide per column, never blanket-drop.**

| Column | Decision | Reason |
|---|---|---|
| `order_delivered_customer_date` | Keep as null + add `is_delivered` flag | Missing because not delivered, which is real information |
| `order_approved_at` | Keep null | Few rows; dropping loses orders |
| `product_category_name` | Fill with `'unknown'` | Keep the revenue; don't lose sales from totals |
| `review_comment_message` | Leave null | Optional field; most customers don't write one |
| Product dimensions | Leave null or fill with category median | Not needed for KPIs; mention median fill as an option |
| `payment_installments = 0` | Set to 1 | Logically at least one payment |

**Interview principle:** *"Drop when the row is unusable. Fill when there's a sensible default.
Flag when the missingness itself means something."*

**E. Fix column names**
- What: rename `product_name_lenght` to `product_name_length`, use snake_case everywhere.
- How: `df.rename(columns={...})`

**F. Enrich / translate**
- What: merge the English category names, and find categories with no translation.
- How: `products.merge(translation, on='product_category_name', how='left', validate='many_to_one')`
- Why: `how='left'` keeps all products. `validate=` raises an error if the join would duplicate rows,
  which protects you from **join fan-out** (an interview favorite).

**G. Validate logic / outliers**
- What: delivery before purchase? Price ≤ 0? Extreme freight?
- How: boolean filters `df[df.delivered < df.purchased]`, `describe()`, IQR rule for outliers.
- Decision: **flag** suspicious rows instead of deleting them, and let the business decide.

**H. Add derived columns (feature engineering)**
- `delivery_days = (delivered - purchased).dt.days`
- `is_late = delivered > estimated` → `np.where(...)`
- `purchase_month = purchase_ts.dt.to_period('M').astype(str)`
- `total_item_value = price + freight_value`

**I. Record cleaning stats.** Every function returns `(clean_df, stats)`, where `stats` = rows in,
rows out, duplicates removed, nulls filled, dates coerced, rows flagged. These go into your logs,
the report's Data Quality sheet and your **Running metrics** table.

**J. Prove it worked:** compare against `mess_log.csv`. Did cleaning catch every injected issue?

**Behavioral angle:** *"How do you handle inconsistent data?"* Walk through A→J with real numbers.
*"Tell me about a judgment call."* The missing delivery dates: you chose to flag instead of drop,
because the missing value means "not delivered", which is information.

💬 **Prompt (updated after Step 1.3: includes the profiling findings and the cleaning-order rule):**
> Read CLAUDE.md, PROJECT_GUIDE.md (Step 1.4, cleaning checklist A–J), section 7 of checkpoint_review.md, and the top 10
> issues in reports/data_profile.txt. We're on Step 1.4.
>
> Build `src/clean.py` with one function per table (`clean_orders`, `clean_items`, `clean_payments`, `clean_reviews`,
> `clean_products`, `clean_customers`, `clean_sellers`), each returning `(clean_df, stats_dict)`. It must work on both the
> raw reference tables and any monthly file in data/incoming or data/holdback. Never modify files in data/.
>
> Work through checklist A–J one section at a time. After each section, give me a 2–3 line summary: the decision, the
> alternative, and why, plus the Excel equivalent. Only stop and ask me when a decision is genuinely debatable.
>
> Requirements:
> 1. Cleaning order: standardize text and convert blanks (including single-space strings) to nulls FIRST, then drop exact
>    duplicates, then drop key-based duplicates. Report both counts separately in stats.
> 2. Dates: parse with errors='coerce' and count how many values became null because they were malformed.
> 3. Handle missing values per column (drop vs fill vs flag), with the reason in a code comment. Keep undelivered orders and
>    add an `is_delivered` flag.
> 4. FLAG (don't delete) the real-data logic issues profiling found: carrier pickup before purchase, delivery before pickup,
>    canceled orders with a delivery date, delivered orders with no delivery date, shipping deadlines far in the future, and
>    orders with no payment. Use boolean `flag_` columns and count them in stats.
> 5. Fix the `lenght` column names, read zip codes as text, merge English category names (with `validate=`) and fill
>    untranslated or missing categories deliberately.
> 6. Add derived columns: `delivery_days`, `is_late`, `purchase_month`, `total_item_value`.
>
> Then write `tests/test_clean.py` (plain Python, no pytest needed) that runs clean.py on one incoming month and checks every
> injected issue in data/mess_log.csv for that month was caught. Run it on 2017_11 and one holdback month and show me the
> results as a table: injected vs caught, per mess type. Finally, update interview_notes.md, project_journal.md and the
> Running metrics.

✅ **Done when:** clean.py runs on any monthly file, returns stats, and catches every injected issue.

## Step 1.5 — End of Day 1 (20 min)
💬 **Prompt:** "End of day. Quiz me with 5 questions on today's pandas and cleaning decisions, then update the Daily recap in interview_notes.md."
Commit to Git.

---

# DAY 2 — SQL: database, loading, automation, KPIs

## Step 2.1 — Design the schema (1 hr)

**What & why:** Decide how the data is stored. Good design makes queries fast and correct.

**What to build in `sql/schema.sql`:**
- **Dimension tables:** `customers`, `products`, `sellers`, `geolocation`. These describe *who/what*.
- **Fact tables:** `orders`, `order_items`, `payments`, `reviews`. These record *events*.
- **Primary keys** (unique row ID) and **foreign keys** (links between tables).
  Composite key for order_items: `(order_id, order_item_id)`.
- **Indexes** on columns used in joins and filters: `order_items(product_id)`, `orders(customer_id)`, `orders(purchase_month)`.
- **Pipeline tables:**
  - `load_log(file_name PK, table_name, rows_loaded, loaded_at, status)` records what was loaded
  - `dq_results(run_id, check_name, failed_rows, run_at)` records data-quality history
  - `order_status_audit(order_id, old_status, new_status, changed_at)` for the trigger

**Concepts to be able to explain:** fact vs dimension (star schema), primary vs foreign key,
normalization (no repeated data), why indexes speed up reads but slow down writes.

💬 **Prompt:**
> Step 2.1. I'll propose the tables first — ask me. Then help me write `sql/schema.sql` for SQLite with
> dimension and fact tables, primary/foreign keys, indexes on join/filter columns, and the pipeline tables
> `load_log`, `dq_results`, `order_status_audit`. Explain star schema, PK vs FK, normalization and index
> trade-offs in interview-friendly terms. Write `src/db.py` with a `get_connection()` helper that creates the DB
> from schema.sql if it doesn't exist and turns on foreign keys.

✅ **Done when:** `db/olist.db` is created with every table, and you can draw the schema on paper.

---

## Step 2.2 — Incremental, idempotent loading + a large file (1.5 hrs) ⭐

**What & why:** This is the heart of the automation. The loader must:
1. **Load only new files.** Check `load_log`, skip files already loaded.
2. **Be idempotent.** Running it twice gives the same result, with no duplicates (`INSERT OR IGNORE`, upsert).
3. **Be all-or-nothing.** Each file loads inside a **transaction**. If something fails halfway, it
   **rolls back** so there are no half-loaded files.
4. **Archive** successfully loaded files to `data/archive/`.
5. **Handle a large file:** load the ~1M-row geolocation file **in chunks**
   (`read_csv(chunksize=100_000)`) and reduce it to one row per zip prefix (1M rows down to ~19k).
   This is your "large dataset" story.

**Key functions/concepts:** `sqlite3.connect`, `conn.execute`, `executemany`, `df.to_sql(if_exists='append')`,
`with conn:` (auto-commit/rollback), parameterized queries (`?` placeholders, which prevent SQL injection),
`shutil.move`, `pathlib.glob('orders_*.csv')`, chunked reading, timing with `time.perf_counter()`.

**Behavioral angle:** *"What happens if the pipeline runs twice?"* → idempotency.
*"What if it crashes halfway?"* → transactions and rollback.
*"Large data?"* → chunked loading of 1M rows, with timings and memory use.

💬 **Prompt:**
> Step 2.2. Write `src/load.py`: (1) load reference tables (customers, products, sellers, translation) once;
> (2) load geolocation in chunks of 100k rows, deduplicating to one row per zip prefix, and log rows in/out
> and time taken; (3) for each file in data/incoming, skip it if load_log shows it was already loaded,
> otherwise clean it with clean.py, insert it inside a transaction using INSERT OR IGNORE, record it in
> load_log, and move it to data/archive. Explain idempotency, transactions/rollback, to_sql vs executemany,
> chunking, and parameterized queries. Then let's test: run it twice and prove row counts don't change.

✅ **Done when:** all incoming files load, a second run loads 0 new rows, and geolocation loads in chunks.

---

## Step 2.3 — SQL-side automation: views + trigger (1 hr)

**What & why:** Move business logic *into the database* so it updates itself.
- **Views** (`sql/views.sql`): saved queries such as `vw_monthly_revenue` and `vw_delivery_performance`.
  When new data loads, views automatically reflect it. No recalculating, no copy-paste.
  This replaces the manual Excel step.
- **Trigger:** when an order's status changes, automatically log old and new status into
  `order_status_audit`. That's an audit trail with zero code in Python.
- **Concept only (SQLite lacks them):** stored procedures and scheduled jobs
  (SQL Server Agent, MySQL Event Scheduler, PostgreSQL pg_cron). Know what they are and when you'd use them.

**Interview comparisons:** view vs table vs materialized view, trigger vs application logic,
stored procedure vs Python script.

💬 **Prompt:**
> Step 2.3. Help me create (a) `sql/views.sql` with base views the report will use — start with
> `vw_order_facts` that joins orders, items, payments, customers and products into one clean analytical view
> (watch for join fan-out from multiple payments per order); (b) a trigger that writes to order_status_audit
> whenever orders.order_status is updated, and a test that proves it fires. Explain views vs tables vs
> materialized views, and what stored procedures / scheduled jobs would do in SQL Server, MySQL or PostgreSQL.

✅ **Done when:** views return data, and updating an order status creates an audit row.

---

## Step 2.4 — Automated data-quality checks (45 min)

**What & why:** Every run, SQL checks the data and records failures, so bad data gets caught
*before* it reaches the report.

| Check | SQL pattern |
|---|---|
| Orders with no matching customer | Anti-join: `LEFT JOIN ... WHERE c.customer_id IS NULL` |
| Items with price ≤ 0 | `WHERE price <= 0` |
| Delivered orders with no delivery date | `WHERE status='delivered' AND delivered_date IS NULL` |
| Delivery before purchase | Date comparison |
| Duplicate primary keys | `GROUP BY ... HAVING COUNT(*) > 1` |
| Payment total ≠ item total | CTEs aggregating both sides, then compare |

`src/checks.py` runs each check, saves results to `dq_results` and logs a WARNING if any fail.

**Behavioral angle:** *"How do you make sure your numbers are right?"* → automated checks every
run, results stored over time, failures surfaced in the report.

💬 **Prompt:**
> Step 2.4. I'll write each check in `sql/checks.sql` first — give me the list from the guide one at a time
> and review my SQL. Then write `src/checks.py` that runs every check in checks.sql, saves results to
> dq_results with a run_id, and logs a WARNING for any failing check.

✅ **Done when:** checks run, results are stored, and you wrote most of the SQL yourself.

---

## Step 2.5 — KPI queries (2 hrs) ⭐ YOU write these first

The rule: you attempt, then Claude Code reviews. Save the final versions as views.

| # | Business question | SQL skill |
|---|---|---|
| 1 | Monthly revenue and month-over-month growth % | `LAG()` |
| 2 | Running total of revenue | `SUM() OVER (ORDER BY ...)` |
| 3 | 3-month moving average of order count | Frame: `ROWS BETWEEN 2 PRECEDING AND CURRENT ROW` |
| 4 | Top 3 product categories per state | CTE + `DENSE_RANK() OVER (PARTITION BY ...)` |
| 5 | Late-delivery rate by state; avg review score late vs on-time | `CASE`, `AVG`, `GROUP BY` |
| 6 | Repeat customer rate | `customer_unique_id`, CTE, `COUNT(DISTINCT)` |
| 7 | Cohort retention by first-purchase month | `MIN() OVER`, chained CTEs |
| 8 | Each category's % of total revenue | `SUM() OVER ()` |
| 9 | Top 10% of sellers by revenue | `NTILE(10)` |
| 10 | Each customer's second-highest order | `ROW_NUMBER()` |

**For each, check:** canceled orders excluded? NULLs handled? Ties handled (RANK vs DENSE_RANK)?
Join fan-out?

💬 **Prompt (repeat for each KPI):**
> Step 2.5, KPI #1. Give me the business question, the tables/columns I need, and the expected output
> shape. Don't write the query. I'll write it, then you review for correctness, edge cases (NULLs, ties,
> canceled orders, fan-out) and style, and show me an alternative approach if one exists.

✅ **Done when:** all 10 KPIs are saved as views (`vw_kpi_...`) and you wrote the first draft of each.

---

## Step 2.6 — SQL interview drill (45 min)

> **Changed (Nov 25):** done as a self-paced practice pack instead of a live drill (it's interview prep, not pipeline work):
> `sql/practice_interview_drill.sql` (questions) + `sql/practice_interview_drill_solutions.sql` (tested solutions). See Checkpoint 5, section 9.

💬 **Prompt:**
> Step 2.6. Quiz me with 8 classic SQL interview questions using the Olist tables, one at a time: find
> duplicates, delete duplicates keeping one, RANK vs DENSE_RANK vs ROW_NUMBER with ties, customers who
> ordered in 3+ consecutive months, WHERE vs HAVING, UNION vs UNION ALL, LEFT JOIN with the filter in ON vs
> WHERE, correlated subquery vs window function. Grade each answer and save my final answers in `sql/practice.sql`.

## Step 2.7 — End of Day 2
💬 "End of day. Quiz me with 5 questions on today's SQL work and update the Daily recap." Commit.

---

# DAY 3 — Automation, reporting, scheduling, interview packaging

## Step 3.1 — Report generator (1.5 hrs)

**What & why:** This is the output the stakeholder sees. It replaces the manual Excel report.

**What `src/report.py` builds** → `reports/ops_report_YYYY-MM-DD.xlsx`:
1. **Summary sheet:** 4–5 headline numbers in plain English
   ("Revenue grew 8% vs last month. 7% of orders arrived late, mostly in the North-East.")
2. **One sheet per KPI**, read with `pd.read_sql("SELECT * FROM vw_kpi_...", conn)`.
3. **4 charts** (matplotlib, saved as PNG and embedded): revenue trend with MoM, top categories,
   late-delivery rate by state, review score late vs on-time.
4. **Data Quality sheet:** this run's cleaning stats and DQ check results.

**Key functions:** `plt.subplots`, `ax.plot / bar / barh`, `ax.set_title`, `plt.tight_layout`,
`fig.savefig(dpi=150)`, `plt.close(fig)` (prevents memory leaks in automated runs),
`pd.ExcelWriter(engine='openpyxl')`, `openpyxl.drawing.image.Image`, column widths, number formats.

**Behavioral angle:** *"Explaining to non-technical stakeholders."* The Summary sheet uses plain
sentences, not tables. Each chart answers one question, with a title that states the takeaway.

💬 **Prompt:**
> Step 3.1. Write `src/report.py` that reads the KPI views with pd.read_sql, makes 4 clean matplotlib charts
> (revenue trend with MoM, top categories, late-delivery rate by state, review score late vs on-time) with
> titles that state the takeaway, and writes an Excel report with openpyxl: a Summary sheet with plain-English
> headline sentences generated from the data, one sheet per KPI, embedded charts, and a Data Quality sheet.
> The filename includes the run date. Explain chart choices for a non-technical audience.

✅ **Done when:** running report.py creates a readable Excel file a manager could understand.

---

## Step 3.2 — The orchestrator: one command (1.5 hrs) ⭐

**What & why:** `python src/run_pipeline.py` runs everything in order, unattended.

**What it does:**
1. Starts a run, with a `run_id` and timestamp.
2. Finds new files in `data/incoming/`.
3. Cleans → loads → runs DQ checks → builds the report.
4. **Logs** every step (rows, timing) to `logs/pipeline_YYYYMMDD.log`.
5. **Error handling:** a bad file is logged and skipped (moved to `data/failed/`), and the rest continues.
6. **Exit code:** `sys.exit(1)` on failure, so a scheduler knows the run failed.
7. **Command-line flags (argparse):** `--dry-run` (show what would run), `--skip-report`, `--full-refresh`.
8. Prints a **run summary:** files processed, rows loaded, checks failed, total time.

**Behavioral angle:** *"How did you use Python for automation?"* This step is the answer.
*"How do you handle failures?"* → logging, skip-and-continue, exit codes, failed folder.

💬 **Prompt:**
> Step 3.2. Write `src/run_pipeline.py` that orchestrates: detect new files → clean → load → DQ checks →
> report. Include: a run_id, logging to logs/pipeline_YYYYMMDD.log with row counts and timing per step,
> try/except so a bad file is logged, moved to data/failed/, and skipped without crashing, sys.exit(1) on
> failure, argparse flags (--dry-run, --skip-report, --full-refresh), and a final run summary. Explain each
> design choice and what I'd add in production (alerts, retries).

✅ **Done when:** one command runs the whole pipeline end to end, with a clean log.

---

## Step 3.3 — Scheduling (45 min)

**Two ways (know both):**
1. **`schedule` library** (`src/scheduler.py`): a Python loop, `schedule.every().day.at("07:00").do(run)`.
   Simple, but the script must stay running.
2. **OS scheduler (use this one):** **Windows Task Scheduler** (you're on Windows). The OS runs it even
   after a restart. (Mac/Linux equivalent: cron, `0 7 * * * python run_pipeline.py`. Worth knowing for interviews.)

**Production concepts to mention:** Airflow, Prefect, cloud schedulers. They add dependency
management, retries, monitoring and alerts.

💬 **Prompt:**
> Step 3.3. Show me (a) `src/scheduler.py` using the schedule library, and (b) walk me step by step through
> scheduling run_pipeline.py daily at 7 AM with Windows Task Scheduler (including the exact python.exe path
> and "Start in" folder). Explain when to use each, how cron does the same on Mac/Linux, and what Airflow adds in a real company.

✅ **Done when:** the scheduled task is set up and you've seen it run once.

---

## Step 3.4 — Live demo test (30 min) — this is your interview story

1. Move one month from `data/holdback/` to `data/incoming/` → run → open the new report. It updated automatically.
2. Run again → **0 new rows** (proof of idempotency).
3. Drop in a deliberately broken file → it's logged, moved to `failed/` and the run continues.
4. Time the full run. Compare it with an estimate of the manual process (e.g. ~3 hours → ~40 seconds).

💬 **Prompt:**
> Step 3.4. Help me run the live demo test: move one holdback month in, run the pipeline, confirm the report
> updated; re-run and prove idempotency; create a deliberately broken file and confirm it's handled. Record all
> results and timings in interview_notes.md under Live demo results and the Running metrics table.

---

## Step 3.5 — Package for interviews (2 hrs)

> Use `docs/schema_diagram.png` as the data-model image in README.md.

💬 **Prompt:**
> Step 3.5. (1) Write README.md: business problem, architecture diagram (text), how to run, sample output,
> data-quality approach, what I'd do next. (2) In interview_notes.md, turn the Decisions and Problems notes
> into 6 STAR stories: automation, messy data, large data, a technical challenge, a mistake I fixed, explaining
> to a non-technical stakeholder. Use real numbers from the Running metrics table. (3) Write a 30-second project
> pitch. (4) Write a 1-page plain-English findings memo for an operations manager in `reports/findings_memo.md`:
> 3 insights + 1 recommendation. (5) Write 2 resume bullets with numbers. (6) Convert interview_notes.md to
> interview_notes.docx with pandoc.

## Step 3.6 — Mock interview (45 min)

💬 **Prompt:**
> Act as a data analyst interviewer. Ask me 10 questions about this project, one at a time — a mix of
> technical (pandas, SQL window functions, design choices, automation) and behavioral. Wait for my answer,
> grade it out of 5, then show a stronger version.

---

## If you fall behind (cut in this order)
1. Trigger (2.3b) → 2. KPIs #7, #9, #10 → 3. argparse flags → 4. scheduler.py (keep Task Scheduler/cron) → 5. geolocation chunking.

**Never cut:** cleaning (1.4), incremental load (2.2), window-function KPIs (2.5), orchestrator (3.2), packaging (3.5).
