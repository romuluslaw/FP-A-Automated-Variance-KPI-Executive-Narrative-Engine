"""Stages 5-6 - Audience packs and safeguards.

Stage 5 builds one pack per audience from the locked results:
  Management -> management basis (business units, reclasses, Adjusted Gross Margin) plus cash and balance sheet
  Board      -> statutory basis plus cash and balance sheet
  Investor   -> statutory totals against budget, QoQ and YoY, plus agreed KPIs
Stage 6 applies the editable exclusion rules (config/exclusion_rules.csv) and redaction, and records a
protection log. A pack renders to ONE Markdown file: the analyst skill, the audience card, the facts and the
standard prompt (last, because that is what a model reads most recently). It can be used with any AI platform.
"""
import pandas as pd

from engine import formatting as fmt
from engine.config import ALL_SUBTOTALS, BU_ALL, HEADLINE_LINES, HORIZONS, PL_SUBTOTALS
from engine.ingestion import read_table
from engine.kpi import KPI_META

RATIO_LABELS = {
    "Gross_Margin_%": ("Gross margin (statutory)", fmt.pct_points),
    "Adjusted_Gross_Margin_%": ("Adjusted gross margin (management basis)", fmt.pct_points),
    "Operating_Margin_%": ("Operating margin", fmt.pct_points),
    "DSO_Days": ("DSO", fmt.days), "DPO_Days": ("DPO", fmt.days),
    "Working_Capital_Drag_Days": ("Working capital drag", fmt.days),
    "Debtor_Turnover_x": ("Debtor turnover", fmt.times), "Payable_Turnover_x": ("Payable turnover", fmt.times),
}
RATIO_SET_LABELS = {"Month": "This month", "Prior_Month": "Prior month", "Prior_Year_Month": "Same month last year", "QTD": "Quarter to date"}
STATEMENT_ORDER = {"P&L": 0, "CF": 1, "BS": 2}
STATEMENT_NAMES = {"P&L": "Profit and loss", "CF": "Cash flow", "BS": "Balance sheet"}


# ---------------------------------------------------------------- loading helpers
def load_policy(df: pd.DataFrame) -> dict:
    """Audience policy table -> {audience: {'basis', 'redaction_level', 'revision_note'}}."""
    return {r.Audience: {"basis": r.Basis, "redaction_level": r.Redaction_Level,
                         "revision_note": r.Revision_Note.strip().lower() == "yes"} for r in df.itertuples()}


def forbidden_terms_for(audience: str, rules: pd.DataFrame) -> list:
    """All forbidden words for an audience, collected from the exclusion rules ('a|b' lists)."""
    terms = []
    for cell in rules.loc[rules["Audience"] == audience, "Forbidden_Terms"]:
        terms += [t.strip() for t in str(cell).split("|") if t.strip()]
    return sorted(set(terms))


def _sections(text: str) -> dict:
    sections, current = {}, None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def instructions_for(audience: str, skill_text: str, cards_text: str, horizons=()) -> str:
    """Skill + the audience's card + ONLY the horizon definitions present in the pack.

    Filtering horizons means an investor pack never defines forecast or month-on-month comparisons it excludes.
    """
    cards = _sections(cards_text)
    defs = "\n".join(l for l in cards.get("Horizon definitions", "").splitlines() if any(l.startswith(f"- {h}:") for h in horizons))
    parts = [skill_text.strip(), f"### Audience card\n{cards.get('Audience: ' + audience, '')}"]
    if defs:
        parts.append(f"### Comparison definitions\n{defs}")
    return "\n\n".join(parts)


def build_notes(driver_notes: pd.DataFrame, tb: pd.DataFrame, period: str) -> pd.DataFrame:
    """Combine driver notes and ledger memos: Source, Applies_To, Visible_To, Text.

    A driver note marked External_OK=Yes reaches the Board and investors (after redaction); No keeps it internal.
    Raw ledger memos are always management-only.
    """
    rows = [{"Source": "Driver note", "Applies_To": r.Applies_To,
             "Visible_To": "Management|Board|Investor" if str(r.External_OK).strip().lower() == "yes" else "Management", "Text": r.Note}
            for r in driver_notes[driver_notes["Period"] == period].itertuples()]
    for r in tb[tb["Memo"].str.strip() != ""].itertuples():
        rows.append({"Source": "Ledger memo", "Applies_To": r.ERP_Description, "Visible_To": "Management", "Text": r.Memo})
    return pd.DataFrame(rows, columns=["Source", "Applies_To", "Visible_To", "Text"])


