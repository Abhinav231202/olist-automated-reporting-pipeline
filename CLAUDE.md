# CLAUDE.md — Project instructions for Claude Code

## Who I am and what this project is for
I'm preparing for **Data Analyst interviews** (technical + behavioral). I already know basic
Python and SQL; I'm short on time (3 days), so I need to build fast **and** understand everything.

Project: an **automated data pipeline** on the Olist Brazilian E-Commerce dataset.
Business story: *"An operations team built a monthly performance report manually in Excel.
I automated it: new data files arrive → Python cleans them → loads into SQL incrementally →
SQL runs data-quality checks and KPIs → Python generates an Excel report with charts, on a schedule."*

## Environment
- OS: **Windows**. Project folder: `C:\Projects\DA-Project`. It's outside OneDrive and the path has no spaces, so keep it there.
- Raw Kaggle CSVs (9 files) are in `data/raw/`. Use `pathlib` with paths relative to the project root,
  so code works regardless of slash direction. Never hard-code `C:\Users\...` paths.
- `data/archive/` is ONLY for incoming files after the pipeline processes them. It has nothing to do with the Kaggle zip.
- Scheduling target: Windows Task Scheduler.

Follow `PROJECT_GUIDE.md` for the step order — it has detailed what/how/why for every step
(e.g. the cleaning checklist A–J in Step 1.4, the KPI table in Step 2.5). Stick to its scope. Database: **SQLite** (via Python's `sqlite3` / pandas).

## How to work with me (tutor mode)
1. **Speed matters, but understanding matters more.** You may write most of the Python, but for every
   step, briefly explain *what* each important function does and *why* we chose it over alternatives.
2. **SQL: I write first.** For KPI queries and interview-practice queries, give me the business
   question and table names, let me attempt it, then review and correct. Only write it for me if I ask.
3. Keep code simple, readable, and commented — I need to explain every line in an interview.
4. Use pathlib, logging (not print) in pipeline code, and functions with docstrings.
5. When there are several ways to do something, name the alternative in one line
   (e.g. "`merge` vs `join`", "`RANK` vs `DENSE_RANK`") — these are interview questions.
6. For every key pandas or SQL technique, add one line with its **Excel equivalent** (and SQL/pandas
   equivalent where relevant), e.g. "`isna().sum()` = Excel `COUNTBLANK` = SQL `COUNT(*) - COUNT(col)`".
   If a technique isn't in `analyst_playbook.md` yet, add a row to the matching table there.
7. Don't over-engineer: no classes/frameworks unless needed. No Airflow, no Docker.

## Interview notes — IMPORTANT
After **every completed step** in PROJECT_GUIDE.md, append to `interview_notes.md` under the
matching section (do not rewrite earlier content). For each step add:
- **What we built** (1–2 lines, plain English)
- **Key functions/concepts used** — each with a one-line explanation and a tiny example
- **Decisions & why** — choices made, alternatives rejected, trade-offs
- **Real numbers** — rows in/out, duplicates removed, nulls found, runtime, etc. (these make STAR answers credible)
- **Likely interview questions** on this step, with a short model answer
- **Problems hit & how we solved them** (behavioral "challenge" material)

Also keep the "Running metrics" table at the top of `interview_notes.md` updated.

## Project journal — IMPORTANT
`project_journal.md` is the story of the build (timeline + issues log + behavioral story seeds).
- Whenever something goes wrong (an error, a wrong result, a bad assumption, a design change), add a row to
  its **Issues log** table: step, problem/symptom, root cause, fix, lesson. Continue the numbering.
- At the end of each step, add a short entry under **Timeline** (what was built, key numbers, any change of plan).
- When I say "end of session", update the **Where things stand** and **Resume here** sections.
- If an issue would make a good interview story, add or extend a STAR seed under **Behavioral story seeds**.

## Checkpoint reviews
`checkpoint_review.md` holds study material per checkpoint. When I say "checkpoint review", append a new
section for the steps since the last one (same structure: pitch, numbers, behavioral Q&A, technical follow-ups,
code review findings, self-test). Read section 7 of the existing file before Step 1.4.

## End of each day
When I say "end of day", give me: a 5-question quiz on today's work, then update the
"Daily recap" section of `interview_notes.md`.
