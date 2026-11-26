"""
report.py  —  Step 3.1
Builds the monthly operations report for a non-technical manager:
    reports/ops_report_YYYY-MM-DD.xlsx

  1. read every KPI view (vw_kpi_*) with pd.read_sql. NO KPI logic lives in Python, so the
     report can never disagree with the SQL (Python only formats, sorts and picks headlines)
  2. read the latest data-quality run from dq_results (+ the change vs the run before)
  3. write plain-English headline sentences GENERATED from those numbers (nothing hard-coded)
  4. draw 4 matplotlib charts (PNG), each titled with its takeaway, and embed them
  5. write the workbook: Summary · Charts · one sheet per KPI · Definitions · Data Quality

Usage:
  python src/report.py   → exit code 0 + the file path in the log, 1 if there's no data
                           or a KPI view fails (so the orchestrator / Task Scheduler knows)
"""

import datetime
from decimal import ROUND_HALF_UP, Decimal
import logging
import pathlib
import sqlite3
import sys
import time

import matplotlib
matplotlib.use("Agg")    # draw to files only, no window: works under Task Scheduler (no screen)
import matplotlib.pyplot as plt
# "R$ 45k ... R$ 993k": two $ signs make matplotlib render the text between them as a math
# formula (garbled italics). Our labels are money, never maths, so switch that parsing off.
plt.rcParams["text.parse_math"] = False
from matplotlib.ticker import FuncFormatter
import pandas as pd
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import checks   # reuse previous_results() for "change vs last run"
import db

# ── paths & constants ─────────────────────────────────────────────────────────
ROOT        = pathlib.Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "reports"
CHARTS_DIR  = REPORTS_DIR / "charts"        # PNGs, overwritten each run (reused in the README)
LOG_FILE    = ROOT / "logs" / "report.log"

# A state needs at least this many deliveries to be named "worst state" in a headline:
# RR has 31 deliveries, so 5 late orders = 16%. The chart still shows every state, with n.
MIN_DELIVERIES_FOR_HEADLINE = 100

# view → (sheet name ≤ 31 chars, one-line plain-English description shown on the sheet)
KPI_VIEWS = {
    "vw_kpi_monthly_revenue":         ("Monthly Revenue",
        "Revenue and orders per month, and the change vs the previous month (MoM). "
        "No MoM when the previous month isn't loaded."),
    "vw_kpi_running_revenue":         ("Running Revenue",
        "Revenue added up month by month since the first month in the report."),
    "vw_kpi_orders_moving_avg":       ("Orders 3-Month Avg",
        "Orders per month and the average of the last 3 calendar months "
        "(blank until 3 real months exist)."),
    "vw_kpi_top_categories_by_state": ("Top Categories by State",
        "The 3 best-selling product categories in each customer state (ties both shown)."),
    "vw_kpi_delivery_by_state":       ("Delivery by State",
        "Share of orders delivered late, and average review score on time vs late, per state. "
        "Check 'Delivered orders' before trusting a rate: small states swing a lot."),
    "vw_kpi_repeat_customers":        ("Repeat Customers",
        "How many customers ordered more than once in the period."),
    "vw_kpi_cohort_retention":        ("Cohort Retention",
        "Of the customers who first bought in a month (rows), the % who bought again N months "
        "later (columns). Blank = not observed yet / month not loaded, not 0%."),
    "vw_kpi_category_share":          ("Category Share",
        "Each product category's share of total revenue, and the running total (Pareto)."),
    "vw_kpi_top_sellers":             ("Top Sellers",
        "The top 10% of sellers by revenue, and how much of all revenue they bring in."),
    "vw_kpi_second_highest_order":    ("Second Highest Order",
        "For every customer with 2+ orders: their second-largest order."),
}

# Chart colours (validated default palette): one main hue, one accent, grey for context.
BLUE, ORANGE, GREY = "#257de0", "#f47230", "#b8b8b8"
INK, INK_SOFT      = "#191919", "#666666"

# Excel's built-in "conditional formatting" presets (fill + text colour), so they look familiar.
FILL = {
    "error": (PatternFill("solid", fgColor="FFC7CE"), Font(color="9C0006", bold=True)),
    "warn":  (PatternFill("solid", fgColor="FFEB9C"), Font(color="9C5700")),
    "ok":    (PatternFill("solid", fgColor="C6EFCE"), Font(color="006100")),
}
HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(color="FFFFFF", bold=True)

log = logging.getLogger("report")


class ReportError(Exception):
    """The report can't be built honestly (no data, or a KPI view is broken)."""


# ── small formatting helpers (used in sentences and chart titles) ─────────────

def month_name(ym: str) -> str:
    """'2018-05' → 'May 2018'."""
    return pd.Period(ym, freq="M").strftime("%b %Y")


