"""Stage 7 - AI analysis and slide text with a local Ollama model (or a data-driven mock for testing).

The AI returns ONE JSON object: commentary (prose), analysis (headline, variances, drivers, unexplained,
questions) and slides (layout, title, bullets). The deck builder adds tables and KPI tiles from engine data.

Modes reported with every result:
  LIVE          - written by the local Ollama model
  MOCK          - deterministic text built from the pack (offline demo / CI)
  MOCK_FALLBACK - live mode failed (connection, HTTP or invalid JSON twice); the text says so prominently
Live calls send num_ctx explicitly: Ollama's default context window is small and truncates silently.
"""
import json
import re

import requests

from engine import formatting as fmt
from engine.config import DEFAULT_AI_TIMEOUT, DEFAULT_NUM_CTX, DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_URL, PL_SUBTOTALS


# ---------------------------------------------------------------- pack lookups
def _row(pack: dict, horizon: str, line: str, bu: str = "All", statement: str = "P&L"):
    """The comparison row for a statement/horizon/line/slice, or None if the pack does not contain it."""
    df = pack["comparisons"]
    if df.empty:
        return None
    sub = df[(df["Statement"] == statement) & (df["Horizon"] == horizon) & (df["Line"] == line) & (df["BU"] == bu)]
    return sub.iloc[0] if len(sub) else None


def _ratio(pack: dict, set_name: str, metric: str):
    return pack["ratios"].get(set_name, {}).get(metric)


def _var_text(r, word: str) -> str:
    """'$A against budget of $C, a variance of $V (p%), Favorable'."""
    return f"{fmt.money(r['Actual'])} against {word} of {fmt.money(r['Comparator'])}, a variance of {fmt.money(r['Var_Amt'])} ({fmt.pct_fraction(r['Var_Pct'])}), {r['Favorability']}"


def _short_var(r, word: str) -> str:
    """Compact bullet form for slides."""
    return f"{r['Line']}: {fmt.money(r['Actual'])} vs {word} {fmt.money(r['Comparator'])} ({fmt.money(r['Var_Amt'])}, {fmt.pct_fraction(r['Var_Pct'])}), {r['Favorability']}."


def _material_rows(pack: dict, horizon: str = "BvA_Month", limit: int = 4) -> list:
    df = pack["comparisons"]
    if df.empty:
        return []
    sub = df[(df["Statement"] == "P&L") & (df["Horizon"] == horizon) & (df["BU"] == "All") & df["Material"] & ~df["Line"].isin(PL_SUBTOTALS)]
    return [r for _, r in sub.reindex(sub["Var_Amt"].abs().sort_values(ascending=False).index).head(limit).iterrows()]


def _clip(text: str, limit: int = 160) -> str:
    """Shorten a bullet to the slide limit at a word boundary (applied after redaction, so it cannot expose anything)."""
    if len(text) <= limit:
        return text
    return text[:limit - 3].rsplit(" ", 1)[0].rstrip(",;:") + "..."


def _movement(var_pct) -> str:
    """Plain movement for external readers: 'up 2.9%', 'down 18.2%' or 'unchanged'."""
    if var_pct is None or var_pct != var_pct:
        return "not comparable"
    return "unchanged" if abs(var_pct) < 0.0005 else f"{'up' if var_pct > 0 else 'down'} {fmt.pct_fraction(abs(var_pct))}"


def _driver_notes(pack: dict) -> list:
    return [n for n in pack["notes"] if n["Source"] == "Driver note"]


def _kpi_sentences(pack: dict, limit: int = 3) -> list:
    k = pack["kpis"]
    if not len(k):
        return []
    flagged = k[k["Status"] != "On target"].head(limit)
    return [f"{r.Label} was {r.Value_Text} against a target of {r.Target_Text}: {r.Status}." for r in flagged.itertuples()]


def _wc_text(pack: dict) -> str:
    dso, dpo, drag = (_ratio(pack, "Month", k) for k in ("DSO_Days", "DPO_Days", "Working_Capital_Drag_Days"))
    if dso is None:
        return ""
    return f"DSO was {fmt.days(dso)}, DPO was {fmt.days(dpo)} and working capital drag was {fmt.days(drag)}."


