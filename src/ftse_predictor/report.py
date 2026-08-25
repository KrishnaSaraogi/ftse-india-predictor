"""
The permanent workbook, rewritten to APPEND and UPDATE rather than
regenerate. rebalancing_predictions.xlsx is a single, ever-growing
file seeded with real historical data - every run adds new rows in the
exact same schema, and when real N-PORT data lands for a quarter that
was predicted, the matching rows get their outcome columns filled in.

SCHEMA (must match exactly - this is READING an existing file with a
fixed column layout, not designing a new one):
    Summary:         Review Date, Quarter Type, Adds Predicted,
                     Adds Actual, Adds Caught, Add Hit Rate %,
                     Removes Predicted, Removes Actual, Removes Caught
    Adds Detail:     Review Date, Quarter Type, Symbol, Company,
                     Predicted, Actually Added, Outcome, Rank,
                     Cum. Coverage %, Months Listed, Market Cap (Cr),
                     Confidence, Confidence Depth %
    Removes Detail:  Review Date, Quarter Type, Symbol, Company,
                     Predicted, Actually Removed, Outcome, Rank,
                     Cum. Coverage %, Market Cap (Cr), Confidence,
                     Confidence Depth %
"""

import os

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .config import CONFIG
from .predict import PredictionResult

FONT_NAME = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(name=FONT_NAME, bold=True, color="FFFFFF")
WARNING_FILL = PatternFill("solid", fgColor="FF0000")
WARNING_FONT = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=12)
CONF_FILLS = {"High": PatternFill("solid", fgColor="C6EFCE"),
              "Elevated": PatternFill("solid", fgColor="FFEB9C"),
              "Watch": PatternFill("solid", fgColor="F2F2F2")}
OUTCOME_FILLS = {"Caught": PatternFill("solid", fgColor="C6EFCE"),
                 "Missed": PatternFill("solid", fgColor="FFC7CE"),
                 "False Positive": PatternFill("solid", fgColor="FFEB9C")}


def _add_confidence(row, quarter_type):
    if quarter_type == "general_review":
        depth = (CONFIG.entry_coverage - row["cum_coverage"]) / CONFIG.entry_coverage * 100
    else:
        mcap_cr = row["full_mcap"] / 1e7
        depth = min((mcap_cr / CONFIG.fast_entry_min_mcap_cr - 1) * 100, 100)
    tier = "High" if depth >= 60 else "Elevated" if depth >= 25 else "Watch"
    return tier, round(depth, 1)


def _remove_confidence(cum_coverage):
    depth = (cum_coverage - CONFIG.exit_coverage) / (100 - CONFIG.exit_coverage) * 100
    tier = "High" if depth >= 60 else "Elevated" if depth >= 25 else "Watch"
    return tier, round(depth, 1)


_QUARTER_TYPE_LABEL = {
    "general_review": "Mar/Sep (general review)",
    "fast_entry": "Jun/Dec (fast entry)",
}


def append_prediction(workbook_path, result):
    """Adds this review's predictions as new rows. Marks them Predicted=True,
    Outcome=pending (blank) - update_outcomes() fills those in later when
    real data arrives. Never overwrites existing rows."""
    label = _QUARTER_TYPE_LABEL[result.quarter_type]

    if not os.path.exists(workbook_path):
        raise FileNotFoundError(
            f"{workbook_path} not found. This workbook should have been "
            f"seeded with real historical data before automation started - "
            f"see README. Refusing to silently create a blank one, since "
            f"that would lose the existing backtest history.")

    wb = load_workbook(workbook_path)

    adds = result.adds.copy()
    new_add_rows = []
    for _, r in adds.iterrows():
        tier, depth = _add_confidence(r, result.quarter_type)
        new_add_rows.append([
            result.review_date, label, r["symbol"], r["name"],
            True, None, None, int(r["rank"]), round(r["cum_coverage"], 1),
            round(r["months_listed"], 1) if pd.notna(r.get("months_listed")) else None,
            round(r["full_mcap"] / 1e7, 0), tier, depth])
    for row in new_add_rows:
        wb["Adds Detail"].append(row)

    removes = result.removes.copy()
    new_rem_rows = []
    for _, r in removes.iterrows():
        tier, depth = _remove_confidence(r["cum_coverage"])
        new_rem_rows.append([
            result.review_date, label, r["symbol"], r["name"],
            True, None, None, int(r["rank"]), round(r["cum_coverage"], 1),
            round(r["full_mcap"] / 1e7, 0), tier, depth])
    for row in new_rem_rows:
        wb["Removes Detail"].append(row)

    summary_ws = wb["Summary"]
    _delete_rows_matching(summary_ws, col_index=1, value=result.review_date)
    summary_ws.append([result.review_date, label, len(adds), None, None, None,
                       len(removes), None, None])

    wb.save(workbook_path)


