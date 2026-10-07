"""Number and label formatting shared by packs, narratives and the number checker.

Packs and narratives MUST format numbers through these helpers so that the number
checker can match every figure in an AI draft back to the pack it came from.
"""
import calendar
import math


def _missing(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def money(v) -> str:
    """Whole-dollar currency: 1234.5 -> '$1,235'; negatives as '-$1,235'; missing as 'n/a'."""
    if _missing(v):
        return "n/a"
    sign = "-" if round(v) < 0 else ""
    return f"{sign}${abs(v):,.0f}"


def pct_fraction(v) -> str:
    """Format a FRACTION (0.079) as a percentage with one decimal: '7.9%'."""
    if _missing(v):
        return "n/a"
    return f"{v * 100:.1f}%"


def pct_points(v) -> str:
    """Format a value already in percentage points (70.59) as '70.6%'."""
    if _missing(v):
        return "n/a"
    return f"{v:.1f}%"


def days(v) -> str:
    """Format day counts with two decimals so DSO - DPO visibly equals the drag: 44.29 -> '44.29 days'."""
    return "n/a" if _missing(v) else f"{v:.2f} days"


def times(v) -> str:
    """Format turnover multiples: 0.68 -> '0.68x'."""
    return "n/a" if _missing(v) else f"{v:.2f}x"


def period_label(period: str) -> str:
    """'2026-06' -> 'June 2026'."""
    year, month = int(period[:4]), int(period[5:7])
    return f"{calendar.month_name[month]} {year}"
