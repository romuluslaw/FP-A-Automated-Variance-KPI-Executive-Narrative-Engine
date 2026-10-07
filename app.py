"""FP&A Automated Planning & Executive Narrative Dashboard (Streamlit).

Run:  streamlit run app.py
Flow: pick the trial balance export, press Run. Review the exceptions list, add any missing driver notes,
review the three audience outputs and decks, then approve (one click per approver). History locks automatically
when every audience is released.
"""
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import formatting as fmt  # noqa: E402
from engine.config import (AUDIENCES, DEFAULT_ABS_THRESHOLD, DEFAULT_AI_TIMEOUT, DEFAULT_NUM_CTX, DEFAULT_OLLAMA_MODEL,  # noqa: E402
                           DEFAULT_OLLAMA_URL, DEFAULT_REL_THRESHOLD, HORIZONS, REPORTING_PERIOD, Paths)
from engine.llm_narrative import check_ollama_connection, model_is_available  # noqa: E402
from engine.pipeline import run_pipeline, signoff_action, signoff_status  # noqa: E402
from engine.signoff import SignoffError  # noqa: E402

BASE = os.environ.get("FPA_BASE_DIR") or None          # tests point this at a temporary workspace
paths = Paths(BASE)
STATUS_ICON = {"PASS": "\u2705", "WARN": "\u26a0\ufe0f", "FAIL": "\u26d4", "SKIPPED": "\u23ed\ufe0f"}
KPI_ICON = {"On target": "\U0001f7e2", "Watch": "\U0001f7e1", "Off target": "\U0001f534"}
# Material-row highlight: colour the TEXT only (never fill the background). A solid light fill assumes a light
# Streamlit theme; on the default dark theme the text colour is unset and effectively invisible on a light block.
# Colouring the text keeps it readable on both light and dark themes without guessing the surrounding colour.
MATERIAL_STYLE = "color: #f5a623; font-weight: 700;"


def escape_md(text: str) -> str:
    """Escape markdown/LaTeX-special characters in a piece of RAW text. Does not render anything.

    Streamlit auto-renders "$...$" as LaTeX/KaTeX. Financial text is full of dollar signs, so every pair of "$"
    was being parsed as a math span: it swallowed the spaces between words (e.g. "vs budget" -> "vsbudget") and
    rendered numbers in an italic serif math font instead of the normal one. This escapes the AI's text so its
    dollar signs, underscores etc. show up literally instead of being interpreted as formatting.

    IMPORTANT: call this on each piece of AI-written text BEFORE composing it with our OWN markdown (e.g.
    f"**{escape_md(title)}**"), never on the already-composed string - escaping the whole string would also
    neutralise the "**" and "_" WE add for bold/italic, making them show up as literal asterisks and underscores
    (exactly the bug this replaces: titles were rendering as "**Title**" instead of bold).
    """
    for ch in ("\\", "$", "_", "*", "`"):
        text = text.replace(ch, "\\" + ch)
    return text


def safe_markdown(text: str) -> None:
    """Escape a plain (not further formatted) piece of AI text and render it."""
    st.markdown(escape_md(text))


st.set_page_config(page_title="FP&A Executive Engine", page_icon="\U0001f4ca", layout="wide")
st.title("\U0001f4ca Automated FP&A Executive Engine")
st.caption("Variance, KPI and cash analysis from a trial balance export, drafted for Management, the Board and Investors")

# ------------------------------------------------------------------ sidebar
sb = st.sidebar
sb.header("1. Choose the export")
choice = sb.selectbox("Trial balance export", ["Day 3 sample (first draft)", "Day 4 sample (after finance adjustments)",
                                              "Default file (data/input/trial_balance.xlsx)", "Upload a file"])
