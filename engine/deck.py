"""Stage 9 - Build the PowerPoint deck from a pack and the AI's slide spec (baseline method: script + fixed layout).

The AI supplies only slide titles and bullets. Tables and KPI tiles come from the engine's pack, which has already
had exclusion rules and redaction applied, so a deck can never show more than its audience's pack. If
config/deck_template.pptx exists its theme and slide size are used (slides are drawn on its blank layout);
otherwise a plain 16:9 default is used. Every deck carries the DRAFT/NOT approved status line in its footer.
"""
import math

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

from engine import formatting as fmt
from engine.validation import check_numbers

STATUS_COLORS = {"On target": RGBColor(0x2E, 0x7D, 0x32), "Watch": RGBColor(0xE6, 0x9F, 0x00), "Off target": RGBColor(0xC6, 0x28, 0x28)}
NAVY, GREY = RGBColor(0x1F, 0x2D, 0x4D), RGBColor(0x55, 0x5B, 0x66)
FONT = "Arial"


def _new_presentation(template_path):
    """Open the template (if provided) or create a plain 16:9 presentation; return (prs, blank layout)."""
    if template_path and template_path.exists():
        prs = Presentation(str(template_path))
    else:
        prs = Presentation()
        prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    blank = next((l for l in prs.slide_layouts if not len(l.placeholders)), prs.slide_layouts[len(prs.slide_layouts) - 1])
    return prs, blank


def _text(slide, left, top, width, height, text, size, bold=False, color=NAVY, bullets=None):
    """Add a text box (one paragraph, or one bullet paragraph per item) with explicit font sizes."""
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = box.text_frame
    frame.word_wrap = True
    items = bullets if bullets is not None else [text]
    for i, item in enumerate(items):
        para = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
        run = para.add_run()
        run.text = (f"\u2022 {item}" if bullets is not None else item)
        run.font.size, run.font.bold, run.font.name, run.font.color.rgb = Pt(size), bold, FONT, color
        para.space_after = Pt(8)
    return box


def _table(slide, left, top, width, headers, rows, col_widths, font=11, row_h=0.32):
    """Add a formatted table; rows are lists of strings."""
    shape = slide.shapes.add_table(len(rows) + 1, len(headers), Inches(left), Inches(top), Inches(width), Inches(row_h * (len(rows) + 1)))
    table = shape.table
    for j, w in enumerate(col_widths):
        table.columns[j].width = Inches(w)
    for i, row in enumerate([headers] + rows):
        for j, value in enumerate(row):
            cell = table.cell(i, j)
            cell.text = str(value)
            for para in cell.text_frame.paragraphs:
                for run in para.runs:
                    run.font.size, run.font.name, run.font.bold = Pt(font), FONT, i == 0
                    run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF) if i == 0 else RGBColor(0x22, 0x22, 0x22)
            cell.fill.solid()
            cell.fill.fore_color.rgb = NAVY if i == 0 else (RGBColor(0xF3, 0xF5, 0xF8) if i % 2 else RGBColor(0xFF, 0xFF, 0xFF))
    return shape


def _footer(slide, status_line, number):
    _text(slide, 0.6, 7.0, 11.5, 0.3, f"{status_line}  |  Figures from the engine; text drafted by AI and reviewed by FP&A", 9, color=GREY)
    _text(slide, 12.2, 7.0, 0.6, 0.3, str(number), 9, color=GREY)


