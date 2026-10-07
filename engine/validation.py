"""Stage 8 - Automated checks on AI drafts (the hallucination control).

1. Number check - every figure in the draft must appear in the pack the AI was given.
2. Forbidden-term check - audience-specific words that must not appear (e.g. 'budget' for investors).
3. Leak scan - no identifier may survive for the audience's redaction level.
Anything flagged goes to FP&A review; nothing is auto-corrected.
"""
import re

NUMBER_RE = re.compile(r"(?<![\w.])-?\$?\d[\d,]*(?:\.\d+)?%?")


def extract_numbers(text: str, skip_small_integers: bool = True) -> list:
    """Return (token, absolute value) for each figure in text.

    Bare integers up to 10 (list numbering, 'three items') are skipped on the draft side because
    they are not financial figures.
    """
    found = []
    for match in NUMBER_RE.finditer(text):
        token = match.group(0).rstrip(",")
        digits = token.replace("$", "").replace(",", "").replace("%", "").lstrip("-")
        try:
            value = float(digits)
        except ValueError:
            continue
        bare = "$" not in token and "%" not in token
        if skip_small_integers and bare and value == int(value) and value <= 10:
            continue
        found.append((token, value))
    return found


def check_numbers(narrative: str, pack_text: str) -> dict:
    """Compare every figure in the narrative with the pack. Returns passed flag and unmatched tokens.

    A figure matches if it is within 0.05 of a pack figure (0.5 for whole numbers, to allow rounding).
    """
    allowed = [v for _, v in extract_numbers(pack_text, skip_small_integers=False)]
    unmatched = []
    drafted = extract_numbers(narrative)
    for token, value in drafted:
        tol = 0.5 if value == int(value) else 0.05
        if not any(abs(value - p) <= tol for p in allowed):
            unmatched.append(token)
    return {"passed": not unmatched, "figures_checked": len(drafted), "unmatched": unmatched}


def check_forbidden_terms(narrative: str, terms: list) -> list:
    """Return the forbidden words/phrases found in the narrative (case-insensitive, whole word)."""
    return [t for t in terms if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", narrative, re.IGNORECASE)]


def run_output_checks(narrative: str, pack_text: str, forbidden_terms: list, redactor, level: str) -> dict:
    """Run all three checks and summarise. `passed` is True only if every check is clean."""
    numbers = check_numbers(narrative, pack_text)
    terms = check_forbidden_terms(narrative, forbidden_terms)
    leaks = redactor.find_leaks(narrative, level)
    return {"numbers": numbers, "forbidden_terms": terms, "leaks": leaks,
            "passed": bool(numbers["passed"] and not terms and not leaks)}


# ---------------------------------------------------------------- slide spec check
ALLOWED_LAYOUTS = {"Management": ("headline", "variance", "drivers", "cash", "questions"),
                   "Board": ("headline", "variance", "drivers", "cash"), "Investor": ("headline", "variance")}


def spec_text(output: dict) -> str:
    """All AI-written text in an output (commentary, analysis, slide titles and bullets) as one string."""
    parts = [output.get("commentary", "")]
    analysis = output.get("analysis", {})
    parts += [analysis.get("headline", "")] + [t for k in ("key_variances", "drivers", "unexplained", "questions") for t in analysis.get(k, [])]
    for slide in output.get("slides", []):
        parts += [slide.get("title", "")] + list(slide.get("bullets", []))
    return "\n".join(str(p) for p in parts)


def check_slide_spec(output: dict, audience: str, max_slides: int = 7, max_title: int = 70, max_bullets: int = 4,
                     max_bullet_chars: int = 160) -> list:
    """Validate the AI's JSON shape and slide limits. Returns a list of problems (empty = valid)."""
    problems = []
    if not isinstance(output.get("commentary"), str) or not output["commentary"].strip():
        problems.append("commentary is missing or empty")
    analysis = output.get("analysis")
    if not isinstance(analysis, dict) or not all(k in analysis for k in ("headline", "key_variances", "drivers", "unexplained", "questions")):
        problems.append("analysis is missing required keys")
    slides = output.get("slides")
    if not isinstance(slides, list) or not slides:
        return problems + ["slides is missing or empty"]
    if len(slides) > max_slides:
        problems.append(f"{len(slides)} slides exceeds the limit of {max_slides}")
    for i, slide in enumerate(slides, 1):
        if slide.get("layout") not in ALLOWED_LAYOUTS[audience]:
            problems.append(f"slide {i}: layout '{slide.get('layout')}' is not allowed for {audience}")
        if len(str(slide.get("title", ""))) > max_title or not str(slide.get("title", "")).strip():
            problems.append(f"slide {i}: title missing or longer than {max_title} characters")
        bullets = slide.get("bullets", [])
        if not isinstance(bullets, list) or len(bullets) > max_bullets:
            problems.append(f"slide {i}: more than {max_bullets} bullets")
        else:
            problems += [f"slide {i}: a bullet exceeds {max_bullet_chars} characters" for b in bullets if len(str(b)) > max_bullet_chars]
    return problems
