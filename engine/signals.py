"""Rule-based signals: deterministic patterns the AI then explains (the AI never discovers them).

Each rule inspects engine results and returns a plain sentence whose figures are already formatted, so
the sentence goes into the pack as evidence and passes the number check.
"""
import pandas as pd

from engine import formatting as fmt


def _row(cmp_df: pd.DataFrame, statement: str, horizon: str, line: str):
    sub = cmp_df[(cmp_df["Statement"] == statement) & (cmp_df["BU"] == "All") & (cmp_df["Horizon"] == horizon) & (cmp_df["Line"] == line)]
    return sub.iloc[0] if len(sub) else None


def compute_signals(cmp_df: pd.DataFrame, kpis: dict, ratio_sets: dict) -> list:
    """Return [(visible_to, sentence)] for the signals that fire this period (possibly none)."""
    out = []
    ALL, INTERNAL = "Management|Board|Investor", "Management|Board"
    rev, cogs = _row(cmp_df, "P&L", "BvA_Month", "Total Revenue"), _row(cmp_df, "P&L", "BvA_Month", "Total COGS")
    if rev is not None and cogs is not None and rev["Var_Amt"] < 0 and cogs["Var_Amt"] > 0:
        out.append((ALL, f"Margin squeeze: Total Revenue was {fmt.money(rev['Actual'])} against budget of {fmt.money(rev['Comparator'])} "
                   f"while Total COGS was {fmt.money(cogs['Actual'])} against budget of {fmt.money(cogs['Comparator'])}."))
    month, prior = ratio_sets.get("Month"), ratio_sets.get("Prior_Month")
    if month and prior and month["DSO_Days"] - prior["DSO_Days"] >= 2:
        out.append((ALL, f"Collection pace: DSO moved from {fmt.days(prior['DSO_Days'])} to {fmt.days(month['DSO_Days'])}."))
    opex, revy = _row(cmp_df, "P&L", "YoY", "Total OpEx"), _row(cmp_df, "P&L", "YoY", "Total Revenue")
    if opex is not None and revy is not None and pd.notna(opex["Var_Pct"]) and pd.notna(revy["Var_Pct"]) and opex["Var_Pct"] > revy["Var_Pct"]:
        out.append((ALL, f"Cost growth: Total OpEx is {fmt.pct_fraction(opex['Var_Pct'])} higher year on year versus revenue growth of {fmt.pct_fraction(revy['Var_Pct'])}."))
    ocf, ni = kpis.get("Operating_Cash_Flow"), _row(cmp_df, "CF", "YoY", "Net Income")
    if ocf is not None and ni is not None and ocf < ni["Actual"]:
        out.append((INTERNAL, f"Cash conversion: Operating Cash Flow of {fmt.money(ocf)} is below Net Income of {fmt.money(ni['Actual'])}."))
    runway = kpis.get("Runway_Months")
    if runway is not None and runway < 12:
        out.append((INTERNAL, f"Cash runway is {runway:.1f} months, below 12 months."))
    return out
