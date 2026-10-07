"""KPI scorecard: values computed by the engine, status judged against targets by the engine (never by the AI).

KPI targets live in data/input/kpi_targets.csv (or the KPI_Targets tab of the Sheets workbook):
KPI, Direction (Higher/Lower is better), Target, Tolerance_Pct, Visible_To (which audiences see it).
Status: On target; Watch (within the tolerance band of the target); Off target.
"""
import pandas as pd

from engine import formatting as fmt
from engine.variance_ratios import adjusted_gross_margin, compute_ratios, period_add

KPI_META = {
    "Gross_Margin_%": ("Gross margin (statutory)", fmt.pct_points),
    "Adjusted_Gross_Margin_%": ("Adjusted gross margin (management basis)", fmt.pct_points),
    "Operating_Margin_%": ("Operating margin", fmt.pct_points),
    "Opex_%_of_Revenue": ("Operating expenses as % of revenue (statutory)", fmt.pct_points),
    "Adjusted_Opex_%_of_Revenue": ("Adjusted opex % of revenue (management basis)", fmt.pct_points),
    "Revenue_Growth_QoQ_%": ("Revenue growth, quarter on quarter", fmt.pct_points),
    "Revenue_Growth_YoY_%": ("Revenue growth, year on year", fmt.pct_points),
    "DSO_Days": ("DSO", fmt.days), "DPO_Days": ("DPO", fmt.days), "Working_Capital_Drag_Days": ("Working capital drag", fmt.days),
    "Cash_Balance": ("Cash balance", fmt.money), "Operating_Cash_Flow": ("Operating cash flow", fmt.money),
    "Free_Cash_Flow": ("Free cash flow", fmt.money),
    "Runway_Months": ("Cash runway", lambda v: "Cash generative" if v is None else f"{v:.1f} months"),
}


def _cf_value(cf: pd.DataFrame, line: str, period: str):
    sub = cf[(cf["Scenario"] == "Actual") & (cf["Period"] == period) & (cf["Line"] == line)]
    return float(sub["Amount"].iloc[0]) if len(sub) else None


def compute_kpis(stat_agg: pd.DataFrame, mgmt_agg: pd.DataFrame, stat_cmp: pd.DataFrame, bs: pd.DataFrame, cf: pd.DataFrame, period: str) -> dict:
    """KPI values for the reporting month. None means 'cannot be computed' (or cash generative for runway).

    Gross_Margin_% is always the statutory figure (same for every audience, as it should be).
    Adjusted_Gross_Margin_% is the SEPARATE management-basis figure after the payroll/admin reclass;
    it is a distinct KPI, visible only to Management (see kpi_targets.csv), not a replacement of the statutory one.
    """
    r = compute_ratios(stat_agg, period)

    def _rev_opex(agg):
        """Revenue and OpEx for the period from a fact table (works for either the statutory or management basis)."""
        sub = agg[(agg["Scenario"] == "Actual") & (agg["Period"] == period)]
        return (float(sub.loc[sub["Category"] == "Revenue", "Amount"].sum()),
                float(sub.loc[sub["Category"] == "OpEx", "Amount"].sum()))

    rev, opex = _rev_opex(stat_agg)
    mgmt_rev, mgmt_opex = _rev_opex(mgmt_agg)

    def growth(horizon):
        row = stat_cmp[(stat_cmp["Statement"] == "P&L") & (stat_cmp["BU"] == "All") & (stat_cmp["Horizon"] == horizon)
                       & (stat_cmp["Line"] == "Total Revenue")]
        return None if row.empty or pd.isna(row.iloc[0]["Var_Pct"]) else round(float(row.iloc[0]["Var_Pct"]) * 100, 2)

    cash = bs[(bs["Scenario"] == "Actual") & (bs["Period"] == period) & (bs["Line"] == "Cash")]
    fcfs = [_cf_value(cf, "Free Cash Flow", p) for p in (period, period_add(period, -1), period_add(period, -2))]
    runway = None
    if all(v is not None for v in fcfs) and len(cash):
        avg = sum(fcfs) / 3
        runway = round(float(cash["Amount"].iloc[0]) / -avg, 1) if avg < 0 else None
    return {"Gross_Margin_%": r["Gross_Margin_%"], "Adjusted_Gross_Margin_%": adjusted_gross_margin(mgmt_agg, period),
            "Operating_Margin_%": r["Operating_Margin_%"],
            "Opex_%_of_Revenue": round(opex / rev * 100, 2) if rev > 0 else None,
            "Adjusted_Opex_%_of_Revenue": round(mgmt_opex / mgmt_rev * 100, 2) if mgmt_rev > 0 else None,
            "Revenue_Growth_QoQ_%": growth("QoQ"), "Revenue_Growth_YoY_%": growth("YoY"),
            "DSO_Days": r["DSO_Days"], "DPO_Days": r["DPO_Days"], "Working_Capital_Drag_Days": r["Working_Capital_Drag_Days"],
            "Cash_Balance": float(cash["Amount"].iloc[0]) if len(cash) else None,
            "Operating_Cash_Flow": _cf_value(cf, "Operating Cash Flow", period),
            "Free_Cash_Flow": _cf_value(cf, "Free Cash Flow", period), "Runway_Months": runway}


def kpi_status(value, target: float, direction: str, tolerance_pct: float) -> str:
    """On target / Watch / Off target. A missing value for runway means cash generative = On target."""
    if value is None:
        return "On target"
    band = abs(target) * tolerance_pct / 100
    if direction == "Higher":
        return "On target" if value >= target else "Watch" if value >= target - band else "Off target"
    return "On target" if value <= target else "Watch" if value <= target + band else "Off target"


def score_kpis(values: dict, targets: pd.DataFrame, warnings: list) -> pd.DataFrame:
    """One row per KPI in the targets table: Value, Target, Status and which audiences see it."""
    rows = []
    for t in targets.itertuples():
        if t.KPI not in KPI_META:
            warnings.append(f"KPI target '{t.KPI}' is not a known KPI and was ignored.")
            continue
        value = values.get(t.KPI)
        if value is None and t.KPI != "Runway_Months":
            warnings.append(f"KPI {t.KPI} could not be computed (missing data).")
            continue
        target = float(t.Target)
        rows.append({"KPI": t.KPI, "Label": KPI_META[t.KPI][0], "Value": value, "Value_Text": KPI_META[t.KPI][1](value),
                     "Target": target, "Target_Text": KPI_META[t.KPI][1](target), "Direction": t.Direction,
                     "Status": kpi_status(value, target, t.Direction, float(t.Tolerance_Pct)), "Visible_To": t.Visible_To})
    df = pd.DataFrame(rows)
    if len(df):
        df["Value"] = df["Value"].astype(object).where(df["Value"].notna(), None)   # keep 'cannot compute' as None, not NaN
    return df
