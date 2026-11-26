"""
run_pipeline.py  —  Step 3.2
ONE command runs the whole pipeline, unattended (e.g. by Windows Task Scheduler at 7 AM):

  1. lock      : logs/pipeline.lock, so two runs can never overlap
  2. load      : reference tables + geolocation (once) and every new month in data/incoming
                 (clean -> load in one transaction -> archive; a bad month is rolled back,
                 moved to data/failed, and the run carries on)            -> load.py
  3. checks    : every data-quality check in sql/checks.sql               -> checks.py
                 any ERROR check fails -> STOP before the report (exit code 1)
                 WARNINGs              -> logged, the report still runs
  4. report    : reports/ops_report_YYYY-MM-DD.xlsx                       -> report.py
  5. summary   : one block at the end of the log + an exit code for the scheduler

No pipeline logic lives here: this file only decides the ORDER and what happens when a step
fails. data/holdback is never read (it's for the Step 3.4 live demo).

Usage:
  python src/run_pipeline.py                 normal run
  python src/run_pipeline.py --dry-run       show what would happen, change nothing
  python src/run_pipeline.py --skip-checks   (testing only) report without new DQ checks
  python src/run_pipeline.py --skip-report   load + checks only
  python src/run_pipeline.py --db PATH       run against another database file (e.g. a test copy)

Exit codes: 0 = success (or nothing to do / another run is active)
            1 = failure: a month failed to load, an ERROR check failed, or a step crashed
"""

import argparse
import datetime
import logging
import os
import pathlib
import sqlite3
import sys
import time

import checks
import clean
import db
import load
import report

# ── paths & constants ─────────────────────────────────────────────────────────
ROOT      = pathlib.Path(__file__).resolve().parent.parent
LOG_DIR   = ROOT / "logs"
LOCK_FILE = LOG_DIR / "pipeline.lock"
# A normal run takes ~15 s. A lock older than this was left by a run that was KILLED
# (power cut, Task Scheduler "stop the task"), when even `finally` can't run.
STALE_LOCK_SECONDS = 2 * 60 * 60

ALL_TABLES = ("customers", "products", "sellers", "geolocation", *clean.MONTH_TABLES)

log = logging.getLogger("pipeline")


# ── logging ───────────────────────────────────────────────────────────────────

class AsciiFormatter(logging.Formatter):
    """
    Turn every log line into plain ASCII ('São Paulo' -> 'S?o Paulo').
    The Windows console (cp1252) can crash or garble on other characters (Issue #25), and
    a traceback with an accented file name must never break the logging of that traceback.
    """
    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).encode("ascii", "replace").decode("ascii")


