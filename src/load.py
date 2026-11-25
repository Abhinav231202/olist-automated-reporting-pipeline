"""
load.py  —  Step 2.2
Incremental, idempotent loader: cleaned data → db/olist.db.

  1. Dimensions first (customers, products, sellers), because foreign keys need the parent
     rows to exist before facts point at them. Each runs once, tracked in load_log.
  2. Geolocation (~1M rows) read in chunks of 100,000, reduced to one row per zip prefix.
  3. Every month in data/incoming:  skip if load_log says it's loaded → clean_month() →
     insert all 4 tables in ONE transaction (all-or-nothing) → load_log → move to data/archive.
     If a month fails: roll back, log the error, move its files to data/failed, carry on.

data/holdback is never read here: it's saved for the live demo (Step 3.4).

Usage:
  python src/load.py      → exit code 0 = every month loaded or skipped, 1 = a month failed
"""

import logging
import pathlib
import re
import shutil
import sqlite3
import sys
import time

import pandas as pd

import clean
import db

# ── paths & constants ─────────────────────────────────────────────────────────
ROOT     = pathlib.Path(__file__).resolve().parent.parent
RAW      = ROOT / "data" / "raw"
INCOMING = ROOT / "data" / "incoming"
ARCHIVE  = ROOT / "data" / "archive"
FAILED   = ROOT / "data" / "failed"
LOG_FILE = ROOT / "logs" / "load.log"

GEO_FILE   = RAW / "olist_geolocation_dataset.csv"
CHUNK_SIZE = 100_000

# Dimension table → the raw file it comes from (the file name is the load_log key).
# products also uses product_category_name_translation.csv (merged in clean_products).
REFERENCE_FILES = {
    "customers": "olist_customers_dataset.csv",
    "products":  "olist_products_dataset.csv",
    "sellers":   "olist_sellers_dataset.csv",
}

# Rough bounding box of Brazil. Profiling found 42 geolocation points outside it
# (bad GPS); they are left out of the average so they can't drag a zip's location abroad.
BRAZIL_LAT = (-33.75, 5.27)
BRAZIL_LNG = (-73.99, -34.79)

# orders_2017_11.csv → table 'orders', month '2017_11'
MONTH_FILE = re.compile(r"^(orders|order_items|payments|reviews)_(\d{4}_\d{2})\.csv$")

log = logging.getLogger("load")


# ── load_log helpers ──────────────────────────────────────────────────────────

def already_loaded(conn: sqlite3.Connection, file_name: str) -> bool:
    """
    True if load_log has a successful load for this file.
    `?` is a placeholder: sqlite3 sends file_name as a VALUE, separate from the SQL text,
    so a name like  x'; DROP TABLE orders; --  can't change the query (SQL injection).
    Never build SQL with f-strings from data.
    """
    row = conn.execute(
        "SELECT 1 FROM load_log WHERE file_name = ? AND status = 'success'", (file_name,)
    ).fetchone()
    return row is not None


def record_load(conn: sqlite3.Connection, file_name: str, table: str, rows: int, status: str) -> None:
    """
    Write or overwrite one load_log row: an UPSERT.
    INSERT OR IGNORE would keep the OLD row; here a file that failed yesterday and loads
    today must change from 'failed' to 'success', so ON CONFLICT … DO UPDATE replaces it.
    """
    conn.execute(
        """
        INSERT INTO load_log (file_name, table_name, rows_loaded, loaded_at, status)
        VALUES (?, ?, ?, datetime('now', 'localtime'), ?)
        ON CONFLICT (file_name) DO UPDATE SET
            table_name  = excluded.table_name,
            rows_loaded = excluded.rows_loaded,
            loaded_at   = excluded.loaded_at,
            status      = excluded.status
        """,
        (file_name, table, rows, status),
    )


# ── inserting a DataFrame ─────────────────────────────────────────────────────

