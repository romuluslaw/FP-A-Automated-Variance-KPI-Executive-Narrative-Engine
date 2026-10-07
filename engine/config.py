"""Shared configuration: file locations, default thresholds and constants.

Every other module imports its paths and constants from here so nothing is hard-coded twice.
"""
from pathlib import Path

REPORTING_PERIOD = "2026-06"            # month being reported (YYYY-MM)
DEFAULT_ABS_THRESHOLD = 25_000.0        # materiality: absolute dollar variance
DEFAULT_REL_THRESHOLD = 0.05            # materiality: relative variance (5%)
REVISION_BLOCK_THRESHOLD = 10_000.0     # a changed ledger line above this needs an explanation to lock
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3.2"
DEFAULT_NUM_CTX = 16384                 # Ollama's own default window is small and truncates silently; this
                                         # engine's packs (cash flow + balance sheet audiences especially) have
                                         # been empirically found to need more than 10k tokens of context, with
                                         # 16384 validated clean end-to-end - see README section 12
DEFAULT_AI_TIMEOUT = 600                # seconds per AI call; CPU-only local inference of a long prompt on a
                                         # memory-constrained laptop can be slow - raise further if a run times
                                         # out (a full 3-audience run has been observed taking up to ~55 minutes
                                         # end-to-end on a 16GB-RAM laptop with no dedicated GPU)
SKILL_MAX_CHARS = 6000                  # analyst skill size limit (enforced by a test)
CARD_MAX_CHARS = 800                    # per-audience style card size limit
MAX_SLIDES = 7

AUDIENCES = ("Management", "Board", "Investor")
# Approval path per audience: groups are done in order; people inside a group may approve in any order.
APPROVAL_PATH = {"Management": (("Finance Director", "CFO"),),
                 "Board": (("Finance Director", "CFO"),),
                 "Investor": (("Finance Director", "CFO"), ("CEO",))}

PL_CATEGORIES = ("Revenue", "COGS", "OpEx", "UNMAPPED")   # categories that make up the P&L
BS_CATEGORIES = ("Asset", "Liability", "Equity")
PL_SUBTOTALS = ("Total Revenue", "Total COGS", "Gross Profit", "Total OpEx", "Operating Profit")
CF_SUBTOTALS = ("Operating Cash Flow", "Investing Cash Flow", "Financing Cash Flow", "Net Cash Flow", "Free Cash Flow")
BS_SUBTOTALS = ("Total Assets", "Total Liabilities", "Total Equity")
ALL_SUBTOTALS = PL_SUBTOTALS + CF_SUBTOTALS + BS_SUBTOTALS
HEADLINE_LINES = {"P&L": PL_SUBTOTALS, "CF": ("Operating Cash Flow", "Free Cash Flow", "Net Cash Flow"),
                  "BS": ("Cash", "Accounts Receivable", "Total Assets", "Total Liabilities", "Total Equity")}
BU_ALL = "All"                          # label for the all-business-units slice
CORPORATE_BU = "Corporate"              # balance-sheet lines carry this BU

# Comparison horizons: BvA = Budget vs Actual, RFvA = Rolling Forecast vs Actual.
HORIZONS = ("BvA_Month", "BvA_QTD", "BvA_YTD", "RFvA_Month", "RFvA_QTD", "RFvA_YTD", "MoM", "QoQ", "YoY")
BS_HORIZONS = ("BvA_Month", "RFvA_Month", "MoM", "QoQ", "YoY")   # balance sheet is point-in-time


class Paths:
    """Resolves every input/output location relative to the project root (or a test folder)."""

    def __init__(self, base_dir=None):
        self.base = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
        self.config = self.base / "config"
        self.input = self.base / "data" / "input"
        self.samples = self.input / "samples"
        self.history = self.base / "data" / "history" / "actuals_history.csv"
        self.restatement_log = self.base / "data" / "history" / "restatement_log.csv"
        self.outputs = self.base / "outputs"
        self.default_tb = self.input / "trial_balance.xlsx"       # the file Finance drops each run
        self.kpi_targets = self.input / "kpi_targets.csv"
        self.skill = self.config / "analyst_skill.md"
        self.cards = self.config / "audience_cards.md"
        self.prompt = self.config / "standard_prompt.md"
        self.deck_template = self.config / "deck_template.pptx"   # optional; a plain default is used if absent

    def out_dir(self, version_id: str) -> Path:
        """Per-run output folder, e.g. outputs/2026-06_r2."""
        return self.outputs / version_id

    def runs_dir(self, period: str) -> Path:
        """Folder holding the run registry and trial-balance snapshots for a period."""
        return self.outputs / "_runs" / period
