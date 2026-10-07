"""Roll-forward: locked actuals history.

Rules
* A closed month is APPENDED once and then read-only; it is never overwritten by the next run.
* Each month starts from a fresh trial balance file; nothing is copied forward from last month's file.
* Restating an already-closed month is allowed only with a logged reason and Finance Director
  approval, and every restated line is written to the restatement log.
"""
import os
import tempfile
from datetime import datetime

import pandas as pd

HISTORY_COLUMNS = ["Period", "ERP_Account_Code", "BU", "Amount", "Locked_Version", "Locked_On"]


class HistoryError(Exception):
    """Raised when a lock or restatement would break the history rules."""


def _read(path) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"Period": str, "ERP_Account_Code": str, "BU": str})


def _write_atomic(df: pd.DataFrame, path) -> None:
    """Write via a temporary file so a crash can never leave a half-written history."""
    folder = os.path.dirname(os.fspath(path)) or "."
    handle, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    os.close(handle)
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def locked_periods(path) -> set:
    """Set of months already locked in the history file."""
    return set(_read(path)["Period"])


def lock_period(path, tb: pd.DataFrame, period: str, version: str) -> int:
    """Append a signed-off month to history. Refuses if the month is already locked.

    Returns the number of rows appended.
    """
    history = _read(path)
    if period in set(history["Period"]):
        raise HistoryError(f"{period} is already locked. Use restate_period with a reason and Finance Director approval.")
    rows = tb[["Period", "ERP_Account_Code", "BU", "Amount"]].copy()
    rows["Locked_Version"] = version
    rows["Locked_On"] = datetime.now().isoformat(timespec="seconds")
    _write_atomic(pd.concat([history, rows], ignore_index=True)[HISTORY_COLUMNS], path)
    return len(rows)


def restate_period(path, log_path, corrected_tb: pd.DataFrame, period: str, reason: str,
                   approver_role: str, prepared_by: str) -> pd.DataFrame:
    """Replace a locked month with corrected figures (human-in-the-loop restatement).

    Requires a non-empty reason and approval by the Finance Director. Every changed line is
    appended to the restatement log. Returns the lines that changed.
    """
    if not reason.strip():
        raise HistoryError("A restatement needs a logged reason.")
    if approver_role != "Finance Director":
        raise HistoryError("A restatement must be approved by the Finance Director.")
    history = _read(path)
    old = history[history["Period"] == period]
    if old.empty:
        raise HistoryError(f"{period} is not in history; nothing to restate.")
    keys = ["Period", "ERP_Account_Code", "BU"]
    merged = old[keys + ["Amount"]].merge(corrected_tb[keys + ["Amount"]], on=keys, how="outer",
                                          suffixes=("_Before", "_After")).fillna({"Amount_Before": 0.0, "Amount_After": 0.0})
    merged["Change"] = merged["Amount_After"] - merged["Amount_Before"]
    changed = merged[merged["Change"].abs() > 0.005].copy()
    if changed.empty:
        raise HistoryError("The corrected figures are identical to history; nothing to restate.")
    stamp = datetime.now().isoformat(timespec="seconds")
    changed["Reason"], changed["Approved_By"], changed["Prepared_By"], changed["Restated_On"] = reason, approver_role, prepared_by, stamp
    existing = pd.read_csv(log_path) if os.path.exists(log_path) else pd.DataFrame()
    _write_atomic(pd.concat([existing, changed], ignore_index=True), log_path)
    new_rows = corrected_tb[keys + ["Amount"]].copy()
    new_rows["Locked_Version"] = f"restated-{stamp}"
    new_rows["Locked_On"] = stamp
    _write_atomic(pd.concat([history[history["Period"] != period], new_rows], ignore_index=True)[HISTORY_COLUMNS], path)
    return changed