def to_rows(df: pd.DataFrame) -> list[tuple]:
    """
    DataFrame → list of plain-Python tuples that sqlite3 can bind.
    sqlite3 only understands int / float / str / bytes / None, not numpy or pandas types:
      datetime → 'YYYY-MM-DD HH:MM:SS' text (NaT → None)
      bool / nullable boolean → 0 / 1 (NA → None, i.e. NULL = unknown)
      every NaN / NA / NaT → None
    """
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].dt.strftime(clean.DATE_FORMAT)
        elif pd.api.types.is_bool_dtype(out[col]):
            out[col] = out[col].astype("Int64")
    out = out.astype(object).where(out.notna(), None)
    return list(out.itertuples(index=False, name=None))


def insert_rows(conn: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    """
    INSERT OR IGNORE every row of df into table and return how many were actually NEW.

    OR IGNORE: a row whose primary key already exists is skipped silently, so re-running
    the same data adds nothing (idempotency). It does NOT skip foreign-key errors (tested
    in Step 2.1), so a real orphan still fails the whole transaction, which is what we want.

    executemany = one prepared statement, run for every tuple. Values go in via `?`;
    table/column names can't be placeholders, so they come from the schema itself
    (pragma_table_info), never from file contents.
    to_sql alternative: shorter, but it can only plain-INSERT, so one duplicate key aborts it.
    """
    table_cols = [r[0] for r in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))]
    if list(df.columns) != table_cols:
        raise ValueError(f"{table}: DataFrame columns don't match the table "
                         f"(differences: {sorted(set(df.columns) ^ set(table_cols))})")
    cols = ", ".join(f'"{c}"' for c in table_cols)
    marks = ", ".join("?" for _ in table_cols)
    cur = conn.executemany(f'INSERT OR IGNORE INTO "{table}" ({cols}) VALUES ({marks})', to_rows(df))
    return cur.rowcount          # rows really inserted (ignored ones don't count)


# ── 1. dimension tables ───────────────────────────────────────────────────────

def load_reference(conn: sqlite3.Connection) -> dict[str, int]:
    """
    Load customers, products (with English category) and sellers once.
    Already in load_log → skipped, without even cleaning. Each table loads in its own
    transaction together with its load_log row, so the data and the log always agree.
    Returns {table: rows inserted}.
    """
    todo = [t for t, f in REFERENCE_FILES.items() if not already_loaded(conn, f)]
    if not todo:
        log.info("reference tables: already loaded, skipped")
        return {}

    t0 = time.perf_counter()
    cleaned = clean.clean_reference()   # cleans all 7 raw tables (~3.5 s); only the dimensions are used here
    inserted = {}
    for table in todo:
        df, stats = cleaned[table]
        with conn:                      # commit on success, roll back on any error
            n = insert_rows(conn, table, df)
            record_load(conn, REFERENCE_FILES[table], table, n, "success")
        inserted[table] = n
        log.info("reference %-10s cleaned %6d -> inserted %6d (ignored %d)",
                 table, stats["rows_out"], n, stats["rows_out"] - n)
    log.info("reference tables loaded in %.1f s", time.perf_counter() - t0)
    return inserted


# ── 2. geolocation: the large file, in chunks ─────────────────────────────────