def pct(x: float, places: int = 1) -> str:
    """
    24.25 → '24.3', rounding half UP like Excel's ROUND. Python's f'{24.25:.1f}' gives '24.2':
    for an exact tie it rounds to the even digit ("banker's rounding"), so the report would
    disagree with the SQL value and with Excel. Decimal(str(x)) avoids float noise too.
    """
    q = Decimal(1).scaleb(-places)                       # places=1 → Decimal('0.1')
    return str(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))


def money(x: float) -> str:
    """992871.75 → 'R$ 992,872' (whole reais: a manager doesn't need centavos)."""
    return f"R$ {x:,.0f}"


def money_short(x: float, _pos=None) -> str:
    """10903867 → 'R$ 10.9M', 44507 → 'R$ 45k'. Also used as a chart axis formatter."""
    if abs(x) >= 1e6:
        return f"R$ {x / 1e6:.1f}M"
    if abs(x) >= 1e3:
        return f"R$ {x / 1e3:.0f}k"
    return f"R$ {x:.0f}"


def pretty_category(name: str) -> str:
    """'watches_gifts' → 'Watches gifts' (database names aren't for managers)."""
    return name.replace("_", " ").capitalize()


# ── 1. read the data ──────────────────────────────────────────────────────────

def load_kpis(conn: sqlite3.Connection) -> dict[str, pd.DataFrame]:
    """
    SELECT * from every KPI view into a DataFrame. Raises ReportError if a view fails or the
    database has no orders, because an empty or half-built report is worse than none: a
    manager would read "R$ 0" as a real number.
    """
    kpis = {}
    for view in KPI_VIEWS:
        t0 = time.perf_counter()
        try:
            kpis[view] = pd.read_sql(f"SELECT * FROM {view}", conn)  # names from our own dict
        except Exception as exc:                 # pandas wraps sqlite3 errors in its own type
            raise ReportError(f"KPI view {view} failed: {exc}") from exc
        log.info("read %-32s %6d rows  %5.2f s", view, len(kpis[view]), time.perf_counter() - t0)
    if kpis["vw_kpi_monthly_revenue"].empty:
        raise ReportError("no orders in the database: load data before building the report")
    return kpis


def load_dq(conn: sqlite3.Connection) -> tuple[pd.DataFrame, str | None]:
    """
    The latest DQ run (run_id = MAX(run_id), ISO timestamps sort correctly) with the change
    vs the previous run. Returns (empty frame, None) if checks have never run: the report is
    still useful, and the Data Quality sheet says so plainly.
    """
    run_id = conn.execute("SELECT MAX(run_id) FROM dq_results").fetchone()[0]
    if run_id is None:
        return pd.DataFrame(), None
    dq = pd.read_sql("SELECT check_name, severity, failed_rows FROM dq_results WHERE run_id = ?",
                     conn, params=(run_id,))
    before = checks.previous_results(conn, run_id)          # {} on the first run
    dq["change_vs_previous_run"] = dq["check_name"].map(before)
    dq["change_vs_previous_run"] = dq["failed_rows"] - dq["change_vs_previous_run"]
    # Sort: ERROR failures first, then WARNING failures, then passes (what needs attention on top)
    dq["_order"] = (dq["failed_rows"] == 0) * 2 + (dq["severity"] == "WARNING")
    dq = dq.sort_values(["_order", "failed_rows"], ascending=[True, False]).drop(columns="_order")
    return dq.reset_index(drop=True), run_id


# ── 2. headline sentences (generated from the data) ───────────────────────────

def missing_months(monthly: pd.DataFrame) -> list[str]:
    """Calendar months between the first and last loaded month that have no data ('2016-11', …)."""
    loaded = pd.PeriodIndex(monthly["purchase_month"], freq="M")
    full = pd.period_range(loaded.min(), loaded.max(), freq="M")
    return [str(p) for p in full.difference(loaded)]


def scope_sentence(monthly: pd.DataFrame) -> str:
    """'Covers 18 months of orders, Oct 2016 to May 2018 (Nov 2016, Dec 2016 not loaded).'"""
    first, last = monthly["purchase_month"].iloc[0], monthly["purchase_month"].iloc[-1]
    gap = missing_months(monthly)
    gap_txt = f" ({', '.join(month_name(m) for m in gap)} not in the loaded data)" if gap else ""
    return (f"Scope: {len(monthly)} months of orders, {month_name(first)} to {month_name(last)}"
            f"{gap_txt}. {monthly['orders'].sum():,} valid orders, "
            f"{money(monthly['revenue'].sum())} revenue in total.")


