"""
scheduler.py  —  Step 3.3 (the ALTERNATIVE; the real schedule is Windows Task Scheduler)
An in-process scheduler: a Python loop that runs the pipeline every day at 07:00,
using the `schedule` library.

  schedule.every().day.at("07:00").do(job)   -> register the job
  while True: schedule.run_pending()         -> check once a minute "is anything due?"

The catch: this only works while THIS script keeps running. Close the terminal, log off or
restart the PC and nothing runs, until someone starts the script again. That's why the
project uses Task Scheduler (task "Olist Pipeline" -> run_pipeline.bat): the OS owns the
schedule and survives restarts.

Usage:
  python src/scheduler.py          run forever, pipeline every day at 07:00 (Ctrl+C to stop)
  python src/scheduler.py --now    run the job once right now and exit (to test it)
  python src/scheduler.py --at 14:30    another time of day (HH:MM, 24-hour clock)
"""

import argparse
import logging
import pathlib
import subprocess
import sys
import time

import schedule

ROOT     = pathlib.Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "src" / "run_pipeline.py"
LOG_FILE = ROOT / "logs" / "scheduler.log"

log = logging.getLogger("scheduler")


def setup_logging() -> None:
    """Console + logs/scheduler.log. The pipeline itself still writes its own daily log."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)-9s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )


def run_pipeline_job() -> int:
    """
    Start run_pipeline.py as a SEPARATE process and log its exit code.

    Why a subprocess instead of `import run_pipeline; run_pipeline.main()`?
      - same contract as Task Scheduler: a fresh Python each run, judged by its exit code
      - a crash or memory leak in one run (pandas, matplotlib) can't kill this loop
    sys.executable = the python.exe running this script, so no hard-coded path.
    The pipeline's lock file still stops overlap with a Task Scheduler run.
    """
    log.info("starting pipeline: %s", PIPELINE.relative_to(ROOT))
    t0 = time.perf_counter()
    result = subprocess.run([sys.executable, str(PIPELINE)], cwd=ROOT)
    seconds = time.perf_counter() - t0
    if result.returncode == 0:
        log.info("pipeline finished: exit code 0 (success) in %.1f s", seconds)
    else:
        # in production this is where an email / Teams alert would go
        log.error("pipeline FAILED: exit code %d after %.1f s - see logs/pipeline_YYYYMMDD.log",
                  result.returncode, seconds)
    return result.returncode


def main() -> int:
    """Register the daily job and loop, or run it once with --now."""
    p = argparse.ArgumentParser(description="In-process daily scheduler for the Olist pipeline.")
    p.add_argument("--now", action="store_true", help="run the job once now and exit")
    p.add_argument("--at", default="07:00", help="time of day, HH:MM (default 07:00)")
    args = p.parse_args()
    setup_logging()

    if args.now:
        return run_pipeline_job()

    schedule.every().day.at(args.at).do(run_pipeline_job)
    log.info("scheduler started: pipeline every day at %s | next run %s | Ctrl+C to stop",
             args.at, schedule.next_run())
    try:
        while True:
            schedule.run_pending()     # runs any job that is due, then returns
            time.sleep(60)             # check once a minute; the job runs within a minute of 07:00
    except KeyboardInterrupt:
        log.info("scheduler stopped (Ctrl+C)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