upload = sb.file_uploader("ERP export (.xlsx)", type=["xlsx"]) if choice == "Upload a file" else None
period = sb.text_input("Reporting month (YYYY-MM)", REPORTING_PERIOD)
with sb.expander("Materiality and AI settings"):
    abs_thresh = st.slider("Absolute threshold ($)", 5000, 100000, int(DEFAULT_ABS_THRESHOLD), 5000)
    rel_thresh = st.slider("Relative threshold (%)", 1.0, 20.0, DEFAULT_REL_THRESHOLD * 100, 0.5) / 100
    use_mock = st.toggle("Mock AI (no Ollama needed)", value=True)
    ollama_url = st.text_input("Ollama URL", DEFAULT_OLLAMA_URL)
    model = st.text_input("Model", DEFAULT_OLLAMA_MODEL)
    CTX_PRESETS = [2048, 4096, 8192, 16384, 24576, 32768, 49152, 65536, 98304, 131072]
    num_ctx = st.select_slider("Context window (tokens)", options=CTX_PRESETS,
                               value=DEFAULT_NUM_CTX if DEFAULT_NUM_CTX in CTX_PRESETS else 8192,
                               help="Pick from these preset sizes rather than typing an arbitrary number, since "
                                    "Ollama context sizes are normally powers of two. This budget covers the "
                                    "prompt AND the model's reply together, not the prompt alone - if a run warns "
                                    "that the prompt is close to this window, move the slider to the NEXT PRESET "
                                    "ABOVE the suggested minimum it reports (prompt tokens plus headroom for the "
                                    "reply), not just above the prompt size itself.")
    ai_timeout = st.number_input("Request timeout (seconds)", 30, 3600, DEFAULT_AI_TIMEOUT, 30,
                                 help="Local CPU inference of a long prompt can take a while - a full 3-audience run has taken up to "
                                      "~55 minutes end-to-end on a 16GB-RAM laptop with no dedicated GPU. Raise this well above the "
                                      "default if a run falls back due to a timeout; there is no cost to setting it high.")
    if st.button("Test Ollama connection"):
        probe = check_ollama_connection(ollama_url)
        if not probe["ok"]:
            st.error(f"Could not reach Ollama at {ollama_url}: {probe['error']}")
        elif not probe["models"]:
            st.warning("Ollama is reachable but has no models installed. Run: ollama pull llama3.2")
        elif not model_is_available(model, probe["models"]):
            st.warning(f"Ollama is reachable, but '{model}' is not installed. Installed: {', '.join(probe['models'])}. Run: ollama pull {model}")
        else:
            st.success(f"Ollama is reachable and '{model}' is installed. Installed models: {', '.join(probe['models'])}")
run_clicked = sb.button("Run workflow", type="primary")

if run_clicked:
    if choice.startswith("Day 3"):
        tb_file = paths.samples / "day3_trial_balance.xlsx"
    elif choice.startswith("Day 4"):
        tb_file = paths.samples / "day4_trial_balance.xlsx"
    elif choice.startswith("Default"):
        tb_file = paths.default_tb
    elif upload is not None:
        tb_file = Path(tempfile.mkdtemp()) / "upload.xlsx"
        tb_file.write_bytes(upload.getvalue())
    else:
        st.sidebar.error("Please upload a file first.")
        st.stop()
    with st.spinner("Running the workflow..."):
        st.session_state["result"] = run_pipeline(tb_file=tb_file, use_mock=use_mock, abs_threshold=float(abs_thresh), rel_threshold=rel_thresh,
                                                  base_dir=BASE, period=period, ollama_url=ollama_url, model=model, num_ctx=int(num_ctx),
                                                  ai_timeout=int(ai_timeout))
result = st.session_state.get("result")
if result is None:
    st.info("Choose an export in the sidebar and press **Run workflow**. Start with the Day 3 sample, then run the Day 4 sample: "
            "the engine detects it as run 2 and compares it with run 1 automatically.")
    st.stop()

# ------------------------------------------------------------------ status and stages
stages = pd.DataFrame([{"": STATUS_ICON.get(s["status"], ""), "Stage": s["stage"], "Name": s["name"], "Detail": s["detail"]} for s in result["stages"]])
if result["status"] != "SUCCESS":
    st.error(f"Run {result['status']}: {result['error']}")
    st.dataframe(stages, hide_index=True, use_container_width=True)
    st.stop()