def _cash_bullets(pack: dict) -> list:
    out = []
    cash = _row(pack, "BvA_Month", "Cash", statement="BS")
    ocf, fcf = _row(pack, "BvA_Month", "Operating Cash Flow", statement="CF"), _row(pack, "BvA_Month", "Free Cash Flow", statement="CF")
    if cash is not None:
        out.append(f"Cash balance was {fmt.money(cash['Actual'])} against budget of {fmt.money(cash['Comparator'])}, {cash['Favorability']}.")
    if ocf is not None:
        out.append(f"Operating cash flow was {fmt.money(ocf['Actual'])} against budget of {fmt.money(ocf['Comparator'])}, {ocf['Favorability']}.")
    if fcf is not None:
        out.append(f"Free cash flow was {fmt.money(fcf['Actual'])} against budget of {fmt.money(fcf['Comparator'])}, {fcf['Favorability']}.")
    return out


# ---------------------------------------------------------------- mock output
def mock_output(pack: dict) -> dict:
    """Build commentary, analysis and slide text for the pack's audience from pack rows only (no hard-coded claims)."""
    audience, label = pack["audience"], fmt.period_label(pack["period"])
    op = _row(pack, "BvA_Month", "Operating Profit")
    material = _material_rows(pack)
    notes = _driver_notes(pack)
    note_text = " ".join(f"{n['Applies_To']}: {n['Text']}" for n in notes[:4])
    covered = {n["Applies_To"].lower() for n in notes}
    unexplained = [r["Line"] for r in material if r["Line"].lower() not in covered] if audience != "Investor" else []
    headline = f"Operating Profit was {_var_text(op, 'budget')}." if op is not None else f"{audience} results for {label}."
    parts = [f"[MOCK AI] {audience} commentary for {label}. Status: {pack['status_line']}.", headline]
    slides, questions = [], []

    if audience == "Management":
        agm, gm = _ratio(pack, "Month", "Adjusted_Gross_Margin_%"), _ratio(pack, "Month", "Gross_Margin_%")
        if agm is not None and gm is not None:
            parts.append(f"Adjusted gross margin (management basis) was {fmt.pct_points(agm)} versus statutory gross margin of {fmt.pct_points(gm)}.")
        if material:
            parts.append("Material variances against budget. " + " ".join(_short_var(r, "budget") for r in material))
        bu_rows = pack["comparisons"][(pack["comparisons"]["BU"] != "All") & (pack["comparisons"]["Horizon"] == "BvA_Month")] if not pack["comparisons"].empty else []
        if len(bu_rows):
            parts.append("Operating Profit against budget by business unit. " + " ".join(f"{r.BU}: {fmt.money(r.Var_Amt)} ({fmt.pct_fraction(r.Var_Pct)}), {r.Favorability}." for r in bu_rows.itertuples()))
        parts += pack["signals"][:2]
        parts += _kpi_sentences(pack)
        parts += _cash_bullets(pack)[:2]
        parts.append("Drivers provided by FP&A. " + note_text if note_text else "Driver not yet provided.")
        questions = [f"What is the plan to address {line}?" for line in unexplained] or []
        questions += [f"Driver not yet provided for {line}." for line in unexplained]
        layouts = ["headline", "variance", "drivers", "cash", "questions"]
    elif audience == "Board":
        rev = _row(pack, "BvA_Month", "Total Revenue")
        if rev is not None:
            parts.append(f"Total Revenue was {_var_text(rev, 'budget')}.")
        gm, pgm = _ratio(pack, "Month", "Gross_Margin_%"), _ratio(pack, "Prior_Month", "Gross_Margin_%")
        if gm is not None:
            parts.append(f"Gross margin was {fmt.pct_points(gm)}" + (f" compared with {fmt.pct_points(pgm)} in the prior month." if pgm is not None else "."))
        if material:
            parts.append("Statutory lines outside the materiality thresholds. " + " ".join(_short_var(r, "budget") for r in material[:3]))
        parts += pack["signals"][:2] + _kpi_sentences(pack) + _cash_bullets(pack)[:2]
        if _wc_text(pack):
            parts.append(_wc_text(pack))
        if note_text:
            parts.append("Drivers provided. " + note_text)
        if pack["revision_note"]:
            parts.append("Data-quality note. " + pack["revision_note"])
        layouts = ["headline", "variance", "drivers", "cash"]
    else:  # Investor
        for line in ("Total Revenue", "Gross Profit", "Total OpEx"):
            b = _row(pack, "BvA_Month", line)
            if b is not None:
                parts.append(f"{line} for the month was {fmt.money(b['Actual'])} against budget of {fmt.money(b['Comparator'])}, {b['Favorability']}.")
        for line in ("Total Revenue", "Operating Profit"):
            q, y = _row(pack, "QoQ", line), _row(pack, "YoY", line)
            if q is not None:
                parts.append(f"{line} for the quarter to date was {fmt.money(q['Actual'])}, {_movement(q['Var_Pct'])} versus the prior quarter.")
            if y is not None:
                parts.append(f"{line} for the month was {fmt.money(y['Actual'])}, {_movement(y['Var_Pct'])} versus the same month last year.")
        parts += _kpi_sentences(pack, limit=4)
        if note_text:
            parts.append("Context. " + note_text)
        layouts = ["headline", "variance"]

    kpi_bullets = _kpi_sentences(pack, limit=2)
    variance_title = "Largest variances against budget"
    if audience == "Investor":                                   # totals only: show the quarter and year movement instead
        variance_title = "Performance against budget, quarter and year"
        moves = []
        for line in ("Total Revenue", "Operating Profit"):
            q, y = _row(pack, "QoQ", line), _row(pack, "YoY", line)
            if q is not None:
                moves.append(f"{line}, quarter to date: {fmt.money(q['Actual'])}, {_movement(q['Var_Pct'])} on the prior quarter.")
            if y is not None:
                moves.append(f"{line}, month: {fmt.money(y['Actual'])}, {_movement(y['Var_Pct'])} on the same month last year.")
    variance_bullets = moves if audience == "Investor" and moves else [_short_var(r, "budget") for r in material[:4]] or ([] if op is None else [f"Operating Profit: {_var_text(op, 'budget')}."][:1])
    slide_map = {
        "headline": {"layout": "headline", "title": (f"Operating Profit {fmt.money(op['Actual'])} vs budget {fmt.money(op['Comparator'])}" if op is not None else f"{label} headline"),
                     "bullets": ([headline] + kpi_bullets)[:4]},
        "variance": {"layout": "variance", "title": variance_title, "bullets": variance_bullets},
        "drivers": {"layout": "drivers", "title": "What is driving the variances",
                    "bullets": ([f"{n['Applies_To']}: {n['Text']}" for n in notes] + [f"Driver not yet provided for {l}." for l in unexplained])[:4]},
        "cash": {"layout": "cash", "title": "Cash and balance sheet position", "bullets": _cash_bullets(pack)[:3] or ["Cash data not available."]},
        "questions": {"layout": "questions", "title": "Open points for owners", "bullets": questions[:4] or ["No open points."]},
    }
    slides = [{**slide_map[l], "bullets": [_clip(b) for b in slide_map[l]["bullets"]][:4]} for l in layouts if slide_map[l]["bullets"]]
    analysis = {"headline": headline, "key_variances": [_short_var(r, "budget") for r in material[:4]],
                "drivers": [f"{n['Applies_To']}: {n['Text']}" for n in notes[:4]] or [f"Driver not yet provided for {l}." for l in unexplained],
                "unexplained": [f"Driver not yet provided for {l}." for l in unexplained], "questions": questions[:4]}
    return {"commentary": " ".join(parts), "analysis": analysis, "slides": slides}


