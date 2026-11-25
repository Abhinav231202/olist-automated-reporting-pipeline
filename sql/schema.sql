-- =============================================================================
-- schema.sql  —  Step 2.1
-- Star schema for the Olist pipeline (SQLite). Columns match exactly what
-- src/clean.py returns (clean_reference() / clean_month()), so Step 2.2 can
-- insert the cleaned DataFrames without renaming anything.
--
-- Run by src/db.py → get_connection() the first time the database is created.
-- Every CREATE uses IF NOT EXISTS, so running it twice is harmless.
--
-- Type conventions (SQLite has only 5 storage types: INTEGER, REAL, TEXT, BLOB, NULL):
--   IDs, zip codes    TEXT     keeps leading zeros ('01046'), never used in maths
--   dates/timestamps  TEXT     ISO-8601 'YYYY-MM-DD HH:MM:SS': sorts correctly as text and
--                              works with date(), strftime(), julianday()
--   money             REAL     SQLite has no exact DECIMAL; round in the report.
--                              (Postgres / SQL Server: NUMERIC(10,2))
--   true/false flags  INTEGER  0 / 1, and NULL = unknown (pandas NA, e.g. is_late for an
--                              undelivered order). CHECK (x IN (0,1)) lets NULL through
--                              because a CHECK only fails when the result is FALSE.
--
-- STRICT tables (SQLite 3.37+): reject a value of the wrong type instead of silently
-- storing it ('abc' in an INTEGER column). Default SQLite accepts it ("type affinity").
-- STRICT makes SQLite behave like Postgres / SQL Server here.
--
-- Rule: no CHECK constraints on *business* rules that clean.py FLAGS (price > 0,
-- review score 1–5, delivery after pickup). Clean.py keeps those rows on purpose
-- ("flag, never delete"); a CHECK would make the load reject them.
-- The DQ checks in Step 2.4 report them instead.
-- =============================================================================

BEGIN;   -- all-or-nothing: a typo halfway through leaves no half-built database

-- -----------------------------------------------------------------------------
-- DIMENSIONS  (who / what / where: descriptive, change slowly, loaded once)
-- -----------------------------------------------------------------------------

-- One row per zip prefix. The raw file has ~1M rows (many GPS points per prefix);
-- Step 2.2 reduces it to 19,015 rows while loading in chunks.
CREATE TABLE IF NOT EXISTS geolocation (
    geolocation_zip_code_prefix TEXT NOT NULL PRIMARY KEY,
    geolocation_lat             REAL,
    geolocation_lng             REAL,
    geolocation_city            TEXT,
    geolocation_state           TEXT
) STRICT;

-- Grain: one row per customer_id, which Olist generates PER ORDER.
-- The real person is customer_unique_id (99,441 ids → 96,096 people).
-- No FK to geolocation: 278 customer zips have no geolocation row; an FK would
-- reject real customers. Join with LEFT JOIN instead.
CREATE TABLE IF NOT EXISTS customers (
    customer_id              TEXT NOT NULL PRIMARY KEY,
    customer_unique_id       TEXT,
    customer_zip_code_prefix TEXT,
    customer_city            TEXT,
    customer_state           TEXT
) STRICT;

-- English category name is stored here (merged in clean_products), so there is no
-- separate translation table: one less join in every category query.
CREATE TABLE IF NOT EXISTS products (
    product_id                    TEXT NOT NULL PRIMARY KEY,
    product_category_name         TEXT,       -- 'unknown' when missing (610 products)
    product_name_length           INTEGER,    -- 'lenght' typo fixed in clean.py
    product_description_length    INTEGER,
    product_photos_qty            INTEGER,
    product_weight_g              INTEGER,
    product_length_cm             INTEGER,
    product_height_cm             INTEGER,
    product_width_cm              INTEGER,
    product_category_name_english TEXT,       -- Portuguese name when untranslated (13)
    flag_missing_category         INTEGER CHECK (flag_missing_category      IN (0, 1)),
    flag_untranslated_category    INTEGER CHECK (flag_untranslated_category IN (0, 1))
) STRICT;