st.success(f"Run {result['run_no']} complete for {fmt.period_label(result['period'])}  |  {result['version_id']}  |  DRAFT until all approvals are in")
with st.expander("Workflow stages", expanded=False):
    st.dataframe(stages, hide_index=True, use_container_width=True)

# ------------------------------------------------------------------ exceptions
review = pd.DataFrame(result["review_list"])
st.subheader("\u2757 Needs your attention")
actionable = review[review["Severity"].isin(["Action", "Review"])] if len(review) else review
if len(actionable):
    st.dataframe(actionable, hide_index=True, use_container_width=True)
    st.caption("Add missing driver notes in data/input/driver_notes.csv (or the Driver_Notes tab of your Sheets workbook), then press Run again. Notes carry over within the month.")
else:
    st.write("Nothing to review: every material variance has a note and every automated check is clean.")
infos = review[review["Severity"] == "Info"] if len(review) else review
if len(infos):
    with st.expander(f"{len(infos)} informational message(s)"):
        st.dataframe(infos, hide_index=True, use_container_width=True)

# ------------------------------------------------------------------ KPI scorecard
st.subheader("\U0001f4a1 KPI scorecard (status computed by the engine)")
audience_view = st.radio("Show KPIs visible to", AUDIENCES, horizontal=True)
packs = result["_frames"]["packs"]
kpis = packs[audience_view]["kpis"]
cols = st.columns(4)
for i, k in enumerate(kpis.itertuples()):
    tile_value = k.Value_Text if k.Value is not None else "N/A"
    tile_help = None if k.Value is not None else f"{k.Value_Text}: this KPI does not apply this month (for cash runway, free cash flow is positive)."
    cols[i % 4].metric(k.Label, tile_value, f"{KPI_ICON.get(k.Status, '')} {k.Status} (target {k.Target_Text})",
                       delta_color="off", help=tile_help)

# ------------------------------------------------------------------ three audience outputs
st.subheader("\U0001f4dd Audience outputs")
st.caption("Review screen only. In real use each audience receives its own file; the Management output never goes to the Board or investors.")
columns = st.columns(3)
titles = {"Management": "\U0001f454 Management", "Board": "\U0001f3db\ufe0f Board of Directors", "Investor": "\U0001f4c8 Investors (fully redacted)"}
for col, a in zip(columns, AUDIENCES):
    with col:
        st.markdown(f"### {titles[a]}")
        mode, check = result["narrative_modes"][a], result["checks"][a]
        st.caption(f"AI mode: **{mode}**  |  checks: {'\u2705 clean' if check['passed'] else '\u26a0\ufe0f review'}")
        if result["narrative_errors"].get(a):
            st.caption(f"\u26a0\ufe0f AI call detail: {result['narrative_errors'][a]}")
        safe_markdown(result["narratives"][a])
        log = result["protection_log"][a]
        st.caption(f"Protection log: {log['rows_excluded']} rows excluded, {sum(log['redaction_counts'].values())} items redacted"
                   f" ({', '.join(f'{k} {v}' for k, v in log['redaction_counts'].items()) or 'none'}), redaction level: {log['redaction_level']}")
        with st.expander("Slide text (from the AI)"):
            for slide in result["slides"][a]:
                st.markdown(f"**{escape_md(slide['title'])}**  _({escape_md(slide['layout'])})_")
                for b in slide["bullets"]:
                    st.markdown(f"- {escape_md(b)}")
        if a in result.get("decks", {}) and Path(result["decks"][a]).exists():
            st.download_button(f"Download {a} deck (.pptx)", Path(result["decks"][a]).read_bytes(), file_name=Path(result["decks"][a]).name,
                               mime="application/vnd.openxmlformats-officedocument.presentationml.presentation", key=f"deck_{a}")
        st.download_button(f"Download {a} pack (.md) for any AI platform", result["_frames"]["pack_texts"][a], file_name=f"{a.lower()}_pack.md", key=f"pack_{a}")

