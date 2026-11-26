@echo off
REM ============================================================================
REM run_pipeline.bat  -  Step 3.3
REM What Windows Task Scheduler runs every day at 07:00 (task "Olist Pipeline").
REM You can also double-click it, or run it from cmd, to test.
REM
REM   1. go to the project folder, so relative paths (data\, db\, logs\) resolve
REM   2. run the pipeline with the FULL path to python.exe (found with `where python`).
REM      A scheduled task doesn't load your terminal's PATH the same way, and the
REM      WindowsApps\python.exe "alias" is only a Microsoft Store shortcut.
REM   3. hand python's exit code back to Task Scheduler (0 = success, 1 = failure),
REM      which shows it as "Last Run Result" 0x0 / 0x1
REM
REM %* passes any extra flags through, e.g.  run_pipeline.bat --dry-run
REM ============================================================================

REM /d also switches the drive letter if needed (plain "cd" doesn't)
cd /d "C:\Projects\DA-Project"

"C:\Users\Abhinav Bhandari\AppData\Local\Programs\Python\Python313\python.exe" src\run_pipeline.py %*

exit /b %ERRORLEVEL%
