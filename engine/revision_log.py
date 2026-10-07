"""Stage 4 - Revision log: every change between run 1 (Day 3) and run 2 (finance reviewed) is recorded automatically.

The script compares the two exports line by line, so an adjustment cannot exist without appearing here.
Finance effort is kept near zero:
* a change is explained automatically from the ERP journal description (memo) when there is one;
* a manual reason (optional, config/revision_reasons) overrides it;
* a change below the block threshold is logged automatically;
* only a change ABOVE the threshold with no explanation blocks the lock.
This log is INTERNAL (Finance and FP&A); it never goes into a pack.
"""
import pandas as pd

KEYS = ["Period", "ERP_Account_Code", "BU"]


class RevisionError(Exception):
    """Raised when run 2 contains material changes that nobody has explained."""


def build_revision_log(prev_tb: pd.DataFrame, tb: pd.DataFrame, reasons: pd.DataFrame, block_threshold: float) -> pd.DataFrame:
    """Diff two trial balances and explain each change. Lines in only one version count as 0 in the other."""
    left = prev_tb[KEYS + ["ERP_Description", "Amount", "Memo"]].rename(columns={"Amount": "Previous_Amount", "Memo": "Memo_Prev"})
    right = tb[KEYS + ["ERP_Description", "Amount", "Memo"]].rename(columns={"Amount": "Current_Amount", "Memo": "Memo_Cur"})
    merged = left.merge(right, on=KEYS, how="outer", suffixes=("_p", "_c"))
    merged["ERP_Description"] = merged["ERP_Description_c"].fillna(merged["ERP_Description_p"])
    merged[["Previous_Amount", "Current_Amount"]] = merged[["Previous_Amount", "Current_Amount"]].fillna(0.0)
    merged[["Memo_Prev", "Memo_Cur"]] = merged[["Memo_Prev", "Memo_Cur"]].fillna("")
    merged["Change"] = merged["Current_Amount"] - merged["Previous_Amount"]
    changed = merged[merged["Change"].abs() > 0.005].copy()

    reason_cols = ["Adjustment_Ref", "Reason", "Prepared_By"]
    exact = reasons[reasons["BU"].str.strip() != ""] if len(reasons) else reasons
    anybu = reasons[reasons["BU"].str.strip() == ""].drop(columns="BU") if len(reasons) else reasons
    log = changed.merge(exact[KEYS + reason_cols] if len(exact) else pd.DataFrame(columns=KEYS + reason_cols), on=KEYS, how="left")
    if len(anybu):
        fill = changed[["Period", "ERP_Account_Code", "BU"]].merge(anybu[["Period", "ERP_Account_Code"] + reason_cols],
                                                                    on=["Period", "ERP_Account_Code"], how="left")
        for col in reason_cols:
            log[col] = log[col].fillna(fill.set_index(changed.index)[col].reindex(log.index))
    statuses, refs, texts = [], [], []
    for row in log.itertuples():
        manual = isinstance(row.Reason, str) and row.Reason.strip() != ""
        memo_changed = row.Memo_Cur.strip() != "" and row.Memo_Cur != row.Memo_Prev
        if manual:
            statuses.append("Explained (manual)"); refs.append(row.Adjustment_Ref if isinstance(row.Adjustment_Ref, str) else ""); texts.append(row.Reason)
        elif memo_changed:
            statuses.append("Explained (ledger memo)"); refs.append(""); texts.append(row.Memo_Cur)
        elif abs(row.Change) < block_threshold:
            statuses.append("Auto-logged (below threshold)"); refs.append(""); texts.append("")
        else:
            statuses.append("UNEXPLAINED"); refs.append(""); texts.append("")
    log["Status"], log["Adjustment_Ref"], log["Reason"] = statuses, refs, texts
    log["Prepared_By"] = log["Prepared_By"].fillna("") if "Prepared_By" in log else ""
    log["Memo"] = log["Memo_Cur"]
    out = ["Period", "ERP_Account_Code", "BU", "ERP_Description", "Previous_Amount", "Current_Amount", "Change",
           "Adjustment_Ref", "Reason", "Prepared_By", "Memo", "Status"]
    return log[out].sort_values(["ERP_Account_Code", "BU"]).reset_index(drop=True)


def assert_all_explained(log: pd.DataFrame) -> None:
    """Block the lock if any material change has no explanation. The message lists the offending lines."""
    bad = log[log["Status"] == "UNEXPLAINED"]
    if len(bad):
        lines = ", ".join(f"{r.ERP_Account_Code}/{r.BU} ({r.Change:+,.0f})" for r in bad.itertuples())
        raise RevisionError(f"{len(bad)} material change(s) between run 1 and run 2 have no explanation (add a journal description in the ERP or a reason): {lines}")


def revision_impact(log: pd.DataFrame, stat_mapping: pd.DataFrame) -> dict:
    """Net effect of the revisions on revenue and operating profit (used for the one-line Board note)."""
    category = stat_mapping.drop_duplicates("ERP_Account_Code").set_index("ERP_Account_Code")["Std_Category"]
    cat = log["ERP_Account_Code"].map(category)
    revenue = float(log.loc[cat == "Revenue", "Change"].sum())
    costs = float(log.loc[cat.isin(["COGS", "OpEx"]), "Change"].sum())
    return {"lines_changed": int(len(log)), "revenue_change": revenue, "operating_profit_change": revenue - costs}