# ------------------------------------------------------------------ comparison explorer
st.subheader("\U0001f50d Comparison explorer")
f = result["_frames"]
e1, e2, e3, e4 = st.columns(4)
statement = e1.selectbox("Statement", ["P&L", "CF", "BS"])
basis = e2.selectbox("P&L basis", ["statutory", "management"], disabled=statement != "P&L")
source = f["mgmt_cmp"] if (statement == "P&L" and basis == "management") else f["stat_cmp"][f["stat_cmp"]["Statement"] == statement]
bu = e3.selectbox("Business unit", sorted(source["BU"].unique(), key=lambda x: (x != "All", x)))
horizons = [h for h in HORIZONS if h in set(source["Horizon"])]
horizon = e4.selectbox("Horizon", horizons)
view = source[(source["BU"] == bu) & (source["Horizon"] == horizon)].copy()
view["Actual"], view["Comparator"], view["Var_Amt"] = (view[c].map(fmt.money) for c in ("Actual", "Comparator", "Var_Amt"))
view["Var_Pct"] = view["Var_Pct"].map(fmt.pct_fraction)
shown = view[["Line", "Actual", "Comparator", "Var_Amt", "Var_Pct", "Favorability", "Material"]]
st.dataframe(shown.style.apply(lambda r: [MATERIAL_STYLE if r["Material"] else ""] * len(r), axis=1), hide_index=True, use_container_width=True)
st.caption("Highlighted (orange, bold) rows are MATERIAL: they cross both the dollar and the percentage threshold.")

# ------------------------------------------------------------------ controls and internal logs
with st.expander("Reconciliation and controls"):
    b = result["bridge"]
    # The two money-formatted values below contain a literal "$"; escape_md() is applied to each BEFORE
    # composing with our own "**" bold markup, for the same reason as the slide-title fix - escaping the whole
    # composed string would also neutralise our own "**", turning it into literal asterisks on screen.
    bs_diff, cash_diff = escape_md(fmt.money(b["balance_sheet_difference"])), escape_md(fmt.money(b["cash_reconciliation_difference"]))
    st.write(f"Operating profit identical on both bases: **{b['operating_profit_check']['passes']}**  |  "
             f"balance sheet difference: **{bs_diff}**  |  cash reconciliation difference: **{cash_diff}**")
    st.dataframe(f["bridge"].assign(Amount=f["bridge"]["Amount"].map(fmt.money)), hide_index=True, use_container_width=True)
with st.expander("Revision log (INTERNAL: Finance and FP&A only)"):
    if len(f["revision_log"]):
        st.dataframe(f["revision_log"], hide_index=True, use_container_width=True)
    else:
        st.write("First run: nothing to compare yet.")

# ------------------------------------------------------------------ sign-off
st.subheader("\u2705 Sign-off")
try:
    status = signoff_status(result["version_id"], BASE)
except SignoffError:
    status = {}
if status:
    st.dataframe(pd.DataFrame([{"Audience": a, "Status": s["status"], "Waiting for": ", ".join(s["pending"]) or "-"} for a, s in status.items()]),
                 hide_index=True, use_container_width=True)
    c1, c2 = st.columns([1, 2])
    role = c1.selectbox("Acting as", ["Finance Director", "CFO", "CEO"])
    if c1.button("Approve everything waiting for me", key="approve"):
        try:
            out = signoff_action(result["version_id"], role, "APPROVE", BASE)
            st.success(f"{role} approved: {', '.join(out['acted_on'])}." + (f" All released: {out['lock']['rows_appended']} rows locked into history." if "lock" in out else ""))
        except SignoffError as exc:
            st.warning(str(exc))
    reject_type = c2.radio("If rejecting, the reason is", ["WORDING", "NUMBERS"], horizontal=True)
    reason = c2.text_input("One-line reason")
    if c2.button("Reject", key="reject"):
        try:
            signoff_action(result["version_id"], role, "REJECT", BASE, reject_type, reason)
            st.info("Rejected and logged. WORDING: FP&A edits and resubmits. NUMBERS: Finance adjusts the report; drop the new export and run again.")
        except SignoffError as exc:
            st.warning(str(exc))
st.caption("Locked months are never overwritten. Next month starts from a fresh export and compares against the locked history.")
