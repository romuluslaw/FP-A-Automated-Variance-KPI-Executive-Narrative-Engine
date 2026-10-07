"""Pipeline orchestrator: runs workflow stages 1-10 in order.

  1 Data in           2 Apply mappings         3 Reconcile + compute (HARD STOPS: bridge, balance sheet, cash)
  4 Revision log      5 Audience packs         6 Safeguards (exclusion + redaction)
  7 AI analysis       8 Automated checks       9 Deck build        10 Sign-off state

Status returned: SUCCESS, BLOCKED (a control failed: nothing is reported), FAILED (bad input).
Logging goes to stderr; the CLI prints clean JSON on stdout. Set FPA_QUIET=1 to silence progress messages.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd

from engine import formatting as fmt
from engine.anonymizer import Redactor
from engine.config import (APPROVAL_PATH, AUDIENCES, DEFAULT_ABS_THRESHOLD, DEFAULT_AI_TIMEOUT, DEFAULT_NUM_CTX,
                           DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL, DEFAULT_REL_THRESHOLD, PL_SUBTOTALS,
                           REPORTING_PERIOD, REVISION_BLOCK_THRESHOLD, Paths)
from engine.history import lock_period
from engine.ingestion import (IngestionError, check_not_last_month, file_sha256, load_column_map, load_history,
                              load_scenario, load_sheets_workbook, load_trial_balance, normalise_period, read_table,
                              require_columns)
from engine.kpi import compute_kpis, score_kpis
from engine.llm_narrative import generate_output, parse_ai_json
from engine.mapping import MappingError, apply_management_mapping, apply_statutory_mapping, find_unmapped
from engine.packs import (apply_safeguards, build_notes, build_pack, forbidden_terms_for, load_policy, notes_todo,
                          render_pack_markdown)
from engine.reconciliation import build_bridge_table, control_total_check, operating_profit_check
from engine.revision_log import RevisionError, assert_all_explained, build_revision_log, revision_impact
from engine.runs import register_run
from engine.signals import compute_signals
from engine.signoff import (RELEASED, SignoffError, approve, load_state, log_dataframe, new_state, pending_roles,
                            record_edit, reject, save_state)
from engine.statements import balance_check, build_balance_sheet, build_cash_flow, cash_reconciliation
from engine.validation import check_slide_spec, run_output_checks, spec_text
from engine.variance_ratios import (adjusted_gross_margin, aggregate_fact, compute_comparisons, compute_ratio_sets,
                                    compute_statement_comparisons, period_add)

REQUIRED = {
    "coa_statutory": ["ERP_Account_Code", "Std_Category", "Std_Line", "Effective_From"],
    "coa_management": ["ERP_Account_Code", "Mgmt_Category", "Mgmt_Line", "Effective_From"],
    "mgmt_reclass_rules": ["Rule_ID", "Source_Account_Code", "Percent", "Target_Line", "Target_Category", "Effective_From", "Description"],
    "driver_notes": ["Period", "Applies_To", "Note", "External_OK"],
    "revision_reasons": ["Period", "ERP_Account_Code", "BU", "Adjustment_Ref", "Reason", "Prepared_By"],
    "kpi_targets": ["KPI", "Direction", "Target", "Tolerance_Pct", "Visible_To"],
}


class PipelineBlocked(Exception):
    """A control failed at a stage; the run stops and nothing is reported."""

    def __init__(self, stage: int, message: str):
        super().__init__(message)
        self.stage = stage


def _log(message: str) -> None:
    if not os.environ.get("FPA_QUIET"):
        print(message, file=sys.stderr)


def _stage(result: dict, number: int, name: str, status: str, detail: str = "") -> None:
    result["stages"].append({"stage": number, "name": name, "status": status, "detail": detail})
    _log(f"[stage {number}] {name}: {status} {detail}")


def load_human_tables(paths: Paths, sheets_workbook=None, reasons_path=None, warnings=None) -> dict:
    """Load the people-owned tables from CSV files or from a Google Sheets/Excel export (text trimmed, periods repaired).

    Exclusion rules, audience policy and the entity master are never read from Sheets (governance).
    """
    if sheets_workbook:
        tables = load_sheets_workbook(sheets_workbook)
        for name, cols in REQUIRED.items():
            require_columns(tables[name], cols, f"Sheets tab for {name}")
    else:
        sources = {"coa_statutory": paths.config / "coa_statutory.csv", "coa_management": paths.config / "coa_management.csv",
                   "mgmt_reclass_rules": paths.config / "mgmt_reclass_rules.csv", "driver_notes": paths.input / "driver_notes.csv",
                   "revision_reasons": paths.input / "revision_reasons.csv", "kpi_targets": paths.kpi_targets}
        tables = {name: read_table(path, REQUIRED[name]) for name, path in sources.items()}
    if reasons_path:
        tables["revision_reasons"] = read_table(reasons_path, REQUIRED["revision_reasons"])
    warnings = warnings if warnings is not None else []
    for name, df in tables.items():
        df = df.apply(lambda col: col.astype(str).str.strip())
        if "Period" in df.columns:
            df["Period"] = df["Period"].map(lambda v: normalise_period(v, warnings))
        tables[name] = df
    return tables


def _mgmt_ratio_sets(stat_sets: dict, mgmt_agg: pd.DataFrame, period: str) -> dict:
    spec = {"Month": (period, "MONTH"), "Prior_Month": (period_add(period, -1), "MONTH"),
            "Prior_Year_Month": (period_add(period, -12), "MONTH"), "QTD": (period, "QTD")}
    return {n: {**r, "Adjusted_Gross_Margin_%": adjusted_gross_margin(mgmt_agg, *spec[n])} for n, r in stat_sets.items()}


def _material_counts(cmp_df: pd.DataFrame) -> dict:
    sub = cmp_df[(cmp_df["Statement"] == "P&L") & (cmp_df["BU"] == "All") & cmp_df["Material"] & ~cmp_df["Line"].isin(PL_SUBTOTALS)]
    return {h: int(n) for h, n in sub.groupby("Horizon").size().items()}


def _redact_output(output: dict, redactor, level: str):
    """Second safety net: redact every AI-written string (commentary, analysis, slides). Returns (output, counts)."""
    counts = {}

    def clean(text):
        new, c = redactor.redact(text, level)
        for k, v in c.items():
            counts[k] = counts.get(k, 0) + v
        return new
    out = {**output, "commentary": clean(output["commentary"]),
           "analysis": {k: (clean(v) if isinstance(v, str) else [clean(t) for t in v]) for k, v in output["analysis"].items()},
           "slides": [{**s, "title": clean(s["title"]), "bullets": [clean(b) for b in s["bullets"]]} for s in output["slides"]]}
    return out, counts


def run_pipeline(tb_file=None, use_mock: bool = True, abs_threshold: float = DEFAULT_ABS_THRESHOLD,
                 rel_threshold: float = DEFAULT_REL_THRESHOLD, base_dir=None, period: str = REPORTING_PERIOD,
                 previous_tb_file=None, reasons_path=None, sheets_workbook=None, ollama_url: str = DEFAULT_OLLAMA_URL,
                 model: str = DEFAULT_OLLAMA_MODEL, num_ctx: int = DEFAULT_NUM_CTX, ai_timeout: int = DEFAULT_AI_TIMEOUT,
                 write_outputs: bool = True, build_decks: bool = True, ai_override: dict = None) -> dict:
    """Run the whole workflow on one trial-balance export and return a result dictionary.

    Run number is automatic: a new/different export for the same period is compared with the previous one
    (run registry) unless `previous_tb_file` is given. Business-rule failures return BLOCKED/FAILED, never raise.
    DataFrames for the dashboard are under result['_frames'] (not JSON-serialised).
    `ai_override` ({audience: path to a JSON file}) lets management use ANOTHER AI platform: the JSON it produced
    (same shape as the standard prompt) goes through the same redaction, checks and deck build as local output.
    """
    paths = Paths(base_dir)
    result = {"status": "RUNNING", "period": period, "run_no": 1, "version_id": f"{period}_r1", "stages": [], "warnings": [],
              "error": "", "thresholds": {"abs": abs_threshold, "rel": rel_threshold}}
    warnings = result["warnings"]
    try:
        # ---- Stage 1: data in -------------------------------------------------------------
        tb_path = Path(tb_file) if tb_file else paths.default_tb
        colmap = load_column_map(paths.config / "erp_column_map.csv")
        tb = load_trial_balance(tb_path, period, colmap, warnings)
        budget = load_scenario(paths.input / "budget.xlsx", "Budget", warnings)
        forecast = load_scenario(paths.input / "forecast.xlsx", "Forecast", warnings)
        history = load_history(paths.history, period, warnings)
        check_not_last_month(tb, history, period)
        if previous_tb_file:
            prev_tb, run_no = load_trial_balance(previous_tb_file, period, colmap, warnings), 2
        elif write_outputs:
            run_no, prev_tb = register_run(paths.runs_dir(period), tb)
        else:
            run_no, prev_tb = 1, None
        version_id = f"{period}_r{run_no}"
        result.update({"run_no": run_no, "version_id": version_id})
        status_line = ("DRAFT run 1 (first draft, not finance reviewed, NOT approved)" if run_no == 1
                       else f"DRAFT run {run_no} (after finance adjustments, pending sign-off, NOT approved)")
        tables = load_human_tables(paths, sheets_workbook, reasons_path, warnings)
        exclusion_rules = read_table(paths.config / "exclusion_rules.csv", ["Audience", "Rule_Type", "Target", "Forbidden_Terms"])
        policy = load_policy(read_table(paths.config / "audience_policy.csv", ["Audience", "Basis", "Redaction_Level", "Revision_Note"]))
        redactor = Redactor(read_table(paths.config / "entity_master.csv", ["Entity_Type", "Legal_Name", "Aliases"]))
        skill, cards, prompt = (p.read_text(encoding="utf-8") for p in (paths.skill, paths.cards, paths.prompt))
        manifest = {p.name: file_sha256(p) for p in [tb_path, paths.input / "budget.xlsx", paths.input / "forecast.xlsx", paths.history,
                                                      paths.skill, paths.cards, paths.prompt] if p.exists()}
        if sheets_workbook:
            manifest["sheets_workbook:" + Path(sheets_workbook).name] = file_sha256(sheets_workbook)
        _stage(result, 1, "Data in", "WARN" if warnings else "PASS", f"{len(tb)} TB lines, {history['Period'].nunique()} locked months, run {run_no}")

        # ---- Stage 2: mappings ------------------------------------------------------------
        cols = ["Scenario", "Period", "ERP_Account_Code", "BU", "Amount"]
        actual = pd.concat([history.assign(Scenario="Actual"), tb.assign(Scenario="Actual")])[cols]
        raw = pd.concat([actual, budget[cols], forecast[cols]], ignore_index=True)
        stat_fact = apply_statutory_mapping(raw, tables["coa_statutory"])
        mgmt_fact = apply_management_mapping(raw, tables["coa_management"], tables["mgmt_reclass_rules"])
        unmapped = find_unmapped(stat_fact)
        for row in unmapped.itertuples():
            warnings.append(f"AUDIT: account {row.ERP_Account_Code} is not in the statutory mapping ({fmt.money(row.Amount)}).")
        _stage(result, 2, "Apply mappings (two owners)", "FAIL" if len(unmapped) else "PASS", f"{len(unmapped)} unmapped account(s)")

        # ---- Stage 3: reconcile (gates), then compute -----------------------------------------
        control, op_check = control_total_check(raw, stat_fact, mgmt_fact), operating_profit_check(stat_fact, mgmt_fact)
        bridge = build_bridge_table(stat_fact, mgmt_fact, tables["mgmt_reclass_rules"], period)
        stat_agg, mgmt_agg = aggregate_fact(stat_fact), aggregate_fact(mgmt_fact)
        bs = build_balance_sheet(stat_agg)
        cf = build_cash_flow(stat_agg, bs)
        bal, rec = balance_check(bs), cash_reconciliation(bs, cf)
        cur_bal = bal[(bal["Scenario"] == "Actual") & (bal["Period"] == period)]["Difference"].abs().max()
        cur_rec = rec[(rec["Scenario"] == "Actual") & (rec["Period"] == period)]["Difference"].abs().max()
        for name, frame in (("Balance sheet", bal), ("Cash flow", rec)):
            bad = frame[(frame["Difference"].abs() > 0.01) & ~((frame["Scenario"] == "Actual") & (frame["Period"] == period))]
            if len(bad):
                warnings.append(f"{name} does not tie for {len(bad)} other scenario-month(s), e.g. {bad.iloc[0]['Scenario']} {bad.iloc[0]['Period']}.")
        gates = {"control_totals": control, "operating_profit_check": op_check, "balance_sheet_difference": float(cur_bal),
                 "cash_reconciliation_difference": float(cur_rec)}
        gates["passes"] = bool(control["passes"] and op_check["passes"] and cur_bal <= 0.01 and cur_rec <= 0.01)
        result["bridge"] = {**gates, "table": bridge.to_dict("records")}
        if not gates["passes"]:
            raise PipelineBlocked(3, f"Reconciliation failed: control totals pass={control['passes']} (unmapped statutory "
                                     f"{fmt.money(control['unmapped_statutory'])}), operating profit difference {fmt.money(op_check['max_abs_difference'])}, "
                                     f"balance sheet difference {fmt.money(cur_bal)}, cash reconciliation difference {fmt.money(cur_rec)}.")
        stat_pl, w1 = compute_comparisons(stat_agg, period, "statutory", abs_threshold, rel_threshold)
        mgmt_cmp, w2 = compute_comparisons(mgmt_agg, period, "management", abs_threshold, rel_threshold, bu_slices=True)
        cf_cmp, w3 = compute_statement_comparisons(cf, period, "CF", abs_threshold, rel_threshold)
        bs_cmp, w4 = compute_statement_comparisons(bs, period, "BS", abs_threshold, rel_threshold)
        warnings += w1 + w2 + w3 + w4
        if stat_pl.empty:
            raise PipelineBlocked(3, "No comparison could be built from the data.")
        stat_cmp = pd.concat([stat_pl, cf_cmp, bs_cmp], ignore_index=True)
        stat_sets = compute_ratio_sets(stat_agg, period)
        mgmt_sets = _mgmt_ratio_sets(stat_sets, mgmt_agg, period)
        kpi_values = compute_kpis(stat_agg, mgmt_agg, stat_cmp, bs, cf, period)
        kpis = score_kpis(kpi_values, tables["kpi_targets"], warnings)
        signals = compute_signals(stat_cmp, kpi_values, stat_sets)
        result.update({"ratios": stat_sets["Month"], "management_ratios": mgmt_sets["Month"], "ratio_sets": stat_sets,
                       "kpis": kpis.drop(columns=["Visible_To"]).to_dict("records"), "signals": [t for _, t in signals],
                       "material_by_horizon": _material_counts(stat_cmp)})
        result["material_variances_count"] = result["material_by_horizon"].get("BvA_Month", 0)
        _stage(result, 3, "Reconcile, then compute", "PASS", "bridge, balance sheet and cash flow all tie; "
               f"{len(kpis)} KPIs scored, {len(signals)} signal(s)")

        # ---- Stage 4: revision log ---------------------------------------------------------------
        revision_note, rev_log = "", pd.DataFrame()
        if prev_tb is not None:
            reasons = tables["revision_reasons"][tables["revision_reasons"]["Period"] == period]
            rev_log = build_revision_log(prev_tb, tb, reasons, REVISION_BLOCK_THRESHOLD)
            assert_all_explained(rev_log)
            impact = revision_impact(rev_log, tables["coa_statutory"])
            result["revision_summary"] = impact
            auto = int(rev_log["Status"].str.startswith(("Explained (ledger", "Auto")).sum())
            revision_note = (f"Figures were revised after the first draft: {impact['lines_changed']} ledger lines changed, moving revenue by "
                             f"{fmt.money(impact['revenue_change'])} and operating profit by {fmt.money(impact['operating_profit_change'])}.")
            _stage(result, 4, "Finance review: revision log", "PASS", f"{len(rev_log)} changed lines, all explained ({auto} automatically)")
        else:
            _stage(result, 4, "Finance review: revision log", "SKIPPED", "first run: nothing to compare yet")

        # ---- Stages 5-6: packs and safeguards -------------------------------------------------
        todo = notes_todo(stat_cmp, tables["driver_notes"], period)
        ctx = {"period": period, "version_label": f"run {run_no}", "status_line": status_line, "stat_cmp": stat_cmp, "mgmt_cmp": mgmt_cmp,
               "ratio_sets_stat": stat_sets, "ratio_sets_mgmt": mgmt_sets, "kpis": kpis, "signals": signals,
               "notes": build_notes(tables["driver_notes"], tb, period), "revision_note": revision_note,
               "reclass_lines": [r.Description for r in tables["mgmt_reclass_rules"].itertuples()],
               "thresholds": {"abs": abs_threshold, "rel": rel_threshold}}
        raw_packs = {a: build_pack(a, ctx, policy) for a in AUDIENCES}
        _stage(result, 5, "Audience packs", "PASS", ", ".join(AUDIENCES))
        packs = {a: apply_safeguards(p, exclusion_rules, redactor) for a, p in raw_packs.items()}
        pack_texts = {a: render_pack_markdown(p, skill, cards, prompt) for a, p in packs.items()}
        _stage(result, 6, "Safeguards (exclusion + redaction)", "PASS",
               "; ".join(f"{a}: {p['protection_log']['rows_excluded']} rows excluded, {sum(p['protection_log']['redaction_counts'].values())} items redacted" for a, p in packs.items()))

        # ---- Stage 7: AI analysis and slide text -----------------------------------------------
        outputs, errors = {}, {}
        for a in AUDIENCES:
            if ai_override and a in ai_override:
                try:
                    out = {**parse_ai_json(Path(ai_override[a]).read_text(encoding="utf-8")), "mode": "EXTERNAL", "error": "", "prompt_tokens": 0}
                except (OSError, ValueError) as exc:
                    raise IngestionError(f"AI output file for {a} could not be used: {exc}") from exc
            else:
                out = generate_output(packs[a], pack_texts[a], use_mock, ollama_url, model, num_ctx, timeout=ai_timeout)
            out, counts = _redact_output(out, redactor, policy[a]["redaction_level"])
            for label, n in counts.items():
                packs[a]["protection_log"].setdefault("output_redactions", {})[label] = n
            outputs[a], errors[a] = out, out["error"]
            if out["mode"] == "MOCK_FALLBACK":
                warnings.append(f"{a}: live Ollama call failed ({out['error']}); showing a clearly labelled fallback.")
            if out["prompt_tokens"] > 0.8 * num_ctx:
                # num_ctx is a SHARED budget for the prompt and the model's reply together, not the prompt alone,
                # so the fix is not "raise it just above the prompt size" - that leaves little or no room for the
                # model to actually write its commentary, analysis and slides. Suggest a concrete floor with
                # headroom reserved for the output.
                suggested = out["prompt_tokens"] + 2048
                warnings.append(f"{a}: prompt is about {out['prompt_tokens']} tokens, close to the {num_ctx}-token "
                                f"context window. num_ctx covers the prompt AND the reply together, so raise it to "
                                f"at least {suggested} (not just above {out['prompt_tokens']}) to leave the model "
                                f"room to write its response.")
        modes = {a: o["mode"] for a, o in outputs.items()}
        _stage(result, 7, "AI analysis and slide text", "WARN" if "MOCK_FALLBACK" in modes.values() else "PASS", str(modes))

        # ---- Stage 8: automated checks ----------------------------------------------------------
        checks = {}
        for a in AUDIENCES:
            c = run_output_checks(spec_text(outputs[a]), pack_texts[a], forbidden_terms_for(a, exclusion_rules), redactor, policy[a]["redaction_level"])
            c["spec_problems"] = check_slide_spec(outputs[a], a)
            c["passed"] = bool(c["passed"] and not c["spec_problems"])
            checks[a] = c
        flagged = [a for a, c in checks.items() if not c["passed"]]
        _stage(result, 8, "Automated checks", "WARN" if flagged else "PASS",
               f"{len(flagged)} draft(s) need FP&A review" if flagged else "numbers, terms, leaks and slide spec clean")

        # ---- Stage 9: deck build ------------------------------------------------------------------
        out_dir = paths.out_dir(version_id)
        deck_checks, deck_paths = {}, {}
        if build_decks:
            try:
                from engine.deck import build_deck, check_deck
                deck_dir = (out_dir / "decks") if write_outputs else Path(tempfile.mkdtemp())
                deck_dir.mkdir(parents=True, exist_ok=True)
                for a in AUDIENCES:
                    path = deck_dir / f"{a.lower()}_deck.pptx"
                    build_deck(packs[a], outputs[a], path, paths.deck_template)
                    deck_checks[a], deck_paths[a] = check_deck(path, pack_texts[a]), str(path)
                bad = [a for a, c in deck_checks.items() if not c["passed"]]
                _stage(result, 9, "Build deck", "WARN" if bad else "PASS", f"{len(deck_paths)} decks" + (f"; check flagged {bad}" if bad else "; overflow and number checks clean"))
            except ImportError:
                _stage(result, 9, "Build deck", "SKIPPED", "python-pptx is not installed")
        else:
            _stage(result, 9, "Build deck", "SKIPPED", "disabled for this run")

        # ---- Stage 10: sign-off ---------------------------------------------------------------------
        states = {}
        for a in AUDIENCES:
            sp = out_dir / f"signoff_{a.lower()}.json"
            states[a] = load_state(sp) if sp.exists() else new_state(version_id, a)
            record_edit(states[a], "Pipeline", "Packs regenerated")
        _stage(result, 10, "Sign-off", "PASS", "; ".join(f"{a}: {' + '.join(APPROVAL_PATH[a][0])}" + (f", then {APPROVAL_PATH[a][1][0]}" if len(APPROVAL_PATH[a]) > 1 else "") for a in AUDIENCES))

        review = [{"Severity": "Action", "Area": "FP&A", "Item": f"Add a driver note for {r.Line}: {fmt.money(r.Var_Amt)} ({r.Favorability}) against budget"} for r in todo.itertuples()]
        for a, c in checks.items():
            review += [{"Severity": "Review", "Area": a, "Item": f"Figure not found in pack: {t}"} for t in c["numbers"]["unmatched"]]
            review += [{"Severity": "Review", "Area": a, "Item": f"Forbidden term: {t}"} for t in c["forbidden_terms"]]
            review += [{"Severity": "Review", "Area": a, "Item": f"Identifier leak: {t}"} for t in c["leaks"]]
            review += [{"Severity": "Review", "Area": a, "Item": f"Slide spec: {t}"} for t in c["spec_problems"]]
        for a, c in deck_checks.items():
            review += [{"Severity": "Review", "Area": f"{a} deck", "Item": t} for t in c["overflow"]]
        review += [{"Severity": "Info", "Area": "Data", "Item": w} for w in warnings]
        result.update({"narratives": {a: o["commentary"] for a, o in outputs.items()}, "narrative_modes": modes, "narrative_errors": errors,
                       "analysis": {a: o["analysis"] for a, o in outputs.items()}, "slides": {a: o["slides"] for a, o in outputs.items()},
                       "checks": checks, "deck_checks": deck_checks, "decks": deck_paths, "review_list": review,
                       "notes_todo": todo.to_dict("records"),
                       "protection_log": {a: p["protection_log"] for a, p in packs.items()},
                       "signoff": {a: {"status": s.status, "pending": pending_roles(s)} for a, s in states.items()}, "status": "SUCCESS"})
        result["_frames"] = {"stat_cmp": stat_cmp, "mgmt_cmp": mgmt_cmp, "bridge": bridge, "revision_log": rev_log, "kpis": kpis,
                             "notes_todo": todo, "bs": bs, "cf": cf, "pack_texts": pack_texts, "outputs": outputs, "packs": packs,
                             "signoff_log": log_dataframe(list(states.values())), "tb": tb}
        if write_outputs:
            _write_outputs(out_dir, result, pack_texts, manifest, states)
    except PipelineBlocked as exc:
        result["status"], result["error"] = "BLOCKED", str(exc)
        _stage(result, exc.stage, "Control failed", "FAIL", str(exc))
    except RevisionError as exc:
        result["status"], result["error"] = "BLOCKED", str(exc)
        _stage(result, 4, "Finance review: revision log", "FAIL", str(exc))
    except (IngestionError, MappingError) as exc:
        result["status"], result["error"] = "FAILED", str(exc)
        _stage(result, 1, "Data in", "FAIL", str(exc))
    return result


def _write_outputs(out_dir, result, pack_texts, manifest, states) -> None:
    """Write every run artefact to outputs/<period>_r<n>/ (packs, logs, CSVs, manifest, locked TB snapshot)."""
    (out_dir / "packs").mkdir(parents=True, exist_ok=True)
    for audience, text in pack_texts.items():
        (out_dir / "packs" / f"{audience.lower()}_pack.md").write_text(text, encoding="utf-8")
    f = result["_frames"]
    f["stat_cmp"].to_csv(out_dir / "comparisons_statutory_bs_cf.csv", index=False)
    f["mgmt_cmp"].to_csv(out_dir / "comparisons_management.csv", index=False)
    f["bridge"].to_csv(out_dir / "bridge.csv", index=False)
    f["revision_log"].to_csv(out_dir / "revision_log_INTERNAL.csv", index=False)
    f["kpis"].drop(columns=["Visible_To"]).to_csv(out_dir / "kpi_scorecard.csv", index=False)
    f["notes_todo"].to_csv(out_dir / "notes_todo.csv", index=False)
    f["signoff_log"].to_csv(out_dir / "signoff_log.csv", index=False)
    f["tb"].to_csv(out_dir / "trial_balance_snapshot.csv", index=False)
    for a, s in states.items():
        save_state(s, out_dir / f"signoff_{a.lower()}.json")
    (out_dir / "input_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out_dir / "ai_output.json").write_text(json.dumps({a: {k: v for k, v in o.items()} for a, o in f["outputs"].items()}, indent=2, default=str), encoding="utf-8")
    (out_dir / "review_list.json").write_text(json.dumps(result["review_list"], indent=2), encoding="utf-8")
    (out_dir / "run_summary.json").write_text(json.dumps(to_jsonable(result), indent=2), encoding="utf-8")


def to_jsonable(obj):
    """Convert a result (NaN, numpy types) into plain JSON-safe values; drops '_frames'."""
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, float) and obj != obj:
        return None
    if hasattr(obj, "item"):
        return to_jsonable(obj.item())
    return obj


# ---------------------------------------------------------------- sign-off actions and roll-forward
def _load_states(version_id: str, base_dir=None) -> dict:
    out_dir = Paths(base_dir).out_dir(version_id)
    missing = [a for a in AUDIENCES if not (out_dir / f"signoff_{a.lower()}.json").exists()]
    if missing:
        raise SignoffError(f"No sign-off state for {version_id}; run the pipeline first.")
    return {a: load_state(out_dir / f"signoff_{a.lower()}.json") for a in AUDIENCES}


def _save_states(version_id: str, states: dict, base_dir=None) -> None:
    out_dir = Paths(base_dir).out_dir(version_id)
    for a, s in states.items():
        save_state(s, out_dir / f"signoff_{a.lower()}.json")
    log_dataframe(list(states.values())).to_csv(out_dir / "signoff_log.csv", index=False)


def signoff_status(version_id: str, base_dir=None) -> dict:
    """Current sign-off position per audience: {audience: {'status', 'pending'}} (used by the dashboard)."""
    return {a: {"status": s.status, "pending": pending_roles(s)} for a, s in _load_states(version_id, base_dir).items()}


def signoff_action(version_id: str, role: str, decision: str = "APPROVE", base_dir=None, reject_type: str = "", reason: str = "",
                   auto_lock: bool = True) -> dict:
    """One click per approver: apply `role`'s decision to every audience that is waiting for that role.

    When every audience is RELEASED the month is locked into history automatically (auto_lock).
    """
    states = _load_states(version_id, base_dir)
    acted = []
    for a, s in states.items():
        if role in pending_roles(s):
            (approve(s, role) if decision == "APPROVE" else reject(s, role, reject_type, reason))
            acted.append(a)
    if not acted:
        raise SignoffError(f"{role} has nothing to {decision.lower()} for {version_id}.")
    _save_states(version_id, states, base_dir)
    out = {"acted_on": acted, "status": {a: s.status for a, s in states.items()}}
    if auto_lock and all(s.status == RELEASED for s in states.values()):
        out["lock"] = lock_after_release(version_id, base_dir)
    return out


def approve_all(version_id: str, base_dir=None, auto_lock: bool = True) -> dict:
    """Demo helper: Finance Director, then CFO, then CEO approve everything they can."""
    out = {}
    for role in ("Finance Director", "CFO", "CEO"):
        try:
            out = signoff_action(version_id, role, "APPROVE", base_dir, auto_lock=auto_lock)
        except SignoffError:
            continue
    return out


def lock_after_release(version_id: str, base_dir=None) -> dict:
    """Roll-forward: append the signed-off month's trial balance to locked history (all audiences must be RELEASED)."""
    paths = Paths(base_dir)
    states = _load_states(version_id, base_dir)
    pending = {a: s.status for a, s in states.items() if s.status != RELEASED}
    if pending:
        raise SignoffError(f"Cannot lock {version_id}: not released for {pending}.")
    snapshot = paths.out_dir(version_id) / "trial_balance_snapshot.csv"
    tb = pd.read_csv(snapshot, dtype={"Period": str, "ERP_Account_Code": str, "BU": str}, keep_default_na=False)
    tb["Amount"] = pd.to_numeric(tb["Amount"])
    period = tb["Period"].iloc[0]
    return {"status": "LOCKED", "period": period, "rows_appended": lock_period(paths.history, tb, period, version_id)}
