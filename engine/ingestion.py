"""Stage 1 - Data in: load and validate every input before anything is calculated.

Design rules
* Fail loudly: a wrong period, a stale version or duplicate keys stops the run with a clear message.
* Google Sheets safe: Sheets can silently convert '2026-06' into a date. Period values are read as
  text and any date-like value is normalised back to 'YYYY-MM' with a warning.
* Tables owned by people (mappings, driver notes, revision reasons) can come from CSV files or from
  a workbook exported from Google Sheets (tab names in SHEETS_TABS).
"""
import hashlib
from pathlib import Path

import pandas as pd

from engine.config import CORPORATE_BU

# Tab name in a Google Sheets / Excel workbook  ->  logical table name
SHEETS_TABS = {
    "Mapping_Statutory": "coa_statutory",
    "Mapping_Management": "coa_management",
    "Reclass_Rules": "mgmt_reclass_rules",
    "Driver_Notes": "driver_notes",
    "Revision_Reasons": "revision_reasons",
    "KPI_Targets": "kpi_targets",
}


class IngestionError(Exception):
    """Raised when an input file is missing, stale or malformed. The pipeline stops."""


def normalise_period(value, warnings: list) -> str:
    """Return a 'YYYY-MM' text period, repairing values that a spreadsheet turned into dates.

    Example: Timestamp('2026-06-01') or '2026-06-01 00:00:00' -> '2026-06' (with a warning).
    """
    text = str(value).strip()
    if len(text) == 7 and text[4] == "-" and text[:4].isdigit() and text[5:].isdigit():
        return text
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        raise IngestionError(f"Period value '{value}' is not a valid YYYY-MM month.")
    fixed = f"{parsed.year:04d}-{parsed.month:02d}"
    warnings.append(f"Period '{text}' looked like a date (spreadsheet auto-conversion); read as {fixed}.")
    return fixed


def require_columns(df: pd.DataFrame, columns, name: str) -> None:
    """Stop with a clear message if a required column is missing."""
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise IngestionError(f"{name}: missing required column(s) {missing}. Found {list(df.columns)}.")


def _read_excel(path) -> pd.DataFrame:
    """Read the first sheet of an Excel file with all key columns as text."""
    path = Path(path)
    if not path.exists():
        raise IngestionError(f"Input file not found: {path}. Run 'python data/generate_mock_data.py' first.")
    return pd.read_excel(path, dtype={"Period": str, "ERP_Account_Code": str, "BU": str,
                                      "TB_Version": str, "Forecast_Version": str})


