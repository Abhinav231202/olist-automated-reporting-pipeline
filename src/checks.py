"""
checks.py  —  Step 2.4
Runs every data-quality check in sql/checks.sql, stores the results in dq_results,
and tells the pipeline whether it is safe to continue.

  1. parse sql/checks.sql into named checks (name, severity, SQL)
  2. run each check → failed_rows (0 = pass)
  3. save every result under ONE run_id, in one transaction
  4. log a WARNING for each failing check, with the change since the previous run
  5. exit code 1 if any ERROR-level check failed (or a check couldn't run), else 0

Usage:
  python src/checks.py        → exit code 0 = safe to build the report, 1 = stop
"""

import datetime
import logging
import pathlib
import re
import sqlite3
import sys
import time

import db

# ── paths & constants ─────────────────────────────────────────────────────────
ROOT        = pathlib.Path(__file__).resolve().parent.parent
CHECKS_PATH = ROOT / "sql" / "checks.sql"
LOG_FILE    = ROOT / "logs" / "checks.log"
SEVERITIES  = ("ERROR", "WARNING")

# Each check in checks.sql starts with a line like:  -- @check: orphan_items_no_order
CHECK_TAG    = re.compile(r"^--\s*@check:\s*(\w+)\s*$", re.MULTILINE)
SEVERITY_TAG = re.compile(r"^--\s*@severity:\s*(\w+)\s*$", re.MULTILINE)

log = logging.getLogger("checks")


def load_checks(path: pathlib.Path = CHECKS_PATH) -> list[dict]:
    """
    Split checks.sql into a list of {"name", "severity", "sql"} dicts, in file order.

    Why one SQL file with tags, not a Python dict of queries? The SQL stays readable and
    runnable on its own (paste any block into DB Browser), and adding a check means
    editing only the .sql file. re.split with a capturing group returns
    [text_before, name1, block1, name2, block2, ...], so names and blocks alternate.
    Fails loudly on a missing/unknown severity or a repeated name: a typo in the check
    file must not turn into a check that silently never runs.
    """
    parts = CHECK_TAG.split(path.read_text(encoding="utf-8"))
    checks, seen = [], set()
    for name, block in zip(parts[1::2], parts[2::2]):
        sev = SEVERITY_TAG.search(block)
        if sev is None or sev.group(1) not in SEVERITIES:
            raise ValueError(f"check '{name}': needs '-- @severity: ERROR' or 'WARNING'")
        if name in seen:
            raise ValueError(f"check '{name}' appears twice in {path.name}")
        seen.add(name)
        # sqlite3 ignores the -- comment lines; strip the ';' because execute() runs one statement
        checks.append({"name": name, "severity": sev.group(1), "sql": block.strip().rstrip(";")})
    if not checks:
        raise ValueError(f"no '-- @check:' tags found in {path}")
    return checks


def ensure_severity_column(conn: sqlite3.Connection) -> None:
    """
    Add dq_results.severity to a database created before Step 2.4 (a tiny migration).
    schema.sql only runs on an EMPTY database, so an existing one never sees new columns.
    pragma_table_info lists the columns (= INFORMATION_SCHEMA.COLUMNS); ADD COLUMN only if
    missing, so running this every time is harmless (idempotent, like everything else here).
    """
    cols = {r[0] for r in conn.execute("SELECT name FROM pragma_table_info('dq_results')")}
    if "severity" not in cols:
        conn.execute("ALTER TABLE dq_results ADD COLUMN severity TEXT NOT NULL DEFAULT 'ERROR' "
                     "CHECK (severity IN ('ERROR', 'WARNING'))")
        conn.commit()
        log.info("added column dq_results.severity")


def previous_results(conn: sqlite3.Connection, run_id: str) -> dict[str, int]:
    """
    failed_rows per check from the most recent run BEFORE this one ({} on the first run).
    run_id is an ISO timestamp, so MAX() on the text is the latest run (ISO sorts correctly).
    """
    rows = conn.execute(
        "SELECT check_name, failed_rows FROM dq_results "
        "WHERE run_id = (SELECT MAX(run_id) FROM dq_results WHERE run_id < ?)", (run_id,)
    ).fetchall()
    return dict(rows)


def run_checks(conn: sqlite3.Connection, run_id: str | None = None,
               path: pathlib.Path = CHECKS_PATH) -> list[dict]:
    """
    Run every check, save the results under run_id, log failures, return the results.

    A check that raises (e.g. a column renamed) is logged as an ERROR and counted as a
    failed ERROR check, so a broken check can never look like a passing one. It isn't
    stored, because there is no honest failed_rows number for it.
    All inserts are in one transaction (`with conn:`): a run is stored completely or not at all.
    """
    run_id = run_id or datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    ensure_severity_column(conn)
    before = previous_results(conn, run_id)

    results = []
    for chk in load_checks(path):
        t0 = time.perf_counter()
        try:
            failed = conn.execute(chk["sql"]).fetchone()[0]
        except sqlite3.Error as exc:
            log.error("check %s could not run: %s", chk["name"], exc)
            results.append({**chk, "failed_rows": None, "crashed": True})
            continue
        ms = (time.perf_counter() - t0) * 1000

        prev = before.get(chk["name"])
        change = "" if prev is None else f" ({failed - prev:+d} vs last run)"
        if failed:
            log.warning("[%s] %-36s %7d failing rows%s", chk["severity"], chk["name"], failed, change)
        else:
            log.info("[%s] %-36s       0  ok  (%.0f ms)", chk["severity"], chk["name"], ms)
        results.append({**chk, "failed_rows": failed, "crashed": False})

    with conn:   # commit on success, roll back if any insert fails
        conn.executemany(
            "INSERT INTO dq_results (run_id, check_name, failed_rows, severity) VALUES (?, ?, ?, ?)",
            [(run_id, r["name"], r["failed_rows"], r["severity"]) for r in results if not r["crashed"]],
        )
    return results


def blocking_failures(results: list[dict]) -> list[str]:
    """Names of ERROR checks that failed or couldn't run: any of these stops the pipeline."""
    return [r["name"] for r in results
            if r["crashed"] or (r["severity"] == "ERROR" and r["failed_rows"] > 0)]


# ── entry point ───────────────────────────────────────────────────────────────

def setup_logging() -> None:
    """Console + logs/checks.log (appended), the same pattern as load.py."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )


def main() -> int:
    """Run all checks once; return the exit code for the orchestrator / Task Scheduler."""
    setup_logging()
    t0 = time.perf_counter()
    conn = db.get_connection()
    try:
        results = run_checks(conn)
    finally:
        conn.close()

    blocking = blocking_failures(results)
    warnings = [r["name"] for r in results
                if r["severity"] == "WARNING" and r["failed_rows"]]
    log.info("%d checks in %.1f s | ERROR failures: %d | WARNING failures: %d",
             len(results), time.perf_counter() - t0, len(blocking), len(warnings))
    if blocking:
        log.error("STOP: structural checks failed: %s", ", ".join(blocking))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