def load_geolocation(conn: sqlite3.Connection, chunksize: int = CHUNK_SIZE) -> int:
    """
    Read ~1M rows in chunks and reduce them to one row per zip prefix (~19k rows).

    Why chunks: read_csv(chunksize=…) returns an iterator of DataFrames, so only one chunk
    is in memory at a time, not the whole file. Same idea as Power Query streaming a file
    Excel couldn't open (Excel's limit is 1,048,576 rows).

    Why not de-duplicate each chunk and insert it straight away: the file is NOT sorted by
    zip, so one zip can appear in several chunks. Each chunk therefore produces PARTIAL
    results (sum of lat/lng + point count; count per city spelling) that are combined
    after the last chunk. Sums and counts combine exactly; an average or a median wouldn't.
    That's the map-reduce pattern.

    Output per zip: mean lat/lng of points inside Brazil, and the most common city/state.
    Returns rows inserted.
    """
    if already_loaded(conn, GEO_FILE.name):
        log.info("geolocation: already loaded, skipped")
        return 0

    t0 = time.perf_counter()
    rows_in = outside = 0
    peak_mb = total_mb = 0.0
    coord_parts, name_parts = [], []
    zip_col = "geolocation_zip_code_prefix"

    reader = pd.read_csv(GEO_FILE, dtype=str, keep_default_na=False,
                         encoding="utf-8-sig", chunksize=chunksize)
    for i, chunk in enumerate(reader, start=1):
        mb = chunk.memory_usage(deep=True).sum() / 1e6
        rows_in, peak_mb, total_mb = rows_in + len(chunk), max(peak_mb, mb), total_mb + mb

        # same text rules as customers/sellers: 'São Paulo ' → 'sao paulo', 'sp' → 'SP'
        chunk, _ = clean.standardize_text(chunk, lower_cols=("geolocation_city",))
        chunk[zip_col] = chunk[zip_col].str.zfill(5)
        chunk["geolocation_city"]  = clean.remove_accents(chunk["geolocation_city"])
        chunk["geolocation_state"] = chunk["geolocation_state"].str.upper()
        lat = pd.to_numeric(chunk["geolocation_lat"], errors="coerce")
        lng = pd.to_numeric(chunk["geolocation_lng"], errors="coerce")
        in_brazil = lat.between(*BRAZIL_LAT) & lng.between(*BRAZIL_LNG)
        outside += int((~in_brazil & lat.notna() & lng.notna()).sum())

        # partial results for this chunk (the "map" step).  Excel: SUMIFS / COUNTIFS per zip
        coord_parts.append(
            pd.DataFrame({zip_col: chunk[zip_col], "lat": lat, "lng": lng})[in_brazil]
              .groupby(zip_col).agg(lat_sum=("lat", "sum"), lng_sum=("lng", "sum"), n=("lat", "size")))
        name_parts.append(
            chunk.groupby([zip_col, "geolocation_city", "geolocation_state"], dropna=False).size())
        log.info("geolocation chunk %2d: %7d rows, %5.1f MB, %5d zips in chunk",
                 i, len(chunk), mb, chunk[zip_col].nunique())

    # combine the partials (the "reduce" step): add up sums and counts across chunks
    coords = pd.concat(coord_parts).groupby(level=0).sum()
    coords["geolocation_lat"] = coords["lat_sum"] / coords["n"]
    coords["geolocation_lng"] = coords["lng_sum"] / coords["n"]

    # most common spelling per zip; ties → alphabetical, so the result is repeatable
    names = (pd.concat(name_parts).groupby(level=[0, 1, 2], dropna=False).sum()
               .rename("n").reset_index()
               .sort_values([zip_col, "n", "geolocation_city"], ascending=[True, False, True])
               .drop_duplicates(subset=zip_col))

    geo = (names.merge(coords[["geolocation_lat", "geolocation_lng"]],
                       left_on=zip_col, right_index=True, how="left", validate="one_to_one")
                [[zip_col, "geolocation_lat", "geolocation_lng", "geolocation_city", "geolocation_state"]])

    with conn:
        n = insert_rows(conn, "geolocation", geo)
        record_load(conn, GEO_FILE.name, "geolocation", n, "success")

    log.info("geolocation: %d rows in -> %d rows out (one per zip prefix), inserted %d", rows_in, len(geo), n)
    log.info("geolocation: %d points outside Brazil excluded from the average; %d zips with no valid point",
             outside, int(geo["geolocation_lat"].isna().sum()))
    log.info("geolocation: peak chunk %.0f MB vs %.0f MB for the whole file in memory; %.1f s",
             peak_mb, total_mb, time.perf_counter() - t0)
    return n


# ── 3. monthly files ──────────────────────────────────────────────────────────

def find_months(folder: pathlib.Path) -> list[str]:
    """Months with at least one file in folder, oldest first ('2016_10', '2017_01', …)."""
    months = set()
    for path in folder.glob("*.csv"):
        m = MONTH_FILE.match(path.name)
        if m:
            months.add(m.group(2))
        else:
            log.warning("ignoring unexpected file %s", path.name)
    return sorted(months)


