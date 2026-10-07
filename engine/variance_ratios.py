"""Stage 3 - Compute: multi-horizon variances, materiality, favorability and working-capital ratios.

Data model: a LONG fact table with one row per Scenario / Period / BU / Line / Category.
Scenario is Actual, Budget or Forecast. Because every month sits in the same table, MoM, QoQ and
YoY are just different choices of which months to add up.

Conventions
* Var_Amt = Actual - Comparator.  Var_Pct is a FRACTION (0.079 = 7.9%), NaN when the comparator is 0.
* Favorable: revenue and profit are better when higher; COGS and OpEx are better when lower.
* DSO / DPO use the ACTUAL number of days in the period (not a fixed 30).
"""
import calendar

import numpy as np
import pandas as pd

from engine.config import BS_HORIZONS, BU_ALL, CORPORATE_BU, HORIZONS, PL_CATEGORIES


class MissingPeriodError(Exception):
    """Raised when a comparison needs a month that is not in the data (never silently treated as 0)."""


# ---------------------------------------------------------------- period helpers
def period_add(period: str, months: int) -> str:
    """Shift a 'YYYY-MM' period by a number of months: period_add('2026-01', -1) -> '2025-12'."""
    index = int(period[:4]) * 12 + (int(period[5:7]) - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def quarter_to_date(period: str) -> list:
    """Months of the current calendar quarter up to and including the period."""
    month = int(period[5:7])
    first = ((month - 1) // 3) * 3 + 1
    return [f"{period[:4]}-{m:02d}" for m in range(first, month + 1)]


def year_to_date(period: str) -> list:
    """Months from January up to and including the period."""
    return [f"{period[:4]}-{m:02d}" for m in range(1, int(period[5:7]) + 1)]


def days_in(periods: list) -> int:
    """Total calendar days across a list of months (used by DSO/DPO)."""
    return sum(calendar.monthrange(int(p[:4]), int(p[5:7]))[1] for p in periods)


def horizon_windows(period: str) -> dict:
    """Define each horizon as (actual scenario, actual months, comparator scenario, comparator months).

    QoQ compares quarter-to-date with the SAME months of the prior quarter, so a mid-quarter
    report is like-for-like. YoY compares the month with the same month last year.
    """
    month, qtd, ytd = [period], quarter_to_date(period), year_to_date(period)
    return {
        "BvA_Month": ("Actual", month, "Budget", month),
        "BvA_QTD": ("Actual", qtd, "Budget", qtd),
        "BvA_YTD": ("Actual", ytd, "Budget", ytd),
        "RFvA_Month": ("Actual", month, "Forecast", month),
        "RFvA_QTD": ("Actual", qtd, "Forecast", qtd),
        "RFvA_YTD": ("Actual", ytd, "Forecast", ytd),
        "MoM": ("Actual", month, "Actual", [period_add(period, -1)]),
        "QoQ": ("Actual", qtd, "Actual", [period_add(p, -3) for p in qtd]),
        "YoY": ("Actual", month, "Actual", [period_add(period, -12)]),
    }


# ---------------------------------------------------------------- aggregation
def aggregate_fact(fact: pd.DataFrame) -> pd.DataFrame:
    """Sum amounts by Scenario / Period / BU / Line / Category (reclass rows merge into their lines)."""
    return fact.groupby(["Scenario", "Period", "BU", "Line", "Category"], as_index=False)["Amount"].sum()


def window_total(agg: pd.DataFrame, scenario: str, periods: list, bu=None) -> pd.Series:
    """Add up P&L amounts for a scenario over a list of months, indexed by (Line, Category).

    Raises MissingPeriodError if any requested month has no data, so a gap can never be mistaken
    for a zero.
    """
    sub = agg[(agg["Scenario"] == scenario) & (agg["Category"].isin(PL_CATEGORIES)) & (agg["Period"].isin(periods))]
    if bu is not None:
        sub = sub[sub["BU"] == bu]
    missing = [p for p in periods if p not in set(sub["Period"])]
    if missing:
        raise MissingPeriodError(f"{scenario} data missing for {missing}")
    return sub.groupby(["Line", "Category"])["Amount"].sum()


def with_subtotals(totals: pd.Series) -> pd.Series:
    """Append Total Revenue, Total COGS, Gross Profit, Total OpEx and Operating Profit rows."""
    by_cat = totals.groupby(level="Category").sum()
    rev, cogs, opex = (float(by_cat.get(c, 0.0)) for c in ("Revenue", "COGS", "OpEx"))
    extra = pd.Series({("Total Revenue", "Revenue"): rev, ("Total COGS", "COGS"): cogs,
                       ("Gross Profit", "Profit"): rev - cogs, ("Total OpEx", "OpEx"): opex,
                       ("Operating Profit", "Profit"): rev - cogs - opex})
    extra.index = pd.MultiIndex.from_tuples(extra.index, names=["Line", "Category"])
    return pd.concat([totals, extra])


# ---------------------------------------------------------------- variance logic
def favorability(category: str, variance: float) -> str:
    """Label a variance Favorable / Unfavorable / Neutral according to the line's category."""
    if abs(variance) < 0.005:
        return "Neutral"
    if category in ("Revenue", "Profit"):
        return "Favorable" if variance > 0 else "Unfavorable"
    if category in ("COGS", "OpEx"):
        return "Favorable" if variance < 0 else "Unfavorable"
    return "n/a"


def flag_material(df: pd.DataFrame, abs_threshold: float, rel_threshold: float) -> pd.DataFrame:
    """Two-sided materiality: |Var $| >= abs threshold AND |Var %| >= rel threshold.

    If the percentage cannot be computed (comparator is zero) the dollar test alone decides,
    because a new line with a large dollar amount should never be hidden.
    """
    df = df.copy()
    big = df["Var_Amt"].abs() >= abs_threshold
    known = df["Var_Pct"].notna()
    df["Material"] = big & ((known & (df["Var_Pct"].abs() >= rel_threshold)) | ~known)
    return df


_ORDER = {"Total Revenue": (1, 1), "Total COGS": (2, 1), "Gross Profit": (3, 0),
          "Total OpEx": (4, 1), "Operating Profit": (5, 0)}
_CATEGORY_ORDER = {"Revenue": 1, "COGS": 2, "OpEx": 4}


def _build_comparison_frame(actual: pd.Series, comparator: pd.Series, basis: str, bu: str, horizon: str) -> pd.DataFrame:
    """Turn two window totals into one comparison table (subtotals, variances, favorability)."""
    a, c = with_subtotals(actual), with_subtotals(comparator)
    index = a.index.union(c.index)
    df = pd.DataFrame({"Actual": a.reindex(index, fill_value=0.0),
                       "Comparator": c.reindex(index, fill_value=0.0)}).reset_index()
    df["Var_Amt"] = df["Actual"] - df["Comparator"]
    df["Var_Pct"] = np.where(df["Comparator"].abs() > 0, df["Var_Amt"] / df["Comparator"].abs().replace(0, np.nan), np.nan)
    df["Favorability"] = [favorability(cat, v) for cat, v in zip(df["Category"], df["Var_Amt"])]
    df.insert(0, "Horizon", horizon)
    df.insert(0, "BU", bu)
    df.insert(0, "Basis", basis)
    df.insert(0, "Statement", "P&L")
    keys = [_ORDER.get(line, (_CATEGORY_ORDER.get(cat, 6), 0)) for line, cat in zip(df["Line"], df["Category"])]
    df["_o1"], df["_o2"] = [k[0] for k in keys], [k[1] for k in keys]
    return df.sort_values(["_o1", "_o2", "Line"]).drop(columns=["_o1", "_o2"]).reset_index(drop=True)


def compute_comparisons(agg: pd.DataFrame, period: str, basis: str, abs_threshold: float,
                        rel_threshold: float, bu_slices: bool = False):
    """Build every comparison horizon for one basis. Returns (DataFrame, warnings).

    bu_slices=True also builds one slice per business unit (management basis only).
    Horizons whose data is missing are skipped with a warning instead of being reported wrongly.
    """
    warnings, frames = [], []
    slices = [BU_ALL]
    if bu_slices:
        slices += sorted(b for b in agg["BU"].unique() if b != CORPORATE_BU)
    windows = horizon_windows(period)
    for bu in slices:
        for horizon in HORIZONS:
            a_sc, a_per, c_sc, c_per = windows[horizon]
            try:
                actual = window_total(agg, a_sc, a_per, None if bu == BU_ALL else bu)
                comparator = window_total(agg, c_sc, c_per, None if bu == BU_ALL else bu)
            except MissingPeriodError as exc:
                if bu == BU_ALL:
                    warnings.append(f"{basis}: horizon {horizon} skipped ({exc}).")
                continue
            frames.append(_build_comparison_frame(actual, comparator, basis, bu, horizon))
    if not frames:
        return pd.DataFrame(), warnings + [f"{basis}: no comparison could be built."]
    return flag_material(pd.concat(frames, ignore_index=True), abs_threshold, rel_threshold), warnings


# ---------------------------------------------------------------- ratios
def _balance(agg: pd.DataFrame, period: str, category: str, line: str) -> float:
    """Actual period-end balance of a balance-sheet line."""
    sub = agg[(agg["Scenario"] == "Actual") & (agg["Period"] == period)
              & (agg["Category"] == category) & (agg["Line"] == line)]
    return float(sub["Amount"].sum())


def compute_ratios(agg: pd.DataFrame, period: str, window: str = "MONTH") -> dict:
    """Margins and working-capital ratios for the month or the quarter to date.

    DSO = AR / Revenue x days;  DPO = AP / COGS x days;  Working Capital Drag = DSO - DPO.
    Turnover = Revenue / AR and COGS / AP for the window. Zero denominators return 0.0.
    """
    periods = [period] if window == "MONTH" else quarter_to_date(period)
    pl = agg[(agg["Scenario"] == "Actual") & (agg["Period"].isin(periods))]
    rev = float(pl.loc[pl["Category"] == "Revenue", "Amount"].sum())
    cogs = float(pl.loc[pl["Category"] == "COGS", "Amount"].sum())
    opex = float(pl.loc[pl["Category"] == "OpEx", "Amount"].sum())
    ar = _balance(agg, period, "Asset", "Accounts Receivable")
    ap = _balance(agg, period, "Liability", "Accounts Payable")
    n_days = days_in(periods)
    dso = round(ar / rev * n_days, 2) if rev > 0 else 0.0
    dpo = round(ap / cogs * n_days, 2) if cogs > 0 else 0.0
    return {
        "Gross_Margin_%": round((rev - cogs) / rev * 100, 2) if rev > 0 else 0.0,
        "Operating_Margin_%": round((rev - cogs - opex) / rev * 100, 2) if rev > 0 else 0.0,
        "DSO_Days": dso,
        "DPO_Days": dpo,
        "Debtor_Turnover_x": round(rev / ar, 2) if ar > 0 else 0.0,
        "Payable_Turnover_x": round(cogs / ap, 2) if ap > 0 else 0.0,
        "Working_Capital_Drag_Days": round(dso - dpo, 2),
        "Days_In_Period": n_days,
    }


def compute_ratio_sets(agg: pd.DataFrame, period: str) -> dict:
    """Ratios for the month, prior month, same month last year and quarter to date.

    A set is omitted (not zero-filled) when its month is not in the data.
    """
    sets = {"Month": (period, "MONTH"), "Prior_Month": (period_add(period, -1), "MONTH"),
            "Prior_Year_Month": (period_add(period, -12), "MONTH"), "QTD": (period, "QTD")}
    actual_periods = set(agg.loc[agg["Scenario"] == "Actual", "Period"])
    return {name: compute_ratios(agg, p, w) for name, (p, w) in sets.items() if p in actual_periods}


def adjusted_gross_margin(mgmt_agg: pd.DataFrame, period: str, window: str = "MONTH") -> float:
    """Management-basis gross margin % (after payroll/admin reclass). Management pack only."""
    periods = [period] if window == "MONTH" else quarter_to_date(period)
    pl = mgmt_agg[(mgmt_agg["Scenario"] == "Actual") & (mgmt_agg["Period"].isin(periods))]
    rev = float(pl.loc[pl["Category"] == "Revenue", "Amount"].sum())
    cogs = float(pl.loc[pl["Category"] == "COGS", "Amount"].sum())
    return round((rev - cogs) / rev * 100, 2) if rev > 0 else 0.0


# ---------------------------------------------------------------- balance sheet and cash flow comparisons
BS_ORDER = ["Cash", "Accounts Receivable", "Prepaid Expenses", "Fixed Assets", "Total Assets", "Accounts Payable",
            "Accrued Expenses", "Total Liabilities", "Share Capital", "Retained Earnings", "Current Period Earnings", "Total Equity"]
CF_ORDER = ["Net Income", "Depreciation", "Change in Receivables", "Change in Prepaid Expenses", "Change in Payables",
            "Change in Accrued Expenses", "Operating Cash Flow", "Capital Expenditure", "Investing Cash Flow", "Equity Raised",
            "Financing Cash Flow", "Net Cash Flow", "Free Cash Flow"]
_HIGHER_BETTER = {"Cash", "Total Equity", "Net Income", "Operating Cash Flow", "Net Cash Flow", "Free Cash Flow"}
_LOWER_BETTER = {"Accounts Receivable"}


def favorability_line(line: str, variance: float) -> str:
    """Balance-sheet/cash-flow direction: cash, equity and operating/free cash flow are better higher; receivables better lower."""
    if abs(variance) < 0.005:
        return "Neutral"
    if line in _HIGHER_BETTER:
        return "Favorable" if variance > 0 else "Unfavorable"
    if line in _LOWER_BETTER:
        return "Favorable" if variance < 0 else "Unfavorable"
    return "n/a"


def _statement_total(df: pd.DataFrame, scenario: str, periods: list) -> pd.Series:
    """Sum a statement (cash flow: flows over months; balance sheet: a single month-end) by line."""
    sub = df[(df["Scenario"] == scenario) & (df["Period"].isin(periods))]
    missing = [p for p in periods if p not in set(sub["Period"])]
    if missing:
        raise MissingPeriodError(f"{scenario} data missing for {missing}")
    return sub.groupby("Line")["Amount"].sum()


def compute_statement_comparisons(df: pd.DataFrame, period: str, statement: str, abs_threshold: float, rel_threshold: float):
    """Comparisons for the cash flow ('CF', flows over all horizons) or balance sheet ('BS', month-end points).

    Balance sheet horizons: BvA and RFvA at month end, MoM vs prior month-end, QoQ vs the same position last quarter, YoY.
    Returns (DataFrame, warnings); horizons with missing data are skipped, never zero-filled.
    """
    order = CF_ORDER if statement == "CF" else BS_ORDER
    if statement == "CF":
        windows, horizons = horizon_windows(period), HORIZONS
    else:
        windows = {"BvA_Month": ("Actual", [period], "Budget", [period]), "RFvA_Month": ("Actual", [period], "Forecast", [period]),
                   "MoM": ("Actual", [period], "Actual", [period_add(period, -1)]),
                   "QoQ": ("Actual", [period], "Actual", [period_add(period, -3)]),
                   "YoY": ("Actual", [period], "Actual", [period_add(period, -12)])}
        horizons = BS_HORIZONS
    frames, warnings = [], []
    for horizon in horizons:
        a_sc, a_per, c_sc, c_per = windows[horizon]
        try:
            actual, comparator = _statement_total(df, a_sc, a_per), _statement_total(df, c_sc, c_per)
        except MissingPeriodError as exc:
            warnings.append(f"{statement}: horizon {horizon} skipped ({exc}).")
            continue
        index = [l for l in order if l in actual.index or l in comparator.index]
        out = pd.DataFrame({"Actual": actual.reindex(index, fill_value=0.0), "Comparator": comparator.reindex(index, fill_value=0.0)})
        out.index.name = "Line"
        out = out.reset_index()
        out["Var_Amt"] = out["Actual"] - out["Comparator"]
        out["Var_Pct"] = np.where(out["Comparator"].abs() > 0, out["Var_Amt"] / out["Comparator"].abs().replace(0, np.nan), np.nan)
        out["Favorability"] = [favorability_line(l, v) for l, v in zip(out["Line"], out["Var_Amt"])]
        out["Category"] = "CF" if statement == "CF" else "BS"
        out.insert(0, "Horizon", horizon)
        out.insert(0, "BU", BU_ALL)
        out.insert(0, "Basis", "statutory")
        out.insert(0, "Statement", statement)
        frames.append(out)
    if not frames:
        return pd.DataFrame(), warnings
    return flag_material(pd.concat(frames, ignore_index=True), abs_threshold, rel_threshold), warnings