def notes_todo(stat_cmp: pd.DataFrame, driver_notes: pd.DataFrame, period: str) -> pd.DataFrame:
    """The to-do list for FP&A: material budget variances (month) that have no driver note yet."""
    covered = {a.strip().lower() for a in driver_notes.loc[driver_notes["Period"] == period, "Applies_To"]}
    sub = stat_cmp[(stat_cmp["Statement"] == "P&L") & (stat_cmp["BU"] == BU_ALL) & (stat_cmp["Horizon"] == "BvA_Month")
                   & stat_cmp["Material"] & ~stat_cmp["Line"].isin(PL_SUBTOTALS)]
    todo = sub[~sub["Line"].str.lower().isin(covered)]
    return todo[["Line", "Actual", "Comparator", "Var_Amt", "Var_Pct", "Favorability"]].reset_index(drop=True)


# ---------------------------------------------------------------- stage 5: build
def select_pack_rows(cmp_df: pd.DataFrame, per_horizon: int = 6) -> pd.DataFrame:
    """Rows worth putting in a pack: headline subtotals per statement, BU operating profit, top material lines."""
    if cmp_df.empty:
        return cmp_df
    heads = [cmp_df[(cmp_df["Statement"] == st) & (cmp_df["BU"] == BU_ALL) & cmp_df["Line"].isin(lines)] for st, lines in HEADLINE_LINES.items()]
    bu_rows = cmp_df[(cmp_df["BU"] != BU_ALL) & (cmp_df["Line"] == "Operating Profit") & (cmp_df["Horizon"] == "BvA_Month")]
    material = cmp_df[(cmp_df["BU"] == BU_ALL) & cmp_df["Material"] & ~cmp_df["Line"].isin(ALL_SUBTOTALS)].copy()
    material["_abs"] = material["Var_Amt"].abs()
    material = material.sort_values("_abs", ascending=False).groupby(["Statement", "Horizon"]).head(per_horizon).drop(columns="_abs")
    out = pd.concat(heads + [bu_rows, material]).drop_duplicates(["Statement", "BU", "Horizon", "Line"])
    out["_s"] = out["Statement"].map(STATEMENT_ORDER)
    out["_h"] = out["Horizon"].map({h: i for i, h in enumerate(HORIZONS)})
    return out.sort_values(["_s", "_h", "BU"], kind="stable").drop(columns=["_s", "_h"]).reset_index(drop=True)


def build_pack(audience: str, ctx: dict, policy: dict) -> dict:
    """Assemble an UNREDACTED pack for one audience from the run context (stage 5)."""
    basis = policy[audience]["basis"]
    pl = ctx["mgmt_cmp"] if basis == "management" else ctx["stat_cmp"][ctx["stat_cmp"]["Statement"] == "P&L"]
    other = ctx["stat_cmp"][ctx["stat_cmp"]["Statement"] != "P&L"]
    cmp_df = pd.concat([pl, other], ignore_index=True)
    ratios = ctx["ratio_sets_mgmt"] if basis == "management" else ctx["ratio_sets_stat"]
    notes = ctx["notes"]
    visible = notes[notes["Visible_To"].map(lambda v: audience in str(v).split("|"))]
    kpis = ctx["kpis"]
    kpis = kpis[kpis["Visible_To"].map(lambda v: audience in str(v).split("|"))] if len(kpis) else kpis
    return {
        "audience": audience, "basis": basis, "redaction_level": policy[audience]["redaction_level"],
        "period": ctx["period"], "version": ctx["version_label"], "status_line": ctx["status_line"],
        "comparisons": select_pack_rows(cmp_df), "ratios": {k: dict(v) for k, v in ratios.items()},
        "kpis": kpis.reset_index(drop=True), "signals": [t for vis, t in ctx["signals"] if audience in vis.split("|")],
        "notes": visible.to_dict("records"), "notes_hidden": int(len(notes) - len(visible)),
        "revision_note": ctx["revision_note"] if policy[audience]["revision_note"] else "",
        "reclass_rules": ctx["reclass_lines"] if basis == "management" else [], "thresholds": ctx["thresholds"],
        "exclusion_log": [], "protection_log": {},
    }


