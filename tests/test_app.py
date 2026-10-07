"""Headless test of the Streamlit dashboard (skipped automatically if Streamlit is not installed)."""
import os
import shutil
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ["FPA_QUIET"] = "1"
ROOT = Path(__file__).resolve().parent.parent

try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # pragma: no cover
    AppTest = None


@unittest.skipIf(AppTest is None, "streamlit not installed")
class TestDashboard(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        for d in ("config", "data"):
            shutil.copytree(ROOT / d, self.ws / d)
        (self.ws / "outputs").mkdir()
        os.environ["FPA_BASE_DIR"] = str(self.ws)

    def tearDown(self):
        os.environ.pop("FPA_BASE_DIR", None)
        shutil.rmtree(self.ws, ignore_errors=True)

    def app(self):
        return AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)

    @staticmethod
    def run_button(at):
        """The sidebar 'Run workflow' button, found by label rather than position (the sidebar also has a
        'Test Ollama connection' button, so indexing by position is fragile)."""
        return [b for b in at.sidebar.button if b.label == "Run workflow"][0]

    def test_first_load_shows_instructions(self):
        at = self.app().run()
        self.assertFalse(at.exception)
        self.assertTrue(any("Run workflow" in i.value for i in at.info))

    def test_narrative_text_is_not_mangled_by_markdown_latex_rendering(self):
        """Bug: st.write() on text full of '$' let Streamlit's markdown parse pairs of '$' as LaTeX, eating
        spaces ('vs budget' -> 'vsbudget') and rendering numbers in an italic math font. safe_markdown escapes
        the characters markdown treats specially before rendering, so the text comes out as plain readable prose."""
        at = self.app().run()
        self.run_button(at).click().run()                       # Day 3
        at.sidebar.selectbox[0].select("Day 4 sample (after finance adjustments)").run()
        self.run_button(at).click().run()                       # Day 4
        self.assertFalse(at.exception)
        bodies = [m.value for m in at.markdown]
        self.assertTrue(any("vs budget" in b or "against budget" in b for b in bodies), "no readable 'vs/against budget' text found in any markdown block")
        self.assertFalse(any("vsbudget" in b for b in bodies), "a markdown block still shows the LaTeX-mangled 'vsbudget'")

    def test_slide_titles_render_bold_not_as_literal_asterisks(self):
        """Regression: an earlier fix for the LaTeX bug escaped the WHOLE composed string, including the "**"
        and "_" the app itself adds for bold/italic slide titles, so a title rendered as the literal text
        "**Operating Profit**" instead of bold "Operating Profit". Titles must render as real markdown bold
        (the "**" must NOT appear as literal characters), while a "$" inside the AI's own title text must
        still show up literally rather than being parsed as LaTeX."""
        at = self.app().run()
        self.run_button(at).click().run()
        at.sidebar.selectbox[0].select("Day 4 sample (after finance adjustments)").run()
        self.run_button(at).click().run()
        self.assertFalse(at.exception)
        bodies = [m.value for m in at.markdown]
        # The broken behaviour rendered our own bold/italic markup as literal characters: "\*\*Title\*\*  _\(headline\)_".
        self.assertFalse(any("\\*\\*" in b or "_\\(" in b for b in bodies), "a slide title's own ** / _ markup was itself escaped, so it would render as literal asterisks/underscores")
        title_blocks = [b for b in bodies if b.startswith("**") and "_(" in b]
        self.assertTrue(title_blocks, "no slide-title markdown block found (expected '**Title**  _(layout)_')")

    def test_reconciliation_summary_renders_bold_not_mangled_by_dollar_signs(self):
        """Regression: a second, separate occurrence of the LaTeX-rendering bug (found after the first fix) -
        the 'Reconciliation and controls' summary line embeds two money-formatted values (each containing a
        literal '$') inside its own '**bold**' markup, the same pattern that broke slide titles earlier. Both
        the '$' values and the app's own bold markers must render correctly, not as mangled math text."""
        at = self.app().run()
        self.run_button(at).click().run()
        self.assertFalse(at.exception)
        bodies = [m.value for m in at.markdown]
        recon = [b for b in bodies if "balance sheet difference" in b]
        self.assertTrue(recon, "no reconciliation summary block found")
        for b in recon:
            self.assertFalse("\\*\\*" in b, f"reconciliation summary's own ** markup was escaped: {b!r}")
            self.assertTrue(b.count("**") >= 6, f"expected three bold spans (6 ** markers): {b!r}")  # True/False + 2 money values
            self.assertIn("$", b)  # a real dollar amount, not swallowed into LaTeX

    def test_run_day3_then_day4_then_approve_everything(self):
        at = self.app().run()
        self.run_button(at).click().run()                       # Day 3 sample selected by default
        self.assertFalse(at.exception)
        self.assertTrue(any("Run 1 complete" in s.value for s in at.success))
        at.sidebar.selectbox[0].select("Day 4 sample (after finance adjustments)").run()
        self.run_button(at).click().run()
        self.assertTrue(any("Run 2 complete" in s.value for s in at.success))
        self.assertGreaterEqual(len(at.metric), 8)              # KPI tiles
        for role in ("Finance Director", "CFO", "CEO"):
            sel = [s for s in at.selectbox if s.label == "Acting as"][0]
            sel.select(role).run()
            [b for b in at.button if b.key == "approve"][0].click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any("All released" in s.value for s in at.success), [s.value for s in at.success])
        self.assertIn("2026-06", set(__import__("pandas").read_csv(self.ws / "data" / "history" / "actuals_history.csv", dtype=str)["Period"]))


if __name__ == "__main__":
    unittest.main()
