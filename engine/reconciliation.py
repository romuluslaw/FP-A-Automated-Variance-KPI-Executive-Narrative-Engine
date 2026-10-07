"""Stage 3 (gate) - Reconcile the two bases before any number is reported.

Hard stops (the run is BLOCKED if any fails):
1. Control total: every raw amount must reach both mapped views (nothing silently dropped).
2. No UNMAPPED accounts remain.
3. Operating profit is identical on both bases for every scenario and month. The management
   reclasses move cost between gross margin and operating expenses but must net to zero.
"""
import pandas as pd

from engine.config import PL_CATEGORIES


def operating_profit_by_period(fact: pd.DataFrame) -> pd.Series:
    """Operating profit per (Scenario, Period) = Revenue - COGS - OpEx (UNMAPPED excluded)."""
    pl = fact[fact["Category"].isin(["Revenue", "COGS", "OpEx"])]
    signed = pl["Amount"].where(pl["Category"] == "Revenue", -pl["Amount"])
    return signed.groupby([pl["Scenario"], pl["Period"]]).sum()


def control_total_check(raw: pd.DataFrame, stat_fact: pd.DataFrame, mgmt_fact: pd.DataFrame, tol: float = 0.01) -> dict:
    """Check raw totals equal mapped totals on both bases and that nothing is unmapped."""
    raw_total = float(raw["Amount"].sum())
    stat_total = float(stat_fact["Amount"].sum())
    mgmt_total = float(mgmt_fact["Amount"].sum())      # reclasses net to zero, so this must tie too
    unmapped_stat = float(stat_fact.loc[stat_fact["Category"] == "UNMAPPED", "Amount"].abs().sum())
    unmapped_mgmt = float(mgmt_fact.loc[mgmt_fact["Category"] == "UNMAPPED", "Amount"].abs().sum())
    passes = (abs(raw_total - stat_total) <= tol and abs(raw_total - mgmt_total) <= tol
              and unmapped_stat <= tol and unmapped_mgmt <= tol)
    return {"raw_total": raw_total, "statutory_total": stat_total, "management_total": mgmt_total,
            "unmapped_statutory": unmapped_stat, "unmapped_management": unmapped_mgmt, "passes": bool(passes)}


def operating_profit_check(stat_fact: pd.DataFrame, mgmt_fact: pd.DataFrame, tol: float = 0.01) -> dict:
    """Compare operating profit on both bases across ALL scenarios and months."""
    stat = operating_profit_by_period(stat_fact)
    mgmt = operating_profit_by_period(mgmt_fact)
    diff = (stat.reindex(stat.index.union(mgmt.index), fill_value=0)
            - mgmt.reindex(stat.index.union(mgmt.index), fill_value=0)).abs()
    max_diff = float(diff.max()) if len(diff) else 0.0
    return {"max_abs_difference": max_diff, "periods_checked": int(len(diff)), "passes": max_diff <= tol}


def build_bridge_table(stat_fact: pd.DataFrame, mgmt_fact: pd.DataFrame, rules: pd.DataFrame,
                       period: str, scenario: str = "Actual") -> pd.DataFrame:
    """Statutory gross profit -> management gross profit bridge for one month, plus operating profit.

    Shows exactly how much each reclass rule moved, so FP&A can explain the two gross margins.
    """
    def gross_profit(fact):
        sub = fact[(fact["Scenario"] == scenario) & (fact["Period"] == period)]
        rev = sub.loc[sub["Category"] == "Revenue", "Amount"].sum()
        cogs = sub.loc[sub["Category"] == "COGS", "Amount"].sum()
        return float(rev - cogs)

    rows = [("Statutory gross profit", gross_profit(stat_fact))]
    moved = mgmt_fact[(mgmt_fact["Scenario"] == scenario) & (mgmt_fact["Period"] == period)
                      & (mgmt_fact["Reclass_Rule"] != "")]
    for rule in rules.itertuples():
        amount = float(moved.loc[moved["Reclass_Rule"] == rule.Rule_ID, "Amount"].sum())
        rows.append((f"Less reclass {rule.Rule_ID}: {rule.Description}", -amount))
    rows.append(("Management gross profit (adjusted)", gross_profit(mgmt_fact)))
    stat_op = float(operating_profit_by_period(stat_fact).get((scenario, period), 0.0))
    mgmt_op = float(operating_profit_by_period(mgmt_fact).get((scenario, period), 0.0))
    rows += [("Statutory operating profit", stat_op), ("Management operating profit", mgmt_op),
             ("Difference (must be zero)", stat_op - mgmt_op)]
    return pd.DataFrame(rows, columns=["Step", "Amount"])