# ---------------------------------------------------------------- stage 6: safeguards
def apply_safeguards(pack: dict, rules: pd.DataFrame, redactor) -> dict:
    """Apply exclusion rules, then redact notes, and write a protection log (stage 6).

    Rule types: horizon, line, statement, metric, dimension (BU) and detail (totals_only keeps subtotal lines only).
    Returns a NEW pack; the input pack is not modified.
    """
    pack = {**pack, "comparisons": pack["comparisons"].copy(), "ratios": {k: dict(v) for k, v in pack["ratios"].items()}}
    log = {"rules_applied": [], "rows_excluded": 0, "metrics_excluded": [], "notes_hidden_by_visibility": pack["notes_hidden"],
           "redaction_level": pack["redaction_level"], "redaction_counts": {}}
    for rule in rules[rules["Audience"] == pack["audience"]].itertuples():
        df = pack["comparisons"]
        if rule.Rule_Type == "metric":
            removed = sum(1 for rs in pack["ratios"].values() if rs.pop(rule.Target, None) is not None)
            if removed:
                log["metrics_excluded"].append(rule.Target)
                log["rules_applied"].append(f"metric:{rule.Target}")
            continue
        if df.empty:
            continue
        if rule.Rule_Type == "horizon":
            keep = df["Horizon"] != rule.Target
        elif rule.Rule_Type == "line":
            keep = df["Line"] != rule.Target
        elif rule.Rule_Type == "statement":
            keep = df["Statement"] != rule.Target
        elif rule.Rule_Type == "dimension" and rule.Target == "BU":
            keep = df["BU"] == BU_ALL
        elif rule.Rule_Type == "detail" and rule.Target == "totals_only":
            keep = df["Line"].isin(ALL_SUBTOTALS)
        else:
            raise ValueError(f"Unknown exclusion rule {rule.Rule_Type}:{rule.Target}")
        n = int((~keep).sum())
        pack["comparisons"] = df[keep].reset_index(drop=True)
        if n:
            log["rows_excluded"] += n
            log["rules_applied"].append(f"{rule.Rule_Type}:{rule.Target} ({n} rows)")
    notes, counts = [], {}
    for note in pack["notes"]:
        new = dict(note)
        for key in ("Applies_To", "Text"):
            new[key], c = redactor.redact(note[key], pack["redaction_level"])
            for label, n in c.items():
                counts[label] = counts.get(label, 0) + n
        notes.append(new)
    pack["notes"], log["redaction_counts"], pack["protection_log"] = notes, counts, log
    return pack


# ---------------------------------------------------------------- rendering
def _comparison_table(df: pd.DataFrame) -> str:
    lines = ["| Horizon | Slice | Line | Actual | Comparator | Variance | Variance % | Direction | Flag |", "|---|---|---|---|---|---|---|---|---|"]
    for r in df.itertuples():
        lines.append(f"| {r.Horizon} | {r.BU} | {r.Line} | {fmt.money(r.Actual)} | {fmt.money(r.Comparator)} | {fmt.money(r.Var_Amt)} | "
                     f"{fmt.pct_fraction(r.Var_Pct)} | {r.Favorability} | {'MATERIAL' if r.Material else ''} |")
    return "\n".join(lines)


def _ratio_table(ratios: dict) -> str:
    sets = [s for s in RATIO_SET_LABELS if s in ratios]
    metrics = [m for m in RATIO_LABELS if any(m in ratios[s] for s in sets)]
    lines = ["| Metric | " + " | ".join(RATIO_SET_LABELS[s] for s in sets) + " |", "|---|" + "---|" * len(sets)]
    for m in metrics:
        label, fn = RATIO_LABELS[m]
        lines.append(f"| {label} | " + " | ".join(fn(ratios[s][m]) if m in ratios[s] else "n/a" for s in sets) + " |")
    return "\n".join(lines)


def _kpi_table(kpis: pd.DataFrame) -> str:
    lines = ["| KPI | Value | Target | Status |", "|---|---|---|---|"]
    lines += [f"| {r.Label} | {r.Value_Text} | {r.Target_Text} | {r.Status} |" for r in kpis.itertuples()]
    return "\n".join(lines)


def render_pack_markdown(pack: dict, skill_text: str, cards_text: str, prompt_text: str) -> str:
    """Render the upload file: instructions, facts, then the standard prompt (last, so a model sees it most recently)."""
    thr, df = pack["thresholds"], pack["comparisons"]
    horizons = set(df["Horizon"]) if len(df) else ()
    parts = [f"# {pack['audience']} pack - {fmt.period_label(pack['period'])} - {pack['status_line']}",
             "## Instructions", instructions_for(pack["audience"], skill_text, cards_text, horizons),
             "## Facts (the only figures you may use)",
             f"Basis: {pack['basis']}. A line is MATERIAL when its variance is at least {fmt.money(thr['abs'])} and at least {fmt.pct_fraction(thr['rel'])}."]
    for statement in ("P&L", "CF", "BS"):
        sub = df[df["Statement"] == statement] if len(df) else df
        if len(sub):
            parts += [f"### Comparisons - {STATEMENT_NAMES[statement]}", _comparison_table(sub)]
    if len(pack["kpis"]):
        parts += ["### KPI scorecard (status computed by the engine)", _kpi_table(pack["kpis"])]
    if pack["signals"]:
        parts += ["### Signals (rule-based, to be explained, not extended)", "\n".join(f"- {s}" for s in pack["signals"])]
    parts += ["### Ratios", _ratio_table(pack["ratios"])]
    if pack["reclass_rules"]:
        parts += ["### Management reclass rules applied", "\n".join(f"- {r}" for r in pack["reclass_rules"])]
    parts += ["### Driver notes (the only permitted explanations)",
              "\n".join(f"- [{n['Source']}] {n['Applies_To']}: {n['Text']}" for n in pack["notes"]) or "None provided."]
    if pack["revision_note"]:
        parts += ["### Data-quality note", pack["revision_note"]]
    parts += ["---", prompt_text.strip()]
    return "\n\n".join(parts) + "\n"