def _kpi_tiles(slide, kpis, left, top):
    """Up to 6 KPI tiles (2 columns x 3 rows) with a status colour.

    The label box allows for two lines: some labels (e.g. 'Gross margin (statutory)' next to
    'Adjusted gross margin (management basis)') are long enough to wrap at this tile width and font size.
    """
    for i, k in enumerate(kpis.head(6).itertuples()):
        x, y = left + (i % 2) * 3.05, top + (i // 2) * 1.7
        _text(slide, x, y, 2.9, 0.5, k.Label, 11, color=GREY)
        _text(slide, x, y + 0.45, 2.9, 0.5, k.Value_Text, 22, bold=True)
        _text(slide, x, y + 1.0, 2.9, 0.35, f"Target {k.Target_Text}: {k.Status}", 11, bold=True, color=STATUS_COLORS.get(k.Status, GREY))


def _variance_rows(pack, statement, horizon="BvA_Month", limit=8):
    df = pack["comparisons"]
    sub = df[(df["Statement"] == statement) & (df["Horizon"] == horizon) & (df["BU"] == "All")] if len(df) else df
    if sub.empty and len(df):
        sub = df[(df["Statement"] == statement) & (df["BU"] == "All")]
        sub = sub[sub["Horizon"] == sub["Horizon"].iloc[0]] if len(sub) else sub
    return [[r.Line, fmt.money(r.Actual), fmt.money(r.Comparator), fmt.money(r.Var_Amt), fmt.pct_fraction(r.Var_Pct), r.Favorability]
            for r in sub.head(limit).itertuples()]


def build_deck(pack: dict, output: dict, path, template_path=None) -> int:
    """Write the deck to `path` and return the number of slides.

    Defensively capped at MAX_SLIDES regardless of how many slides the AI returned: the automated check in
    stage 8 already flags an AI reply that exceeds the limit for FP&A review, but the deck that actually gets
    built and sent out must never itself exceed it, even if a live model didn't follow the instruction exactly.
    """
    from engine.config import MAX_SLIDES
    slides = output["slides"][:MAX_SLIDES]
    prs, blank = _new_presentation(template_path)
    status, n = pack["status_line"], 1
    title_slide = prs.slides.add_slide(blank)
    _text(title_slide, 0.8, 2.4, 11.5, 1.0, f"{pack['audience']} briefing", 40, bold=True)
    _text(title_slide, 0.8, 3.5, 11.5, 0.6, fmt.period_label(pack["period"]), 24, color=GREY)
    _text(title_slide, 0.8, 4.3, 11.5, 0.6, status, 14, color=GREY)
    for slide_spec in slides:
        n += 1
        slide = prs.slides.add_slide(blank)
        _text(slide, 0.6, 0.4, 12.1, 0.9, slide_spec["title"], 26, bold=True)
        layout, bullets = slide_spec["layout"], slide_spec["bullets"]
        if layout == "headline":
            _text(slide, 0.6, 1.5, 6.0, 4.9, "", 16, bullets=bullets)
            if len(pack["kpis"]):
                _kpi_tiles(slide, pack["kpis"], 7.0, 1.5)
        elif layout in ("variance", "cash"):
            _text(slide, 0.6, 1.5, 5.4, 4.9, "", 15, bullets=bullets)
            headers = ["Line", "Actual", "Comparator", "Variance", "Var %", "Direction"]
            rows = _variance_rows(pack, "P&L" if layout == "variance" else "CF")
            if layout == "cash":
                rows = (_variance_rows(pack, "BS", limit=4) + rows)[:9]
            if rows:
                _table(slide, 6.2, 1.5, 6.6, headers, rows, [1.9, 1.0, 1.0, 1.0, 0.7, 1.0], font=10)
        else:
            _text(slide, 0.6, 1.5, 12.0, 4.9, "", 18, bullets=bullets)
        _footer(slide, status, n)
    prs.save(str(path))
    return n


def check_deck(path, pack_text: str) -> dict:
    """Re-open a saved deck: estimate text overflow in every box and check all figures against the pack."""
    prs, overflow, texts = Presentation(str(path)), [], []
    for i, slide in enumerate(prs.slides, 1):
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                size = next((r.font.size.pt for p in shape.text_frame.paragraphs for r in p.runs if r.font.size), 18)
                chars_per_line = max(1, int(shape.width / 914400 * 72 / (size * 0.52)))
                lines = sum(max(1, math.ceil(len(p.text) / chars_per_line)) for p in shape.text_frame.paragraphs)
                need = lines * size * 1.2 / 72 + 0.1 * len(shape.text_frame.paragraphs)
                if need > shape.height / 914400 + 0.05:
                    overflow.append(f"slide {i}: text box may overflow ({lines} lines at {size:.0f}pt)")
                texts.append(shape.text_frame.text)
            if shape.has_table:
                texts += [c.text for r in shape.table.rows for c in r.cells]
    numbers = check_numbers("\n".join(texts), pack_text)
    return {"slides": len(prs.slides), "overflow": overflow, "numbers": numbers, "passed": not overflow and numbers["passed"]}