def update_outcomes(workbook_path, review_date, raw_add_isins, raw_remove_isins,
                    isin_lookup):
    """Called once real N-PORT data is available for a review that was
    predicted. Fills in Actually Added/Removed + Outcome on the matching
    rows, and completes the Summary row. isin_lookup maps symbol -> isin
    for the predicted rows, since Adds/Removes Detail is keyed by symbol
    not isin."""
    wb = load_workbook(workbook_path)
    review_str = str(pd.Timestamp(review_date).date())

    stats = {"adds_caught": 0, "adds_missed": 0, "adds_false_positive": 0,
            "removes_caught": 0}

    for sheet_name, raw_isins, actual_col_name in [
            ("Adds Detail", raw_add_isins, "Actually Added"),
            ("Removes Detail", raw_remove_isins, "Actually Removed")]:
        ws = wb[sheet_name]
        headers = [c.value for c in ws[1]]
        date_i = headers.index("Review Date")
        sym_i = headers.index("Symbol")
        actual_i = headers.index(actual_col_name)
        outcome_i = headers.index("Outcome")

        predicted_isins_this_date = set()
        for row in ws.iter_rows(min_row=2):
            if row[date_i].value is None:
                continue
            row_date = str(pd.Timestamp(row[date_i].value).date())
            if row_date != review_str:
                continue
            symbol = row[sym_i].value
            isin = isin_lookup.get(symbol)
            if isin:
                predicted_isins_this_date.add(isin)
            was_actual = isin in raw_isins if isin else False
            row[actual_i].value = was_actual
            outcome = "Caught" if was_actual else "False Positive"
            row[outcome_i].value = outcome
            if sheet_name == "Adds Detail":
                stats["adds_caught" if was_actual else "adds_false_positive"] += 1
            else:
                if was_actual:
                    stats["removes_caught"] += 1

        missed_isins = raw_isins - predicted_isins_this_date
        for isin in missed_isins:
            new_row = [None] * len(headers)
            new_row[date_i] = pd.Timestamp(review_date)
            new_row[headers.index("Predicted")] = False
            new_row[actual_i] = True
            new_row[outcome_i] = "Missed"
            ws.append(new_row)
            if sheet_name == "Adds Detail":
                stats["adds_missed"] += 1

    summary_ws = wb["Summary"]
    headers = [c.value for c in summary_ws[1]]
    date_i = headers.index("Review Date")
    for row in summary_ws.iter_rows(min_row=2):
        if row[date_i].value and str(pd.Timestamp(row[date_i].value).date()) == review_str:
            row[headers.index("Adds Actual")].value = \
                stats["adds_caught"] + stats["adds_missed"]
            row[headers.index("Adds Caught")].value = stats["adds_caught"]
            predicted = row[headers.index("Adds Predicted")].value or 0
            row[headers.index("Add Hit Rate %")].value = \
                round(stats["adds_caught"] / predicted * 100, 1) if predicted else None
            row[headers.index("Removes Actual")].value = len(raw_remove_isins)
            row[headers.index("Removes Caught")].value = stats["removes_caught"]
            break

    wb.save(workbook_path)
    return stats


def write_health_warning(workbook_path, health_log):
    """If any data fell back to last-known-good this run, write an
    unmissable warning at the top of the Summary sheet - designed to be
    obvious to someone with no technical background: big, red, plain
    language, no jargon."""
    if not health_log.has_issues:
        return
    wb = load_workbook(workbook_path)
    ws = wb["Summary"]
    ws.insert_rows(1, amount=2)
    ws["A1"] = ("WARNING: some data could not be refreshed this run and "
               "OLDER data was used instead. This prediction may be less "
               "reliable than usual. Details below.")
    ws["A1"].fill = WARNING_FILL
    ws["A1"].font = WARNING_FONT
    ws.merge_cells("A1:I1")
    ws["A2"] = " | ".join(health_log.summary_lines())
    ws["A2"].font = Font(name=FONT_NAME, italic=True, color="CC0000")
    ws.merge_cells("A2:I2")
    wb.save(workbook_path)


def _delete_rows_matching(ws, col_index, value):
    value_str = str(pd.Timestamp(value).date())
    to_delete = []
    for row in ws.iter_rows(min_row=2):
        cell = row[col_index - 1]
        if cell.value and str(pd.Timestamp(cell.value).date()) == value_str:
            to_delete.append(cell.row)
    for r in reversed(to_delete):
        ws.delete_rows(r)


def apply_formatting(workbook_path):
    """Reapplies consistent formatting after appends - openpyxl does not
    preserve conditional formatting across programmatic row inserts, so
    this is re-run at the end of every update."""
    wb = load_workbook(workbook_path)
    for name in ("Summary", "Adds Detail", "Removes Detail"):
        ws = wb[name]
        header_row = 1
        for r in range(1, 4):
            if ws.cell(row=r, column=1).value in ("Review Date",):
                header_row = r
                break
        for cell in ws[header_row]:
            if cell.value:
                cell.fill = HEADER_FILL
                cell.font = HEADER_FONT
                cell.alignment = Alignment(horizontal="center", wrap_text=True)
        ws.freeze_panes = f"A{header_row + 1}"

        headers = [c.value for c in ws[header_row]]
        oi = headers.index("Outcome") + 1 if "Outcome" in headers else None
        ci = headers.index("Confidence") + 1 if "Confidence" in headers else None
        for row in ws.iter_rows(min_row=header_row + 1):
            for cell in row:
                cell.font = Font(name=FONT_NAME)
            if oi and row[oi - 1].value in OUTCOME_FILLS:
                row[oi - 1].fill = OUTCOME_FILLS[row[oi - 1].value]
            if ci and row[ci - 1].value in CONF_FILLS:
                row[ci - 1].fill = CONF_FILLS[row[ci - 1].value]

        for col in ws.columns:
            vals = [c.value for c in col if c.value is not None]
            if not vals:
                continue
            ln = max(len(str(v)) for v in vals)
            ws.column_dimensions[get_column_letter(col[0].column)].width = \
                min(max(ln + 2, 11), 50)
    wb.save(workbook_path)