def headline_sentences(kpis: dict[str, pd.DataFrame]) -> list[str]:
    """
    6 plain-English sentences, one per question the manager asks. Every number comes
    from a KPI view; the code only chooses words ("up"/"down") from the sign of the number.
    """
    monthly  = kpis["vw_kpi_monthly_revenue"]
    delivery = kpis["vw_kpi_delivery_by_state"]
    repeat   = kpis["vw_kpi_repeat_customers"].iloc[0]
    category = kpis["vw_kpi_category_share"]
    out = []

    # 1. latest month revenue + MoM (or why there isn't one)
    last = monthly.iloc[-1]
    s = (f"Revenue in {month_name(last.purchase_month)} was {money(last.revenue)} "
         f"from {last.orders:,} orders")
    if pd.notna(last.mom_growth_pct):
        prev = month_name(monthly.iloc[-2].purchase_month)
        direction = "up" if last.mom_growth_pct > 0 else "down" if last.mom_growth_pct < 0 else "unchanged"
        s += f", {direction} {abs(last.mom_growth_pct):.1f}% vs {prev} ({money(last.prev_month_revenue)})."
    else:
        s += f". No month-over-month comparison: {last.mom_note}."
    out.append(s)

    # 2. best month (context for the trend)
    best = monthly.loc[monthly["revenue"].idxmax()]
    s = f"The strongest month was {month_name(best.purchase_month)} with {money(best.revenue)}"
    s += (f" ({best.mom_growth_pct:+.1f}% vs the month before)." if pd.notna(best.mom_growth_pct) else ".")
    out.append(s)

    # 3. late deliveries + worst state (only states with enough deliveries to be fair)
    total = delivery[delivery["state"] == "ALL"].iloc[0]
    states = delivery[(delivery["state"] != "ALL")
                      & (delivery["delivered_orders"] >= MIN_DELIVERIES_FOR_HEADLINE)]
    worst = states.sort_values("late_rate_pct", ascending=False).iloc[0]
    out.append(f"{pct(total.late_rate_pct)}% of delivered orders arrived late "
               f"({total.late_orders:,} of {total.delivered_orders:,}). The worst state was "
               f"{worst.state}: {pct(worst.late_rate_pct)}% late ({worst.late_orders:,} of "
               f"{worst.delivered_orders:,} deliveries).")

    # 4. what lateness costs in reviews
    out.append(f"Late deliveries hurt customer satisfaction: late orders scored "
               f"{total.avg_review_late:.2f} out of 5 on average, vs {total.avg_review_on_time:.2f} "
               f"for on-time orders ({total.review_drop_when_late:.2f} stars lower).")

    # 5. repeat customers
    habit = "most customers buy only once" if repeat.repeat_rate_pct < 10 else "repeat buying is common"
    out.append(f"{pct(repeat.repeat_rate_pct)}% of customers ({int(repeat.repeat_customers):,} of "
               f"{int(repeat.customers):,}) ordered more than once, so {habit}.")

    # 6. category concentration (Pareto) — 'unknown' isn't something to act on, so not named #1
    named = category[category["category"] != "unknown"]
    top = named.iloc[0]
    n80 = int((category["cumulative_pct"] < 80).sum()) + 1
    out.append(f"The top category was {pretty_category(top.category)} ({money(top.revenue)}, "
               f"{pct(top.pct_of_total)}% of revenue); {n80} of {len(category)} categories make up "
               f"80% of revenue.")

    return out


def data_status_sentence(dq: pd.DataFrame) -> str:
    """One line on whether to trust the numbers, shown under the headlines (not one of them)."""
    if dq.empty:
        return "Data checks: no data-quality run found, so these numbers have not been checked."
    errors = int(((dq.severity == "ERROR") & (dq.failed_rows > 0)).sum())
    warns  = int(((dq.severity == "WARNING") & (dq.failed_rows > 0)).sum())
    if errors:
        return (f"WARNING: {errors} critical data check(s) failed. Treat these numbers as "
                f"unreliable until fixed (see Data Quality).")
    return (f"Data checks: {len(dq)} run, 0 critical problems; {warns} known data issues are "
            f"tracked (see Data Quality).")


def gap_note(monthly: pd.DataFrame) -> str:
    """One line explaining the blank MoM cells, generated from mom_note."""
    gap_rows = monthly[monthly["mom_note"] == "previous month not loaded (gap)"]
    if gap_rows.empty:
        return "Every month has a month-over-month figure except the first month loaded."
    months = ", ".join(month_name(m) for m in gap_rows["purchase_month"])
    return (f"Note: a month whose previous month isn't loaded has no month-over-month (MoM) figure "
            f"(here: {months}), so a data gap is never shown as growth or a drop.")


# ── 3. charts ─────────────────────────────────────────────────────────────────