-- No FK to geolocation for the same reason as customers (7 seller zips missing).
CREATE TABLE IF NOT EXISTS sellers (
    seller_id              TEXT NOT NULL PRIMARY KEY,
    seller_zip_code_prefix TEXT,
    seller_city            TEXT,
    seller_state           TEXT
) STRICT;

-- -----------------------------------------------------------------------------
-- FACTS  (events: one row per order / item line / payment / review; grow every month)
-- Load order matters with foreign keys ON: dimensions → orders → items/payments/reviews.
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS orders (
    order_id                      TEXT NOT NULL PRIMARY KEY,
    customer_id                   TEXT REFERENCES customers (customer_id),
    order_status                  TEXT,
    order_purchase_timestamp      TEXT,
    order_approved_at             TEXT,
    order_delivered_carrier_date  TEXT,
    order_delivered_customer_date TEXT,       -- NULL = not delivered (real information)
    order_estimated_delivery_date TEXT,
    flag_malformed_date           INTEGER CHECK (flag_malformed_date          IN (0, 1)),
    is_delivered                  INTEGER CHECK (is_delivered                 IN (0, 1)),
    flag_unknown_status           INTEGER CHECK (flag_unknown_status          IN (0, 1)),
    flag_pickup_before_purchase   INTEGER CHECK (flag_pickup_before_purchase  IN (0, 1)),
    flag_delivered_before_pickup  INTEGER CHECK (flag_delivered_before_pickup IN (0, 1)),
    flag_canceled_with_delivery   INTEGER CHECK (flag_canceled_with_delivery  IN (0, 1)),
    flag_delivered_no_date        INTEGER CHECK (flag_delivered_no_date       IN (0, 1)),
    flag_no_payment               INTEGER CHECK (flag_no_payment              IN (0, 1)),  -- NULL = not checked
    purchase_month                TEXT,       -- 'YYYY-MM', the report's main filter
    delivery_days                 INTEGER,    -- NULL when not delivered
    is_late                       INTEGER CHECK (is_late IN (0, 1))                       -- NULL when not delivered
) STRICT;

-- Grain: one row per item line in an order. Composite PK: order_item_id restarts at 1
-- in every order, so neither column alone is unique, but the pair is.
CREATE TABLE IF NOT EXISTS order_items (
    order_id                       TEXT    NOT NULL REFERENCES orders (order_id),
    order_item_id                  INTEGER NOT NULL,
    product_id                     TEXT    REFERENCES products (product_id),
    seller_id                      TEXT    REFERENCES sellers (seller_id),
    shipping_limit_date            TEXT,
    price                          REAL,
    freight_value                  REAL,
    flag_malformed_date            INTEGER CHECK (flag_malformed_date            IN (0, 1)),
    flag_price_not_positive        INTEGER CHECK (flag_price_not_positive        IN (0, 1)),
    flag_shipping_limit_far_future INTEGER CHECK (flag_shipping_limit_far_future IN (0, 1)),
    total_item_value               REAL,      -- price + freight_value
    PRIMARY KEY (order_id, order_item_id)
) STRICT;

-- Grain: one row per payment method used in an order (voucher + card = 2 rows).
CREATE TABLE IF NOT EXISTS payments (
    order_id                      TEXT    NOT NULL REFERENCES orders (order_id),
    payment_sequential            INTEGER NOT NULL,
    payment_type                  TEXT,
    payment_installments          INTEGER,    -- 0 already fixed to 1 in clean.py
    payment_value                 REAL,
    flag_installments_was_zero    INTEGER CHECK (flag_installments_was_zero    IN (0, 1)),
    flag_payment_type_not_defined INTEGER CHECK (flag_payment_type_not_defined IN (0, 1)),
    flag_zero_payment_value       INTEGER CHECK (flag_zero_payment_value       IN (0, 1)),
    PRIMARY KEY (order_id, payment_sequential)
) STRICT;