# ---------------------------------------------------------------- live mode
def estimate_tokens(text: str) -> int:
    """Rough token estimate (about 4 characters per token) used to warn before Ollama truncates silently."""
    return len(text) // 4


def check_ollama_connection(url: str, timeout: int = 5) -> dict:
    """Ping a local Ollama server and list its installed models. Never raises; returns a plain result dict.

    Used by the dashboard's "Test Ollama connection" button and the CLI's --check-ollama flag, so a connection
    or a wrong model name can be diagnosed before (or instead of) running the whole pipeline and getting a
    generic fallback.
    """
    try:
        reply = requests.get(f"{url.rstrip('/')}/api/tags", timeout=timeout)
        reply.raise_for_status()
        return {"ok": True, "models": [m.get("name", "") for m in reply.json().get("models", [])], "error": ""}
    except Exception as exc:
        return {"ok": False, "models": [], "error": f"{type(exc).__name__}: {exc}"}


def model_is_available(model: str, models: list) -> bool:
    """True if `model` (with or without a ':tag' suffix) matches one of the server's installed model names."""
    base = model.split(":")[0]
    return any(m == model or m.split(":")[0] == base for m in models)


def call_ollama(prompt: str, url: str, model: str, num_ctx: int, timeout: int = 180) -> str:
    """Send a prompt to a local Ollama server in JSON mode with an explicit context window; raises on any failure."""
    reply = requests.post(f"{url.rstrip('/')}/api/generate", timeout=timeout,
                          json={"model": model, "prompt": prompt, "stream": False, "format": "json",
                                "options": {"temperature": 0.2, "num_ctx": num_ctx}})
    reply.raise_for_status()
    return reply.json()["response"].strip()