def _clean_keys(df: pd.DataFrame, warnings: list) -> pd.DataFrame:
    """Trim whitespace on key columns and repair date-converted periods."""
    df = df.copy()
    if "Period" in df.columns:
        df["Period"] = df["Period"].map(lambda v: normalise_period(v, warnings))
    for col in ("ERP_Account_Code", "BU"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()
    return df


def _check_amount(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """Coerce Amount to numbers; stop if any cell is blank or text."""
    df = df.copy()
    df["Amount"] = pd.to_numeric(df["Amount"], errors="coerce")
    if df["Amount"].isna().any():
        raise IngestionError(f"{name}: {int(df['Amount'].isna().sum())} Amount value(s) are blank or not numeric.")
    return df


def load_column_map(path) -> dict:
    """One-time map from the ERP's own column headers to internal names (config/erp_column_map.csv)."""
    table = read_table(path, ["ERP_Header", "Internal_Name"])
    return dict(zip(table["ERP_Header"], table["Internal_Name"]))


def load_trial_balance(path, period: str, column_map: dict, warnings: list) -> pd.DataFrame:
    """Load a raw ERP trial balance export. No Period/Version columns are needed.

    * ERP headers are renamed through the column map.
    * If the file happens to carry a Period column it must match the run (stale-file guard).
    * Amounts may contain thousands separators; blanks or text stop the run.
    """
    path = Path(path)
    if not path.exists():
        raise IngestionError(f"Trial balance file not found: {path}. Run 'python data/generate_mock_data.py' first.")
    df = pd.read_excel(path, dtype=str).rename(columns=column_map)
    require_columns(df, ["ERP_Account_Code", "ERP_Description", "BU", "Amount"], "Trial balance export")
    df["Amount"] = pd.to_numeric(df["Amount"].astype(str).str.replace(",", "", regex=False), errors="coerce")
    if df["Amount"].isna().any():
        raise IngestionError(f"Trial balance export: {int(df['Amount'].isna().sum())} Amount value(s) are blank or not numeric.")
    for col in ("ERP_Account_Code", "BU", "ERP_Description"):
        df[col] = df[col].astype(str).str.strip()
    if "Period" in df.columns:
        df["Period"] = df["Period"].map(lambda v: normalise_period(v, warnings))
        if (df["Period"] != period).any():
            raise IngestionError(f"Trial balance contains periods {sorted(df['Period'].unique())}; expected only {period}. This looks like a stale file.")
    else:
        df["Period"] = period
    if df.duplicated(["ERP_Account_Code", "BU"]).any():
        raise IngestionError("Trial balance export has duplicate Account/BU rows; each must appear once.")
    df["Memo"] = df["Memo"].fillna("").astype(str) if "Memo" in df.columns else ""
    return df[["Period", "ERP_Account_Code", "ERP_Description", "BU", "Amount", "Memo"]]


def check_not_last_month(tb: pd.DataFrame, history: pd.DataFrame, period: str) -> None:
    """Stop if the P&L in the export is identical to the previous locked month (last month's file by mistake)."""
    from engine.variance_ratios import period_add
    prev_period = period_add(period, -1)
    prev = history[(history["Period"] == prev_period) & history["ERP_Account_Code"].str[0].isin(["4", "5", "6"])]
    cur = tb[tb["ERP_Account_Code"].str[0].isin(["4", "5", "6"])]
    if prev.empty or cur.empty:
        return
    merged = cur.merge(prev, on=["ERP_Account_Code", "BU"], suffixes=("", "_prev"))
    if len(merged) == len(cur) and (merged["Amount"] - merged["Amount_prev"]).abs().max() < 0.005:
        raise IngestionError(f"The P&L in this export is identical to {prev_period}: it looks like last month's file.")


def load_scenario(path, scenario: str, warnings: list) -> pd.DataFrame:
    """Load the Budget or Rolling Forecast workbook (long format: one row per month/account/BU).

    For forecasts with several Forecast_Version values, only the latest version is used.
    """
    df = _read_excel(path)
    require_columns(df, ["Period", "ERP_Account_Code", "BU", "Amount"], f"{scenario} file")
    df = _check_amount(_clean_keys(df, warnings), f"{scenario} file")
    if scenario == "Forecast" and "Forecast_Version" in df.columns:
        versions = sorted(df["Forecast_Version"].astype(str).unique())
        if len(versions) > 1:
            warnings.append(f"Forecast file holds versions {versions}; using the latest ({versions[-1]}).")
        df = df[df["Forecast_Version"].astype(str) == versions[-1]]
    if df.duplicated(["Period", "ERP_Account_Code", "BU"]).any():
        raise IngestionError(f"{scenario} file has duplicate Period/Account/BU rows.")
    df = df.assign(Scenario=scenario)
    return df[["Scenario", "Period", "ERP_Account_Code", "BU", "Amount"]]


def load_history(path, reporting_period: str, warnings: list) -> pd.DataFrame:
    """Load locked actuals history (months already closed and signed off).

    Safety rule: history must contain only months BEFORE the reporting period. If it already
    holds the reporting month, the month is locked and re-running would double count it.
    """
    path = Path(path)
    if not path.exists():
        raise IngestionError(f"History file not found: {path}. Run 'python data/generate_mock_data.py' first.")
    df = pd.read_csv(path, dtype={"Period": str, "ERP_Account_Code": str, "BU": str})
    require_columns(df, ["Period", "ERP_Account_Code", "BU", "Amount"], "Actuals history")
    df = _clean_keys(df, warnings)
    if (df["Period"] >= reporting_period).any():
        raise IngestionError(f"History already contains {reporting_period} (or later): the month is locked. "
                             "Use history.restate_period for a corrected restatement, or report a later month.")
    months = sorted(df["Period"].unique())
    expected = pd.period_range(months[0], pd.Period(reporting_period, "M") - 1, freq="M").strftime("%Y-%m")
    gaps = sorted(set(expected) - set(months))
    if gaps:
        warnings.append(f"History is missing month(s) {gaps}; comparisons that need them will be skipped.")
    return df[["Period", "ERP_Account_Code", "BU", "Amount"]]


def read_table(path, required_columns) -> pd.DataFrame:
    """Read a small CSV table (mapping, rules, notes) with every column as text, then check columns."""
    path = Path(path)
    if not path.exists():
        raise IngestionError(f"Configuration file not found: {path}")
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    except pd.errors.ParserError as exc:
        raise IngestionError(f"{path.name} could not be read ({exc}). Check for a stray comma inside a cell: wrap text containing commas in quotes.") from exc
    require_columns(df, required_columns, path.name)
    return df


def load_sheets_workbook(path) -> dict:
    """Read the human-owned tables from a workbook exported from Google Sheets (or Excel).

    Returns {logical_table_name: DataFrame}. Every cell is read as text so that Sheets cannot
    alter codes or periods; numeric columns are converted by the modules that use them.
    """
    path = Path(path)
    if not path.exists():
        raise IngestionError(f"Sheets workbook not found: {path}")
    sheets = pd.read_excel(path, sheet_name=None, dtype=str)
    missing = [tab for tab in SHEETS_TABS if tab not in sheets]
    if missing:
        raise IngestionError(f"Sheets workbook is missing tab(s): {missing}. Expected {list(SHEETS_TABS)}.")
    return {SHEETS_TABS[tab]: sheets[tab].fillna("") for tab in SHEETS_TABS}


def file_sha256(path) -> str:
    """SHA-256 hash of a file, used for the input manifest (proves which file version a run used)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
