"""Stage 2 - Apply the two Chart-of-Accounts mappings (two owners).

* Statutory mapping  - owned by Finance; feeds the Board and Investor packs.
* Management mapping - owned by FP&A; adds management-only reclass rules (e.g. part of payroll
  and admin apportioned to cost of revenue). Feeds the Management pack only.

Both mappings are effective-dated: a row is used for periods on or after its Effective_From, so a
mapping change in July never rewrites June.
"""
import pandas as pd

UNMAPPED = "UNMAPPED"


class MappingError(Exception):
    """Raised when a mapping or reclass rule is invalid."""


def _apply_effective_mapping(fact: pd.DataFrame, mapping: pd.DataFrame, category_col: str, line_col: str) -> pd.DataFrame:
    """Attach Category and Line to every fact row using the mapping effective at the row's period.

    Rows whose account has no mapping are NOT dropped. They go to Category/Line 'UNMAPPED' so
    totals still tie and the reconciliation stage can stop the run (the original silent-drop bug).
    """
    left = fact.copy()
    left["_p"] = pd.to_datetime(left["Period"] + "-01")
    right = mapping[["ERP_Account_Code", category_col, line_col, "Effective_From"]].copy()
    right["_p"] = pd.to_datetime(right["Effective_From"] + "-01")
    merged = pd.merge_asof(left.sort_values("_p"), right.sort_values("_p").drop(columns="Effective_From"),
                           on="_p", by="ERP_Account_Code", direction="backward")
    merged = merged.rename(columns={category_col: "Category", line_col: "Line"})
    merged["Category"] = merged["Category"].fillna(UNMAPPED)
    merged["Line"] = merged["Line"].fillna(UNMAPPED)
    return merged.drop(columns="_p").reset_index(drop=True)


def apply_statutory_mapping(fact: pd.DataFrame, mapping: pd.DataFrame) -> pd.DataFrame:
    """Map raw rows to the statutory (financial statement) hierarchy."""
    out = _apply_effective_mapping(fact, mapping, "Std_Category", "Std_Line")
    out["Reclass_Rule"] = ""
    return out


def apply_reclass_rules(mapped: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
    """Move a percentage of a source account into a target line (e.g. 20% of payroll into COGS).

    Each rule is calculated on the ORIGINAL source amount, the source row is reduced and a new row
    is added to the target line, tagged with the Rule_ID. Reclasses net to zero, so total costs and
    operating profit are unchanged; only the gross margin moves.
    """
    base = mapped.copy()
    original = mapped["Amount"].copy()
    moved_frames = []
    for rule in rules.itertuples():
        percent = float(rule.Percent)
        if not 0 <= percent <= 1:
            raise MappingError(f"Reclass rule {rule.Rule_ID}: Percent must be between 0 and 1, got {rule.Percent}.")
        mask = (mapped["ERP_Account_Code"] == rule.Source_Account_Code) & (mapped["Period"] >= rule.Effective_From)
        moved = mapped[mask].copy()
        amount = original[mask] * percent
        base.loc[mask, "Amount"] = base.loc[mask, "Amount"] - amount
        moved["Amount"] = amount
        moved["Line"] = rule.Target_Line
        moved["Category"] = rule.Target_Category
        moved["Reclass_Rule"] = rule.Rule_ID
        moved_frames.append(moved)
    return pd.concat([base] + moved_frames, ignore_index=True)


def apply_management_mapping(fact: pd.DataFrame, mapping: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
    """Map raw rows to the management hierarchy, then apply the management-only reclass rules."""
    out = _apply_effective_mapping(fact, mapping, "Mgmt_Category", "Mgmt_Line")
    out["Reclass_Rule"] = ""
    return apply_reclass_rules(out, rules)


def find_unmapped(fact: pd.DataFrame) -> pd.DataFrame:
    """Return the distinct accounts that fell into the UNMAPPED bucket (for audit warnings)."""
    rows = fact[fact["Category"] == UNMAPPED]
    return rows.groupby("ERP_Account_Code", as_index=False)["Amount"].sum()