def extract_json_object(text: str) -> str:
    """Return the first balanced top-level {...} object in text, tolerating stray prose around it.

    Ollama's "format": "json" guarantees syntactically valid JSON but not that the WHOLE reply is just the
    object (a smaller model can still prepend or append a sentence). Scanning for a balanced brace pair,
    aware of quoted strings, is more forgiving than assuming the entire response is the JSON document.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in the AI reply.")
    depth, in_string, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("Unbalanced JSON object in the AI reply.")


def parse_ai_json(text: str) -> dict:
    """Parse the model's reply as JSON (tolerating markdown fences and surrounding prose).

    Only 'commentary' and a well-formed 'slides' list are required. A smaller model very often nails the prose
    but drops an optional analysis sub-key (e.g. "questions") or the whole analysis object; those are filled
    with safe empty defaults rather than discarding a usable reply. A missing/invalid 'slides' list is still an
    error here (it is retried), but downstream the slide-spec check flags an EMPTY slides list for FP&A review
    rather than throwing the commentary away too.
    """
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = json.loads(extract_json_object(cleaned))
    if not isinstance(data, dict):
        raise ValueError("AI reply JSON is not an object.")
    if not isinstance(data.get("commentary"), str) or not data["commentary"].strip():
        raise ValueError("AI reply is missing a non-empty 'commentary' string.")
    if "slides" not in data or not isinstance(data["slides"], list):
        raise ValueError("AI reply is missing a 'slides' list.")
    analysis = data.get("analysis")
    if not isinstance(analysis, dict):
        analysis = {}
    analysis.setdefault("headline", "")
    for key in ("key_variances", "drivers", "unexplained", "questions"):
        value = analysis.get(key)
        analysis[key] = value if isinstance(value, list) else []
    data["analysis"] = analysis
    return data


def generate_output(pack: dict, pack_text: str, use_mock: bool, url: str = DEFAULT_OLLAMA_URL,
                    model: str = DEFAULT_OLLAMA_MODEL, num_ctx: int = DEFAULT_NUM_CTX, retries: int = 2,
                    timeout: int = DEFAULT_AI_TIMEOUT) -> dict:
    """Produce commentary, analysis and slides. Returns those keys plus mode, error and prompt_tokens.

    Live failures never raise: they become a clearly labelled MOCK_FALLBACK so the run continues visibly, and
    `error` always carries the real exception message (shown in the dashboard) rather than a generic label.
    A connection failure (server not reachable at all) stops after one attempt, since retrying cannot help.
    A timeout or an invalid-JSON reply is retried, since both can be transient (model still loading, or a
    smaller model occasionally missing the schema on a long prompt).
    """
    tokens = estimate_tokens(pack_text)
    if use_mock:
        return {**mock_output(pack), "mode": "MOCK", "error": "", "prompt_tokens": tokens}
    last_error = ""
    for attempt in range(retries):
        try:
            nudge = "" if attempt == 0 else "\n\nYour previous reply could not be used. Return ONE valid JSON object with the exact keys commentary, analysis and slides, and nothing else."
            data = parse_ai_json(call_ollama(pack_text + nudge, url, model, num_ctx, timeout))
            return {**data, "mode": "LIVE", "error": "", "prompt_tokens": tokens}
        except requests.exceptions.ConnectionError as exc:
            last_error = f"ConnectionError: {exc}"
            break                                             # the server isn't reachable at all; retrying won't help
        except Exception as exc:                               # timeout, HTTP or JSON failure: worth a retry
            last_error = f"{type(exc).__name__}: {exc}"
    fallback = mock_output(pack)
    fallback["commentary"] = f"[FALLBACK - OLLAMA UNAVAILABLE OR INVALID REPLY, NOT AI OUTPUT] {fallback['commentary']}"
    return {**fallback, "mode": "MOCK_FALLBACK", "error": last_error, "prompt_tokens": tokens}