def move_file(src: pathlib.Path, dest_dir: pathlib.Path) -> None:
    """Move a file; if one with the same name is already there, add a timestamp instead of overwriting."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if dest.exists():
        dest = dest_dir / f"{src.stem}_{time.strftime('%Y%m%d_%H%M%S')}{src.suffix}"
    shutil.move(str(src), str(dest))


def load_month(conn: sqlite3.Connection, month: str, incoming: pathlib.Path = INCOMING,
               archive: pathlib.Path = ARCHIVE, failed: pathlib.Path = FAILED) -> dict:
    """
    Load one month's 4 files as ONE unit. Returns {'status': ..., 'inserted': {table: n}}.

    Order inside the transaction = FK order: orders before items / payments / reviews.
    `with conn:` = BEGIN … COMMIT, or ROLLBACK if anything raises, so an error in
    reviews also undoes that month's orders, items and payments. No half-loaded months.
    Files are moved only AFTER the commit: a file system move can't be rolled back.
    If the move itself fails, the month is already in load_log, so the next run skips it.
    """
    files = {t: f"{t}_{month}.csv" for t in clean.MONTH_TABLES}
    present = [incoming / f for f in files.values() if (incoming / f).exists()]

    if all(already_loaded(conn, f) for f in files.values()):
        log.warning("%s: already in load_log, skipped (files left in %s)", month, incoming.name)
        return {"status": "skipped", "inserted": {}}

    t0 = time.perf_counter()
    try:
        results = clean.clean_month(month, folders=(incoming,))   # only the files we will move
        inserted = {}
        with conn:
            for table in clean.MONTH_TABLES:
                df, stats = results[table]
                inserted[table] = insert_rows(conn, table, df)
                record_load(conn, files[table], table, inserted[table], "success")
    except Exception:
        # the with-block has already rolled back; record the failure in its own transaction
        log.exception("%s: FAILED, rolled back, moving %d file(s) to %s", month, len(present), failed.name)
        with conn:
            for table, f in files.items():
                record_load(conn, f, table, 0, "failed")
        for path in present:
            move_file(path, failed)
        return {"status": "failed", "inserted": {}}

    for path in present:
        move_file(path, archive)
    log.info("%s: loaded in %.2f s | %s", month, time.perf_counter() - t0,
             ", ".join(f"{t} {results[t][1]['rows_out']}->{n}" for t, n in inserted.items()))
    return {"status": "loaded", "inserted": inserted}


def load_incoming(conn: sqlite3.Connection, incoming: pathlib.Path = INCOMING,
                  archive: pathlib.Path = ARCHIVE, failed: pathlib.Path = FAILED) -> dict:
    """Load every month waiting in `incoming`. One failed month doesn't stop the others."""
    months = find_months(incoming)
    log.info("incoming: %d month(s) found in %s", len(months), incoming.name)
    summary = {"loaded": 0, "skipped": 0, "failed": 0,
               "inserted": {t: 0 for t in clean.MONTH_TABLES}}
    for month in months:
        result = load_month(conn, month, incoming, archive, failed)
        summary[result["status"]] += 1
        for t, n in result["inserted"].items():
            summary["inserted"][t] += n
    return summary


# ── entry point ───────────────────────────────────────────────────────────────

def setup_logging() -> None:
    """Log to the console AND logs/load.log (appended), so scheduled runs leave a history."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )
    logging.getLogger("clean").setLevel(logging.WARNING)   # per-table cleaning lines are too chatty here


def main() -> int:
    """Run the full load; return an exit code for the orchestrator / Task Scheduler."""
    setup_logging()
    t0 = time.perf_counter()
    conn = db.get_connection()
    try:
        load_reference(conn)
        load_geolocation(conn)
        summary = load_incoming(conn)
        for table in ("customers", "products", "sellers", "geolocation", *clean.MONTH_TABLES):
            total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]   # name from our own list
            log.info("table %-12s %7d rows", table, total)
    finally:
        conn.close()
    log.info("months: %d loaded, %d skipped, %d failed | new rows: %s | %.1f s total",
             summary["loaded"], summary["skipped"], summary["failed"],
             summary["inserted"], time.perf_counter() - t0)
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
