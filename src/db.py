"""
db.py  —  Step 2.1 (+ Step 2.3: views and trigger, + Step 2.5: KPI views)
One place that opens the SQLite database, so every script (loader, DQ checks, report)
connects the same way.

get_connection():
  1. opens db/olist.db (sqlite3 creates the file if it's missing)
  2. turns foreign keys ON for this connection
  3. builds every table from sql/schema.sql if the database has no tables yet
  4. (re)creates the base views (sql/views.sql), the KPI views (sql/kpis.sql) and the audit
     trigger (sql/triggers.sql) on EVERY connection, so they always match the files, even on
     an existing database

Usage:
  python src/db.py   → create the database if needed and log its tables, indexes and FKs
"""

import logging
import pathlib
import sqlite3

# ── paths ─────────────────────────────────────────────────────────────────────
ROOT        = pathlib.Path(__file__).resolve().parent.parent   # DA-Project/
DB_PATH     = ROOT / "db" / "olist.db"
SCHEMA_PATH = ROOT / "sql" / "schema.sql"
# Database objects re-applied on every connection. ORDER MATTERS: kpis.sql reads the base views,
# and its LIMIT 0 validation fails if views.sql hasn't created them yet.
OBJECT_SCRIPTS = [
    ROOT / "sql" / "views.sql",      # vw_order_facts, vw_item_facts (base views)
    ROOT / "sql" / "kpis.sql",       # 10 vw_kpi_* views built on the base views
    ROOT / "sql" / "triggers.sql",   # audit trigger on orders.order_status
]

log = logging.getLogger(__name__)


def _table_names(conn: sqlite3.Connection) -> list[str]:
    """
    User tables in the database, read from SQLite's built-in catalogue `sqlite_master`
    (= INFORMATION_SCHEMA.TABLES in Postgres / SQL Server). Names starting with
    'sqlite_' are SQLite's own internal tables, so they're skipped.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def apply_sql_objects(conn: sqlite3.Connection) -> None:
    """
    Run views.sql, kpis.sql and triggers.sql (DROP IF EXISTS + CREATE), so the database
    always has the current version of every view and trigger.

    Why every connection, not just at creation: schema.sql only runs on an empty database,
    but views change while the project grows (new KPI views in Step 2.5). Re-applying takes
    a few milliseconds, because a view stores only its SQL text, not data.

    Each script has its own BEGIN/COMMIT. If one fails halfway (e.g. a typo in a view),
    the open transaction is rolled back here, so the old views stay and the error is raised.
    """
    for script in OBJECT_SCRIPTS:
        try:
            conn.executescript(script.read_text(encoding="utf-8"))
        except sqlite3.Error:
            if conn.in_transaction:
                conn.rollback()
            log.exception("failed to apply %s (previous version kept)", script.name)
            raise
    log.debug("applied %s", ", ".join(s.name for s in OBJECT_SCRIPTS))


def get_connection(db_path: pathlib.Path | str = DB_PATH) -> sqlite3.Connection:
    """
    Open the database, switch on foreign keys, and create the schema if it's empty.

    Why check for *tables* instead of "does the file exist"?  sqlite3.connect() creates
    an empty file immediately. If schema creation then failed, a file-exists check would
    skip creation forever and leave an empty database. Checking sqlite_master
    recovers from that automatically.

    PRAGMA foreign_keys = ON is set on EVERY connection: SQLite stores it per connection,
    not in the file, and it's OFF by default (for backwards compatibility). Without it,
    REFERENCES clauses are only documentation and orphan rows load silently.

    db_path can be ':memory:' for a throwaway in-memory test database.
    """
    if str(db_path) != ":memory:":
        pathlib.Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")   # before any transaction: inside one it's ignored
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite build does not support foreign keys")
    # Page cache: SQLite's default is ~2 MB, so for a 100 MB database every view query
    # re-reads pages from disk. 64 MB (negative = KiB) made vw_order_facts 2x faster and
    # vw_item_facts 5x faster (Step 2.3, measured). Per connection, like foreign_keys.
    conn.execute("PRAGMA cache_size = -65536")

    if not _table_names(conn):
        # executescript runs the whole file (many statements). conn.execute runs only one.
        # The script has its own BEGIN/COMMIT, so a failure leaves no partial schema.
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        log.info("created schema from %s -> %s", SCHEMA_PATH.name, db_path)

    apply_sql_objects(conn)
    return conn


def describe_schema(conn: sqlite3.Connection) -> None:
    """Log each table (row count, columns, PK, FKs, indexes), then each view and trigger."""
    for table in _table_names(conn):
        # pragma_table_info / pragma_foreign_key_list / pragma_index_list = SQLite's version
        # of INFORMATION_SCHEMA.COLUMNS / KEY_COLUMN_USAGE / indexes. Table names come from
        # sqlite_master (not user input), so the f-string is safe here.
        cols = conn.execute(f"SELECT name, pk FROM pragma_table_info('{table}')").fetchall()
        pk   = [name for name, pos in sorted(cols, key=lambda c: c[1]) if pos > 0]
        fks  = conn.execute(
            f"SELECT \"from\", \"table\", \"to\" FROM pragma_foreign_key_list('{table}')").fetchall()
        idx  = conn.execute(
            f"SELECT name FROM pragma_index_list('{table}') WHERE origin = 'c'").fetchall()
        rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        log.info("%-19s %2d cols  %d rows  PK (%s)", table, len(cols), rows, ", ".join(pk))
        for col, parent, parent_col in fks:
            log.info("%19s FK %s -> %s.%s", "", col, parent, parent_col)
        for (name,) in idx:
            log.info("%19s INDEX %s", "", name)

    # views and triggers live in the same catalogue as tables
    for kind, name, on_table in conn.execute(
            "SELECT type, name, tbl_name FROM sqlite_master "
            "WHERE type IN ('view', 'trigger') ORDER BY type DESC, name"):
        log.info("%-19s %s%s", name, kind.upper(), f" on {on_table}" if kind == "trigger" else "")


def main() -> None:
    """CLI: create db/olist.db if needed, then describe what's in it."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    conn = get_connection()
    try:
        log.info("database: %s (%.1f KB)", DB_PATH, DB_PATH.stat().st_size / 1024)
        describe_schema(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