def _style(ax) -> None:
    """Quiet axes: no top/right border, light grid, grey tick labels (the data is the focus)."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.spines["left"].set_color(GREY)
    ax.spines["bottom"].set_color(GREY)
    ax.tick_params(colors=INK_SOFT, labelsize=9)
    ax.set_axisbelow(True)


def _save(fig, name: str) -> pathlib.Path:
    """Save at 150 dpi (sharp in Excel, small file), then close the figure to free memory."""
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    path = CHARTS_DIR / name
    fig.tight_layout()
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)   # without this, a scheduled run that never exits keeps every figure in memory
    return path


def chart_revenue(monthly: pd.DataFrame) -> pathlib.Path:
    """
    Top: revenue per month as a line on a REAL calendar axis. Missing months are NaN, so the
    line breaks and a grey band says "not loaded" (a 0 would draw a fake crash).
    Bottom: MoM % as bars (blue up / orange down). Two panels, not a second y-axis on one
    chart: R$ and % on one chart would let the scale choice tell the story.
    """
    full = pd.period_range(monthly["purchase_month"].iloc[0], monthly["purchase_month"].iloc[-1], freq="M")
    data = monthly.set_index(pd.PeriodIndex(monthly["purchase_month"], freq="M")).reindex(full)
    x = range(len(full))
    gap = data["revenue"].isna()

    fig, (ax, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True,
                                  gridspec_kw={"height_ratios": [3, 1.3]})
    ax.plot(x, data["revenue"], color=BLUE, linewidth=2, marker="o", markersize=4)
    for i, missing in enumerate(gap):
        if missing:
            for a in (ax, ax2):
                a.axvspan(i - 0.5, i + 0.5, color="#eeeeee", zorder=0)
    if gap.any():
        mid = sum(i for i, m in enumerate(gap) if m) / gap.sum()
        ax.text(mid, data["revenue"].max() * 0.5, "not\nloaded", ha="center", fontsize=8, color=INK_SOFT)

    # label only the last point and the peak (selective labels, not a number on every point)
    last_i = len(full) - 1
    peak_i = int(data["revenue"].fillna(-1).to_numpy().argmax())
    for i in {last_i, peak_i}:
        ax.annotate(money_short(data["revenue"].iloc[i]), (i, data["revenue"].iloc[i]),
                    textcoords="offset points", xytext=(0, 8), ha="center", fontsize=9, color=INK)
    ax.yaxis.set_major_formatter(FuncFormatter(money_short))
    ax.set_ylim(0, data["revenue"].max() * 1.15)        # start at 0: no exaggerated swings
    ax.set_ylabel("Revenue", color=INK_SOFT)
    ax.grid(axis="y", color="#e6e6e6")

    mom = data["mom_growth_pct"]
    colors = [BLUE if v >= 0 else ORANGE for v in mom.fillna(0)]
    ax2.bar(x, mom.fillna(0), color=colors, width=0.7)
    ax2.axhline(0, color=GREY, linewidth=1)
    for i, (v, loaded) in enumerate(zip(mom, ~gap)):
        if loaded and pd.isna(v):                        # month exists but has no fair comparison
            ax2.text(i, 2, "n/a", ha="center", fontsize=7, color=INK_SOFT)
    ax2.set_ylabel("vs previous\nmonth (%)", color=INK_SOFT)
    ax2.grid(axis="y", color="#e6e6e6")
    ax2.set_xticks(list(x)[::2])
    ax2.set_xticklabels([p.strftime("%b\n%Y") for p in full][::2])

    first, last = monthly.iloc[0], monthly.iloc[-1]
    change = (f"{last.mom_growth_pct:+.1f}% vs previous month" if pd.notna(last.mom_growth_pct)
              else "no MoM (previous month not loaded)")
    ax.set_title(f"Monthly revenue grew from {money_short(first.revenue)} ({month_name(first.purchase_month)}) "
                 f"to {money_short(last.revenue)} ({month_name(last.purchase_month)}); latest: {change}",
                 loc="left", fontsize=11, color=INK, fontweight="bold")
    for a in (ax, ax2):
        _style(a)
    return _save(fig, "01_revenue_trend.png")


def chart_top_categories(category: pd.DataFrame) -> pathlib.Path:
    """Horizontal bars, biggest on top: long category names stay readable, ranking is obvious."""
    top = category[category["category"] != "unknown"].head(10).iloc[::-1]   # reversed: #1 at the top
    fig, ax = plt.subplots(figsize=(10, 5.5))
    ax.barh([pretty_category(c) for c in top["category"]], top["revenue"], color=BLUE, height=0.65)
    for y, (rev, share) in enumerate(zip(top["revenue"], top["pct_of_total"])):
        ax.text(rev, y, f"  {money_short(rev)}  ({pct(share)}%)", va="center", fontsize=8.5, color=INK)
    ax.xaxis.set_major_formatter(FuncFormatter(money_short))
    ax.set_xlim(0, top["revenue"].max() * 1.25)          # room for the labels
    ax.grid(axis="x", color="#e6e6e6")
    top3 = top.iloc[::-1].head(3)
    ax.set_title(f"The top 3 categories bring in {pct(top3['pct_of_total'].sum(), 0)}% of revenue; "
                 f"{pretty_category(top3.iloc[0].category)} leads",
                 loc="left", fontsize=11, color=INK, fontweight="bold")
    ax.set_xlabel("Revenue (product price, excluding shipping) and share of total", color=INK_SOFT)
    _style(ax)
    return _save(fig, "02_top_categories.png")


def chart_late_by_state(delivery: pd.DataFrame) -> pathlib.Path:
    """
    Every state, sorted worst first, with the national rate as a dashed line. Orange = worse
    than the national rate, grey = better ("highlight what matters, grey the rest").
    The number of deliveries sits in each label, so a 31-order state isn't over-read.
    """
    national = delivery.loc[delivery["state"] == "ALL", "late_rate_pct"].iloc[0]
    states = delivery[delivery["state"] != "ALL"].sort_values("late_rate_pct")   # worst ends on top
    fig, ax = plt.subplots(figsize=(10, 8))
    colors = [ORANGE if r > national else GREY for r in states["late_rate_pct"]]
    labels = [f"{s}  ({n:,})" for s, n in zip(states["state"], states["delivered_orders"])]
    ax.barh(labels, states["late_rate_pct"], color=colors, height=0.7)
    for y, r in enumerate(states["late_rate_pct"]):
        ax.text(r, y, f" {pct(r)}%", va="center", fontsize=8, color=INK)
    ax.axvline(national, color=INK_SOFT, linestyle="--", linewidth=1)
    ax.text(national, len(states) - 0.3, f" national {pct(national)}%", fontsize=8.5, color=INK_SOFT)
    ax.set_xlim(0, states["late_rate_pct"].max() * 1.15)
    ax.tick_params(axis="y", labelsize=8)
    ax.grid(axis="x", color="#e6e6e6")
    ax.set_xlabel("% of delivered orders that arrived late   (state, number of deliveries)", color=INK_SOFT)
    worst = states[states["delivered_orders"] >= MIN_DELIVERIES_FOR_HEADLINE].iloc[-1]
    above = int((states["late_rate_pct"] > national).sum())
    ax.set_title(f"{worst.state} has the highest late-delivery rate ({pct(worst.late_rate_pct)}%); "
                 f"{above} of {len(states)} states are worse than the national {pct(national)}%",
                 loc="left", fontsize=11, color=INK, fontweight="bold")
    _style(ax)
    return _save(fig, "03_late_rate_by_state.png")


def chart_review_late_vs_on_time(delivery: pd.DataFrame) -> pathlib.Path:
    """Two bars on the full 0–5 star scale (a cut axis would exaggerate the gap)."""
    t = delivery[delivery["state"] == "ALL"].iloc[0]
    on_time_n = t.delivered_orders - t.late_orders
    fig, ax = plt.subplots(figsize=(7, 5))
    labels = [f"On time\n({on_time_n:,} orders)", f"Late\n({t.late_orders:,} orders)"]
    values = [t.avg_review_on_time, t.avg_review_late]
    ax.bar(labels, values, color=[BLUE, ORANGE], width=0.5)
    for i, v in enumerate(values):
        ax.text(i, v + 0.08, f"{v:.2f}", ha="center", fontsize=12, color=INK, fontweight="bold")
    ax.set_ylim(0, 5)
    ax.set_ylabel("Average review score (1–5 stars)", color=INK_SOFT)
    ax.grid(axis="y", color="#e6e6e6")
    ax.set_title(f"Late orders score {t.review_drop_when_late:.2f} stars lower "
                 f"({t.avg_review_late:.2f} vs {t.avg_review_on_time:.2f} out of 5)",
                 loc="left", fontsize=11, color=INK, fontweight="bold")
    _style(ax)
    return _save(fig, "04_review_late_vs_on_time.png")


# ── 4. workbook formatting ────────────────────────────────────────────────────

def friendly_header(col: str) -> str:
    """'late_rate_pct' → 'Late rate %', 'revenue' → 'Revenue (R$)' (labels for a manager)."""
    text = col.replace("_pct", " %").replace("_", " ").replace("mom", "MoM").strip()
    text = text[0].upper() + text[1:]
    if is_money(col):
        text += " (R$)"
    return text


def is_money(col: str) -> bool:
    """Columns holding reais. Excel equivalent: Format Cells → Currency."""
    return ("revenue" in col or col.endswith("_value")) and "pct" not in col


def number_format(col: str, s: pd.Series) -> str | None:
    """
    Excel number format per column. % columns hold 7.54 (already ×100 in SQL), so they get a
    literal "%" sign ('0.00"%"'), NOT Excel's % format, which would multiply by 100 → 754%.
    """
    if is_money(col):
        return '"R$" #,##0.00'
    if "pct" in col:
        return '0.00"%"'
    if "review" in col:
        return "0.00"
    if pd.api.types.is_float_dtype(s):
        return "#,##0.0"
    if pd.api.types.is_integer_dtype(s):
        return "#,##0"
    return None


def format_table(ws, df: pd.DataFrame, header_row: int) -> None:
    """
    Header style, number formats, column widths, frozen header, filter buttons.
    Excel equivalents: Format Cells, double-click the column border (AutoFit),
    View → Freeze Panes, Data → Filter.
    """
    first_data = header_row + 1
    last_row = header_row + len(df)
    for j, col in enumerate(df.columns, start=1):
        cell = ws.cell(row=header_row, column=j, value=friendly_header(col))
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        fmt = number_format(col, df[col])
        if fmt:
            for row in ws.iter_rows(min_row=first_data, max_row=last_row, min_col=j, max_col=j):
                row[0].number_format = fmt
        # width ≈ longest text in the column (header or a sample of values), capped
        # (pandas 3.0: astype(str) keeps NaN as NaN, not 'nan', so map missing values to '' first)
        sample = df[col].head(200).map(lambda v: "" if pd.isna(v) else str(v))
        longest = max([len(friendly_header(col)) * 0.9] + [len(v) + 3 for v in sample])
        ws.column_dimensions[get_column_letter(j)].width = min(max(longest, 9), 45)
    ws.row_dimensions[header_row].height = 30
    ws.freeze_panes = ws.cell(row=first_data, column=1)
    ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(df.columns))}{max(last_row, first_data)}"


def write_kpi_sheet(writer, view: str, df: pd.DataFrame) -> None:
    """One KPI view → one formatted sheet: description in row 1, table from row 3."""
    sheet, description = KPI_VIEWS[view]
    if view == "vw_kpi_cohort_retention":
        df = cohort_triangle(df)
    df.to_excel(writer, sheet_name=sheet, index=False, startrow=2)
    ws = writer.sheets[sheet]
    ws["A1"] = description
    ws["A1"].font = Font(italic=True, color="444444")
    ws["A2"] = f"Source: {view} (SQL view)"
    ws["A2"].font = Font(size=8, color="888888")
    format_table(ws, df, header_row=3)
    log.info("sheet %-24s %6d rows", sheet, len(df))


def cohort_triangle(df: pd.DataFrame) -> pd.DataFrame:
    """
    Long format (one row per cohort × month) → the classic triangle: rows = first-purchase
    month, columns = months later. Only a reshape; every % comes from the view.
    Excel: PivotTable (rows = cohort, columns = months_since_first, values = retention).
    Missing cells stay blank (NaN): "not observed", never 0%.
    """
    tri = df.pivot_table(index="cohort_month", columns="months_since_first",
                         values="retention_pct", aggfunc="first")
    tri.columns = [f"m{c}_pct" for c in tri.columns]           # 'm1_pct' → header 'M1 %'
    sizes = df.groupby("cohort_month")["cohort_customers"].first()
    return tri.join(sizes).reset_index().loc[:, ["cohort_month", "cohort_customers", *tri.columns]]


def write_summary(wb, sentences: list[str], scope: str, status: str, note: str,
                  revenue_note: str, run_date: datetime.date) -> None:
    """Summary sheet: title, scope, numbered headline sentences, notes. Text, not tables."""
    ws = wb.create_sheet("Summary", 0)
    ws.column_dimensions["A"].width = 4
    ws.column_dimensions["B"].width = 125
    ws["B1"] = "Monthly Operations Report"
    ws["B1"].font = Font(size=16, bold=True, color="1F4E78")
    ws["B2"] = f"Generated automatically on {run_date:%d %b %Y} from the order database."
    ws["B2"].font = Font(italic=True, color="666666")
    ws["B3"] = scope
    ws["B5"] = "Headlines"
    ws["B5"].font = Font(size=12, bold=True)
    row = 6
    for i, s in enumerate(sentences, start=1):
        ws.cell(row=row, column=1, value=f"{i}.").alignment = Alignment(vertical="top")
        c = ws.cell(row=row, column=2, value=s)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[row].height = 32
        row += 1
    # data status: green if checks passed, red if an ERROR check failed (text says it too)
    c = ws.cell(row=row + 1, column=2, value=status)
    c.fill, c.font = FILL["error"] if status.startswith("WARNING") else FILL["ok"]
    c.alignment = Alignment(wrap_text=True, vertical="top")
    row += 3
    for text in (note, revenue_note,
                 "Charts: see the 'Charts' sheet. Definitions of every term: 'Definitions'. "
                 "Data checks: 'Data Quality'. The other sheets hold the detail behind each number."):
        c = ws.cell(row=row, column=2, value=text)
        c.font = Font(color="444444", size=9)
        c.alignment = Alignment(wrap_text=True)
        row += 1


def write_charts(wb, chart_paths: list[pathlib.Path]) -> None:
    """Embed the PNGs one under another (scroll down to read, like a printed page)."""
    ws = wb.create_sheet("Charts", 1)
    row = 1
    for path in chart_paths:
        img = XLImage(str(path))
        scale = 800 / img.width                    # 150-dpi PNG shown at ~800 px wide
        img.width, img.height = int(img.width * scale), int(img.height * scale)
        ws.add_image(img, f"A{row}")
        row += int(img.height / 20) + 2            # ≈ 20 px per default row, + a spacer


def definitions_rows(monthly: pd.DataFrame) -> list[tuple[str, str]]:
    """
    The DEFINITIONS block of sql/kpis.sql in plain English. kpis.sql is the source of truth:
    if a definition changes there, change it here too. Numbers (scope, the freight %) are
    generated from the data, so they never go stale after a new month is loaded.
    """
    freight_pct = 100 * (monthly["revenue_incl_freight"].sum() / monthly["revenue"].sum() - 1)
    gap = missing_months(monthly)
    gap_txt = (f" {', '.join(month_name(m) for m in gap)} are not in the loaded data, so the month "
               f"after a gap has no month-over-month figure." if gap else "")
    return [
        ("Revenue", f"The price of the products sold, excluding shipping (freight). Shipping is passed "
                    f"on to the delivery company, so it isn't the marketplace's sales. Including shipping "
                    f"would make revenue {pct(freight_pct)}% higher. Amounts in Brazilian reais (R$)."),
        ("Valid order", "Every order except canceled and unavailable ones (those sales never happened). "
                        "Orders still being processed or shipped count, because they are booked sales."),
        ("Month", "The month the customer placed the order (not when it was delivered)."),
        ("Late delivery", "Delivered on a later calendar day than the date promised to the customer. "
                          "Arriving on the promised day, at any time, is on time. Only orders that were "
                          "actually delivered and have trustworthy dates are counted."),
        ("Customer", "A real person. The system gives a new customer number to every order, so we count "
                     "people instead; otherwise nobody would ever look like a repeat customer."),
        ("Repeat customer", "A person with 2 or more valid orders in the period (two orders placed at the "
                            "same checkout also count)."),
        ("Month-over-month (MoM)", "Change in revenue vs the previous calendar month, in %. Left blank "
                                   "when the previous month isn't loaded, instead of comparing with an "
                                   "older month."),
        ("Scope", f"Orders placed from {month_name(monthly['purchase_month'].iloc[0])} to "
                  f"{month_name(monthly['purchase_month'].iloc[-1])} ({len(monthly)} months loaded).{gap_txt} "
                  f"New months appear automatically when they are loaded."),
        ("Data check: ERROR", "A problem in the pipeline itself (e.g. a duplicate or broken link between "
                              "tables). If any ERROR check fails, the report should not be trusted."),
        ("Data check: WARNING", "A known issue in the source data that we keep and track on purpose "
                                "(e.g. an order with a missing delivery date). The numbers are still usable."),
    ]


def write_definitions(wb, monthly: pd.DataFrame) -> None:
    """Definitions sheet: Term | What it means. Every number in the report depends on these."""
    ws = wb.create_sheet("Definitions")
    ws["A1"], ws["B1"] = "Term", "What it means"
    for c in (ws["A1"], ws["B1"]):
        c.fill, c.font = HEADER_FILL, HEADER_FONT
    for i, (term, text) in enumerate(definitions_rows(monthly), start=2):
        ws.cell(row=i, column=1, value=term).font = Font(bold=True)
        cell = ws.cell(row=i, column=2, value=text)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[i].height = 45
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 110


def write_data_quality(writer, dq: pd.DataFrame, run_id: str | None) -> None:
    """
    Latest DQ run: check, severity, failing rows, change vs the previous run, and a status
    in words (colour + text, so it's readable in black-and-white print and by colour-blind readers).
    Excel equivalent: Conditional Formatting → "Light red fill with dark red text".
    """
    sheet = "Data Quality"
    if dq.empty:
        pd.DataFrame({"Status": ["No data-quality run found. Run src/checks.py before the report."]}) \
          .to_excel(writer, sheet_name=sheet, index=False)
        log.warning("no dq_results found: Data Quality sheet says so")
        return
    out = dq.copy()
    out["status"] = [
        "OK" if n == 0 else
        "ERROR: pipeline problem, numbers not reliable" if sev == "ERROR" else
        "WARNING: known data issue, kept and tracked"
        for sev, n in zip(out["severity"], out["failed_rows"])
    ]
    out.to_excel(writer, sheet_name=sheet, index=False, startrow=2)
    ws = writer.sheets[sheet]
    errors = int(((out.severity == "ERROR") & (out.failed_rows > 0)).sum())
    warns = int(((out.severity == "WARNING") & (out.failed_rows > 0)).sum())
    ws["A1"] = (f"Data-quality run {run_id}: {len(out)} checks, {errors} ERROR failures, "
                f"{warns} WARNINGs. ERROR = pipeline broken (report not reliable). "
                f"WARNING = real-data issue kept on purpose.")
    ws["A1"].font = Font(bold=True, color="9C0006" if errors else "1F4E78")
    format_table(ws, out, header_row=3)
    status_col = out.columns.get_loc("status") + 1
    for i, (sev, n) in enumerate(zip(out["severity"], out["failed_rows"]), start=4):
        key = "ok" if n == 0 else "error" if sev == "ERROR" else "warn"
        for j in (2, status_col):                       # severity cell + status cell
            ws.cell(row=i, column=j).fill, ws.cell(row=i, column=j).font = FILL[key]
    ws.column_dimensions[get_column_letter(status_col)].width = 46
    log.info("sheet %-24s %6d checks (%d ERROR failures, %d WARNINGs)", sheet, len(out), errors, warns)


# ── 5. build the report ───────────────────────────────────────────────────────

def build_report(conn: sqlite3.Connection | None = None,
                 run_date: datetime.date | None = None,
                 out_dir: pathlib.Path = REPORTS_DIR) -> pathlib.Path:
    """
    Build reports/ops_report_YYYY-MM-DD.xlsx and return its path (the orchestrator uses it).
    Raises ReportError if there's no data or a KPI view fails. Running twice on the same day
    overwrites that day's file (idempotent: same data → same report).
    """
    t0 = time.perf_counter()
    run_date = run_date or datetime.date.today()
    own_conn = conn is None
    conn = conn or db.get_connection()
    try:
        kpis = load_kpis(conn)
        dq, run_id = load_dq(conn)
    finally:
        if own_conn:
            conn.close()

    monthly = kpis["vw_kpi_monthly_revenue"]
    sentences = headline_sentences(kpis)
    status = data_status_sentence(dq)
    charts = [chart_revenue(monthly),
              chart_top_categories(kpis["vw_kpi_category_share"]),
              chart_late_by_state(kpis["vw_kpi_delivery_by_state"]),
              chart_review_late_vs_on_time(kpis["vw_kpi_delivery_by_state"])]
    log.info("drew %d charts -> %s", len(charts), CHARTS_DIR.relative_to(ROOT))

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"ops_report_{run_date:%Y-%m-%d}.xlsx"
    revenue_note = definitions_rows(monthly)[0][1]
    # ExcelWriter = pandas writes the tables; writer.book is the openpyxl workbook for the rest
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for view, df in kpis.items():
            write_kpi_sheet(writer, view, df)
        wb = writer.book
        write_summary(wb, sentences, scope_sentence(monthly), status, gap_note(monthly),
                      "Revenue = " + revenue_note, run_date)
        write_charts(wb, charts)
        write_definitions(wb, monthly)
        write_data_quality(writer, dq, run_id)
        wb.active = 0                                   # open on the Summary sheet

    log.info("wrote %s (%d sheets, %.0f KB) in %.1f s", path.relative_to(ROOT), len(wb.sheetnames),
             path.stat().st_size / 1024, time.perf_counter() - t0)
    for s in [scope_sentence(monthly), *sentences, status, gap_note(monthly)]:
        log.info("summary: %s", s.encode("ascii", "replace").decode())   # ASCII for the Windows console
    return path


# ── entry point ───────────────────────────────────────────────────────────────

def setup_logging() -> None:
    """Console + logs/report.log (appended), the same pattern as load.py and checks.py."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )


def main() -> int:
    """Build today's report; exit code 0 on success, 1 if it couldn't be built."""
    setup_logging()
    try:
        build_report()
    except (ReportError, sqlite3.Error) as exc:       # expected failures: one clear line
        log.error("report NOT built: %s", exc)
        return 1
    except Exception:                                  # a bug: full traceback INTO the log file,
        log.exception("report NOT built: unexpected error")   # since a scheduled run has no console
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
