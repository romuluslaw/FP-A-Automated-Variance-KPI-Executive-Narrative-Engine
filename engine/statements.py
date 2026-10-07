"""Balance sheet and cash flow, derived from the same trial-balance data (statutory basis).

Balance sheet: lines come straight from the mapped trial balance. 'Current Period Earnings' (the month's
operating profit) is added because retained earnings in the export are the OPENING balance.
Cash flow (indirect method), derived month by month from balance sheet movements:
    Operating = Net income + Depreciation - change in receivables - change in prepaid
                + change in payables + change in accrued expenses
    Investing = -(change in fixed assets + depreciation)           (capital expenditure)
    Financing = change in share capital
Two controls protect it: the balance sheet must balance, and net cash flow must equal the change in cash.
"""
import pandas as pd

from engine.variance_ratios import period_add

BS_LINES = {"Asset": ["Cash", "Accounts Receivable", "Prepaid Expenses", "Fixed Assets"],
            "Liability": ["Accounts Payable", "Accrued Expenses"],
            "Equity": ["Share Capital", "Retained Earnings"]}


def _lines(agg: pd.DataFrame, scenario: str, period: str) -> dict:
    """{line: amount} for balance-sheet lines of one scenario/period, plus P&L totals."""
    sub = agg[(agg["Scenario"] == scenario) & (agg["Period"] == period)]
    by_line = sub.groupby("Line")["Amount"].sum().to_dict()
    cat = sub.groupby("Category")["Amount"].sum()
    by_line["_revenue"], by_line["_cogs"], by_line["_opex"] = (float(cat.get(c, 0.0)) for c in ("Revenue", "COGS", "OpEx"))
    by_line["_ni"] = by_line["_revenue"] - by_line["_cogs"] - by_line["_opex"]
    by_line["_dep"] = float(by_line.get("Depreciation", 0.0))
    return by_line


def build_balance_sheet(agg: pd.DataFrame) -> pd.DataFrame:
    """Long balance sheet (Scenario, Period, Line, Category, Amount) with subtotals and current earnings."""
    rows = []
    for (scenario, period), _ in agg.groupby(["Scenario", "Period"]):
        v = _lines(agg, scenario, period)
        if "Cash" not in v:
            continue                                    # no balance sheet for this scenario/month
        for cat, names in BS_LINES.items():
            rows += [(scenario, period, n, cat, float(v.get(n, 0.0))) for n in names]
        rows.append((scenario, period, "Current Period Earnings", "Equity", v["_ni"]))
        assets = sum(v.get(n, 0.0) for n in BS_LINES["Asset"])
        liab = sum(v.get(n, 0.0) for n in BS_LINES["Liability"])
        equity = sum(v.get(n, 0.0) for n in BS_LINES["Equity"]) + v["_ni"]
        rows += [(scenario, period, "Total Assets", "BS_Total", assets), (scenario, period, "Total Liabilities", "BS_Total", liab),
                 (scenario, period, "Total Equity", "BS_Total", equity)]
    return pd.DataFrame(rows, columns=["Scenario", "Period", "Line", "Category", "Amount"])


def balance_check(bs: pd.DataFrame) -> pd.DataFrame:
    """Assets - Liabilities - Equity for every scenario/month (must be zero)."""
    t = bs[bs["Category"] == "BS_Total"].pivot_table(index=["Scenario", "Period"], columns="Line", values="Amount", aggfunc="sum")
    t["Difference"] = t["Total Assets"] - t["Total Liabilities"] - t["Total Equity"]
    return t.reset_index()[["Scenario", "Period", "Difference"]]


def build_cash_flow(agg: pd.DataFrame, bs: pd.DataFrame) -> pd.DataFrame:
    """Long cash flow (Scenario, Period, Line, Category, Amount). A month needs the prior month's balance sheet."""
    rows = []
    have = set(zip(bs["Scenario"], bs["Period"]))
    for scenario, period in sorted(have):
        prior = period_add(period, -1)
        if (scenario, prior) not in have:
            continue
        cur, old = _lines(agg, scenario, period), _lines(agg, scenario, prior)
        d = lambda n: float(cur.get(n, 0.0)) - float(old.get(n, 0.0))      # noqa: E731  change in a balance
        items = [("Net Income", cur["_ni"]), ("Depreciation", cur["_dep"]), ("Change in Receivables", -d("Accounts Receivable")),
                 ("Change in Prepaid Expenses", -d("Prepaid Expenses")), ("Change in Payables", d("Accounts Payable")),
                 ("Change in Accrued Expenses", d("Accrued Expenses"))]
        cfo = sum(v for _, v in items)
        capex = -(d("Fixed Assets") + cur["_dep"])
        cff = d("Share Capital")
        items += [("Operating Cash Flow", cfo), ("Capital Expenditure", capex), ("Investing Cash Flow", capex),
                  ("Equity Raised", cff), ("Financing Cash Flow", cff), ("Net Cash Flow", cfo + capex + cff),
                  ("Free Cash Flow", cfo + capex)]
        rows += [(scenario, period, n, "CF_Total" if n.endswith("Cash Flow") else "CF_Line", v) for n, v in items]
    return pd.DataFrame(rows, columns=["Scenario", "Period", "Line", "Category", "Amount"])


def cash_reconciliation(bs: pd.DataFrame, cf: pd.DataFrame) -> pd.DataFrame:
    """Net cash flow minus change in cash for every scenario/month with a cash flow (must be zero)."""
    cash = bs[bs["Line"] == "Cash"].set_index(["Scenario", "Period"])["Amount"]
    net = cf[cf["Line"] == "Net Cash Flow"].set_index(["Scenario", "Period"])["Amount"]
    rows = [(s, p, net[(s, p)] - (cash[(s, p)] - cash[(s, period_add(p, -1))])) for (s, p) in net.index]
    return pd.DataFrame(rows, columns=["Scenario", "Period", "Difference"])
