"""Stage 6 - PDPA / PII redaction.

Redaction happens BEFORE text reaches the AI (on the pack) and again on the AI's output as a second
safety net. Three levels, set per audience in config/audience_policy.csv:

* none - nothing removed (internal Management pack)
* pii  - emails, phone numbers, bank/account numbers, national IDs, and named individuals
* full - pii plus customer and vendor names (from config/entity_master.csv) and any
         'Name Pte Ltd / Inc / Corp' pattern the master does not know about

Deny-list matching is case-insensitive and whole-word, longest name first.
"""
import re

import pandas as pd

PII_LABELS = ("EMAIL", "PHONE", "ACCOUNT", "NATIONAL_ID", "PERSON")
FULL_LABELS = PII_LABELS + ("CUSTOMER", "VENDOR", "COMPANY")

_PATTERNS = {
    "EMAIL": r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
    "PHONE": r"\+\d{1,3}[ -]?\d{3,4}[ -]?\d{3,4}(?:[ -]?\d{3,4})?",
    "ACCOUNT": r"\b\d{3}-\d{5,7}-\d\b|\b\d{4}-\d{4}-\d{4}\b|\b\d{10,16}\b",
    "NATIONAL_ID": r"\b[STFG]\d{7}[A-Z]\b",
    "COMPANY": r"\b(?:[A-Z][\w&'-]*\s){1,3}(?:Pte\.?\s?Ltd|Ltd|Inc|LLC|Corp|Limited)\b",
}
_ENTITY_LABEL = {"Customer": "CUSTOMER", "Vendor": "VENDOR", "Person": "PERSON"}


class Redactor:
    """Builds the redaction rules once from the entity master and applies them to any text."""

    def __init__(self, entity_master: pd.DataFrame):
        """entity_master columns: Entity_Type (Customer/Vendor/Person), Legal_Name, Aliases ('a|b')."""
        terms = {"CUSTOMER": [], "VENDOR": [], "PERSON": []}
        for row in entity_master.itertuples():
            label = _ENTITY_LABEL.get(row.Entity_Type)
            if label is None:
                continue
            names = [row.Legal_Name] + [a for a in str(row.Aliases).split("|") if a.strip()]
            terms[label].extend(n.strip() for n in names if n.strip())
        self._regex = {label: re.compile(p) for label, p in _PATTERNS.items()}
        for label, names in terms.items():
            if names:
                alternation = "|".join(re.escape(n) for n in sorted(set(names), key=len, reverse=True))
                self._regex[label] = re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)

    def _labels(self, level: str) -> tuple:
        """Which redaction categories apply at a level."""
        if level == "none":
            return ()
        if level == "pii":
            return PII_LABELS
        if level == "full":
            return FULL_LABELS
        raise ValueError(f"Unknown redaction level '{level}' (use none, pii or full).")

    def redact(self, text: str, level: str):
        """Return (redacted_text, counts_by_category)."""
        counts = {}
        for label in self._labels(level):
            if label in self._regex:
                text, n = self._regex[label].subn(f"[{label}_REDACTED]", text)
                if n:
                    counts[label] = n
        return text, counts

    def find_leaks(self, text: str, level: str) -> list:
        """List identifiers still present in text for the level (empty list = clean)."""
        leaks = []
        for label in self._labels(level):
            if label in self._regex:
                leaks += [m.group(0) for m in self._regex[label].finditer(text)]
        return leaks