-- Grain: ONE review per order (clean_reviews keeps the latest), so order_id is the PK.
-- review_id is NOT unique: one review can cover several orders from the same checkout,
-- so it can't be the primary key.
CREATE TABLE IF NOT EXISTS reviews (
    review_id               TEXT NOT NULL,
    order_id                TEXT NOT NULL PRIMARY KEY REFERENCES orders (order_id),
    review_score            INTEGER,          -- out-of-range scores are flagged, not blocked
    review_comment_title    TEXT,
    review_comment_message  TEXT,
    review_creation_date    TEXT,
    review_answer_timestamp TEXT,
    flag_malformed_date     INTEGER CHECK (flag_malformed_date IN (0, 1)),
    has_comment             INTEGER CHECK (has_comment         IN (0, 1)),
    flag_invalid_score      INTEGER CHECK (flag_invalid_score  IN (0, 1))
) STRICT;

-- -----------------------------------------------------------------------------
-- INDEXES  (for columns used in JOIN … ON and WHERE / GROUP BY)
-- Each PRIMARY KEY already has its own index. A composite PK (order_id, order_item_id)
-- also serves lookups on order_id alone (leftmost column), so order_items.order_id and
-- payments.order_id need no extra index.
-- Not indexed on purpose: order_status (8 values; an index barely narrows the search).
-- -----------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_orders_customer_id      ON orders (customer_id);         -- join to customers
CREATE INDEX IF NOT EXISTS idx_orders_purchase_month   ON orders (purchase_month);      -- monthly report filter / GROUP BY
CREATE INDEX IF NOT EXISTS idx_order_items_product_id  ON order_items (product_id);     -- join to products (category KPIs)
CREATE INDEX IF NOT EXISTS idx_order_items_seller_id   ON order_items (seller_id);      -- join to sellers (seller KPIs)
CREATE INDEX IF NOT EXISTS idx_customers_unique_id     ON customers (customer_unique_id); -- repeat-customer KPI groups by person

-- -----------------------------------------------------------------------------
-- PIPELINE TABLES  (the pipeline's own memory: what ran, what failed, what changed)
-- -----------------------------------------------------------------------------

-- One row per processed file. The loader (Step 2.2) checks this before loading,
-- which makes a second run skip files already loaded (idempotency).
CREATE TABLE IF NOT EXISTS load_log (
    file_name   TEXT    NOT NULL PRIMARY KEY,   -- e.g. 'orders_2017_11.csv'
    table_name  TEXT    NOT NULL,
    rows_loaded INTEGER NOT NULL,
    loaded_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    status      TEXT    NOT NULL CHECK (status IN ('success', 'failed'))
) STRICT;

-- One row per check per run, so DQ history can be compared across months (Step 2.4).
CREATE TABLE IF NOT EXISTS dq_results (
    run_id      TEXT    NOT NULL,               -- e.g. '2026-11-25T08:00:00'
    check_name  TEXT    NOT NULL,               -- e.g. 'items_price_not_positive'
    failed_rows INTEGER NOT NULL,
    run_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    -- Step 2.4: ERROR = structural, stops the pipeline; WARNING = known real-data issue.
    -- Last column on purpose: an existing database gets it from ALTER TABLE ADD COLUMN
    -- (checks.ensure_severity_column), which always appends at the end.
    severity    TEXT    NOT NULL DEFAULT 'ERROR' CHECK (severity IN ('ERROR', 'WARNING')),
    PRIMARY KEY (run_id, check_name)            -- each check once per run
) STRICT;

-- Written by a trigger (Step 2.3) whenever orders.order_status changes.
-- audit_id: one order can change status several times, so order_id alone can't be the key.
-- No FK to orders on purpose: an audit trail must survive even if the order row is removed.
CREATE TABLE IF NOT EXISTS order_status_audit (
    audit_id   INTEGER PRIMARY KEY,             -- SQLite fills this automatically (rowid)
    order_id   TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT,
    changed_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
) STRICT;

COMMIT;