def setup_logging() -> pathlib.Path:
    """
    Console + logs/pipeline_YYYYMMDD.log (one file per day, appended).
    Configured on the ROOT logger, so the 'load', 'checks' and 'report' loggers inside
    the reused modules write into the same file. force=True replaces any earlier setup.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"pipeline_{datetime.date.today():%Y%m%d}.log"
    fmt = AsciiFormatter("%(asctime)s  %(levelname)-7s  %(name)-8s  %(message)s",
                         datefmt="%Y-%m-%d %H:%M:%S")
    handlers = [logging.StreamHandler(), logging.FileHandler(log_file, encoding="ascii")]
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("clean").setLevel(logging.WARNING)       # per-table cleaning lines: too chatty
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)
    return log_file


# ── lock file ─────────────────────────────────────────────────────────────────

def acquire_lock(run_id: str, path: pathlib.Path = LOCK_FILE) -> bool:
    """
    Create the lock file, or return False if another run holds it.

    os.O_CREAT | os.O_EXCL = "create, but fail if the file exists", in ONE operating-system
    call. "if not path.exists(): create it" would be two steps, and two runs starting in
    the same second could both pass the check (a race condition).
    A lock older than STALE_LOCK_SECONDS is assumed abandoned and replaced.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and time.time() - path.stat().st_mtime > STALE_LOCK_SECONDS:
        log.warning("removing stale lock (%s), left by a run that was killed: %s",
                    path.name, path.read_text(encoding="ascii", errors="replace").strip())
        path.unlink(missing_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = path.read_text(encoding="ascii", errors="replace").strip()
        log.warning("another pipeline run is active (%s): exiting without doing anything", holder)
        return False
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(f"pid={os.getpid()} run_id={run_id}\n")
    return True


def release_lock(path: pathlib.Path = LOCK_FILE) -> None:
    """Delete the lock. Called from `finally`, so it also runs after a crash or Ctrl+C."""
    path.unlink(missing_ok=True)


# ── the steps ─────────────────────────────────────────────────────────────────

def incoming_files() -> list[pathlib.Path]:
    """Monthly CSVs waiting in data/incoming (the same file-name rule load.py uses)."""
    return sorted(p for p in load.INCOMING.glob("*.csv") if load.MONTH_FILE.match(p.name))


def run_step(name: str, func, summary: dict, *args):
    """
    Run one step, time it, and record the time in the summary (also if the step raises:
    the exception goes on up to main(), which logs the traceback).
    """
    summary["step"] = name
    log.info("--- step: %s ---", name)
    t0 = time.perf_counter()
    try:
        return func(*args)
    finally:
        summary["timings"][name] = time.perf_counter() - t0
        log.info("--- step %s: %.1f s ---", name, summary["timings"][name])


def step_load(conn: sqlite3.Connection, summary: dict) -> None:
    """
    Reference tables + geolocation (each skips itself once loaded), then the new months.
    No new files -> loading is skipped, but checks and the report still run, so a scheduled
    run ALWAYS leaves a fresh report (and proves the pipeline is alive).
    """
    added = {t: 0 for t in ALL_TABLES}
    added.update(load.load_reference(conn))
    added["geolocation"] += load.load_geolocation(conn)

    files = incoming_files()
    summary["files_found"] = len(files)
    if not files:
        log.info("no new files in data/incoming: monthly load skipped")
    else:
        log.info("%d file(s) in data/incoming", len(files))
        result = load.load_incoming(conn)
        summary.update(months_loaded=result["loaded"], months_skipped=result["skipped"],
                       months_failed=result["failed"])
        for table, n in result["inserted"].items():
            added[table] += n
        if result["failed"]:
            log.error("%d month(s) failed to load: rolled back and moved to data/failed; "
                      "continuing with the data already in the database", result["failed"])

    summary["rows_added"] = added
    for table in ALL_TABLES:     # names come from our own constant, never from data
        total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        log.info("table %-12s +%6d new  -> %8d rows", table, added[table], total)


def step_checks(conn: sqlite3.Connection, summary: dict, run_id: str) -> None:
    """Run all DQ checks under the pipeline's run_id (so dq_results links to this log)."""
    results = checks.run_checks(conn, run_id)
    blocking = checks.blocking_failures(results)
    warned = [r["name"] for r in results if r["severity"] == "WARNING" and r["failed_rows"]]
    summary["checks"] = {"run": len(results), "passed": len(results) - len(blocking) - len(warned),
                         "warned": len(warned), "failed": len(blocking)}
    summary["blocking"] = blocking
    log.info("checks: %(run)d run | %(passed)d passed | %(warned)d WARNING | %(failed)d ERROR",
             summary["checks"])


def step_report(conn: sqlite3.Connection, summary: dict) -> None:
    """Build today's Excel report and keep its path for the summary."""
    path = report.build_report(conn)
    summary["report"] = str(path.relative_to(ROOT))


def run(args: argparse.Namespace, summary: dict) -> None:
    """
    The pipeline itself. Sets summary['status']; any unexpected exception is left for
    main() to log with its traceback.
      fail-fast  : an ERROR check stops everything (a wrong report is worse than no report)
      continue   : a failed month is isolated by load.py; the other months still load
    """
    conn = run_step("connect", db.get_connection, summary, args.db)
    try:
        run_step("load", step_load, summary, conn, summary)

        if args.skip_checks:
            log.warning("--skip-checks: no new DQ run; the report shows the PREVIOUS run's results")
        else:
            run_step("checks", step_checks, summary, conn, summary, summary["run_id"])
            if summary["blocking"]:
                log.error("STOP: ERROR-level checks failed: %s", ", ".join(summary["blocking"]))
                log.error("report NOT built, so nobody sees numbers built on broken data")
                summary["status"] = "FAILED (data-quality ERROR)"
                return

        if args.skip_report:
            log.info("--skip-report: report not built")
        else:
            run_step("report", step_report, summary, conn, summary)
    finally:
        conn.close()

    summary["status"] = "COMPLETED WITH ERRORS" if summary["months_failed"] else "SUCCESS"


# ── dry run ───────────────────────────────────────────────────────────────────

def dry_run(args: argparse.Namespace) -> None:
    """
    Say what a real run would do, without changing anything: the database is opened
    READ-ONLY (mode=ro, so even db.py's view refresh can't write), no files move, no lock.
    The only thing written is the log.
    """
    db_path = pathlib.Path(args.db)
    files = incoming_files()
    log.info("[dry run] database: %s", db_path)
    log.info("[dry run] %d file(s) in data/incoming", len(files))

    if not db_path.exists():
        log.info("[dry run] database doesn't exist yet: a real run would create it and load everything")
    else:
        # file:///C:/...?mode=ro  : a URI so we can pass the read-only option
        conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            for table, f in load.REFERENCE_FILES.items():
                state = "skip (already loaded)" if load.already_loaded(conn, f) else "LOAD"
                log.info("[dry run] reference %-12s %s", table, state)
            state = "skip (already loaded)" if load.already_loaded(conn, load.GEO_FILE.name) else "LOAD"
            log.info("[dry run] reference %-12s %s", "geolocation", state)
            for month in load.find_months(load.INCOMING):
                names = [f"{t}_{month}.csv" for t in clean.MONTH_TABLES]
                done = all(load.already_loaded(conn, n) for n in names)
                log.info("[dry run] month %s: %s", month,
                         "skip (already in load_log)" if done else "LOAD -> archive (or failed)")
        finally:
            conn.close()
    if not files:
        log.info("[dry run] no new files: the monthly load would be skipped")

    if args.skip_checks:
        log.info("[dry run] checks: skipped (--skip-checks)")
    else:
        found = checks.load_checks()
        n_err = sum(c["severity"] == "ERROR" for c in found)
        log.info("[dry run] checks: would run %d (%d ERROR, %d WARNING); any ERROR failure stops the run",
                 len(found), n_err, len(found) - n_err)
    if args.skip_report:
        log.info("[dry run] report: skipped (--skip-report)")
    else:
        log.info("[dry run] report: would write reports/ops_report_%s.xlsx", datetime.date.today())


# ── entry point ───────────────────────────────────────────────────────────────

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Command-line flags. argparse also gives --help for free."""
    p = argparse.ArgumentParser(description="Run the Olist pipeline: load -> checks -> report.")
    p.add_argument("--dry-run", action="store_true", help="show what would run; change nothing")
    p.add_argument("--skip-checks", action="store_true", help="skip DQ checks (testing only)")
    p.add_argument("--skip-report", action="store_true", help="load + checks, no report")
    p.add_argument("--db", default=str(db.DB_PATH),
                   help="database file (default db/olist.db); point at a COPY to test failures")
    return p.parse_args(argv)


def log_summary(s: dict) -> None:
    """The run summary block: the first thing to read in the log the morning after."""
    c = s["checks"]
    lines = [
        f"run_id          {s['run_id']}",
        f"status          {s['status']}",
        f"files found     {s['files_found']} in data/incoming",
        f"months          {s['months_loaded']} loaded, {s['months_skipped']} skipped, "
        f"{s['months_failed']} failed",
        f"rows added      {sum(s['rows_added'].values())} "
        f"({', '.join(f'{t} {n}' for t, n in s['rows_added'].items() if n) or 'none'})",
        f"checks          " + (f"{c['run']} run: {c['passed']} passed, {c['warned']} warned, "
                               f"{c['failed']} failed" if c else "not run"),
        f"report          {s['report'] or 'not built'}",
        f"step timings    " + (", ".join(f"{k} {v:.1f}s" for k, v in s["timings"].items()) or "-"),
        f"total runtime   {s['seconds']:.1f} s",
    ]
    level = logging.INFO if s["status"] == "SUCCESS" else logging.ERROR
    log.log(level, "=" * 60)
    for line in lines:
        log.log(level, "SUMMARY  %s", line)
    log.log(level, "=" * 60)


def main(argv: list[str] | None = None) -> int:
    """
    Parse flags, take the lock, run, ALWAYS release the lock and log the summary.
    Returns the exit code for Task Scheduler: 0 success, 1 failure.
    """
    args = parse_args(argv)
    run_id = datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")   # same format as dq_results.run_id
    log_file = setup_logging()
    log.info("=== pipeline run %s started%s | log: %s ===", run_id,
             " (DRY RUN)" if args.dry_run else "", log_file.relative_to(ROOT))

    if args.dry_run:
        try:
            dry_run(args)
        except Exception:
            log.exception("dry run failed")
            return 1
        log.info("=== dry run finished: nothing was changed ===")
        return 0

    if not acquire_lock(run_id):
        return 0                    # not a failure: the run that holds the lock does the work

    summary = {"run_id": run_id, "status": "FAILED", "step": "start", "files_found": 0,
               "months_loaded": 0, "months_skipped": 0, "months_failed": 0,
               "rows_added": {}, "checks": {}, "blocking": [], "report": None,
               "timings": {}, "seconds": 0.0}
    t0 = time.perf_counter()
    try:
        run(args, summary)
    except Exception:
        # a bug or an unexpected error (disk full, locked DB...): full traceback INTO the log,
        # because at 3 AM nobody is watching the console
        log.exception("pipeline CRASHED during step '%s'", summary["step"])
        summary["status"] = f"FAILED (crash in step '{summary['step']}')"
    finally:
        release_lock()              # runs on success, crash, and Ctrl+C alike
        summary["seconds"] = time.perf_counter() - t0
        log_summary(summary)
        log.info("=== pipeline run %s finished: exit code %d ===", run_id,
                 0 if summary["status"] == "SUCCESS" else 1)
    return 0 if summary["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
