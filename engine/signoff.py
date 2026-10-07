"""Stages 10-11 - Sign-off per audience, with logged rejections.

Approval path (config.APPROVAL_PATH): Management and Board need the Finance Director and the CFO (either order);
the Investor deck also needs the CEO after both. One review session approves for every audience that needs the role.
A rejection must say why:
* WORDING -> back to FP&A edit; sign-off restarts (same numbers).
* NUMBERS -> back to the finance review; numbers are re-locked as a NEW run and everything regenerates; sign-off restarts.
Any edit after an approval clears the approvals. Every action is logged.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd

from engine.config import APPROVAL_PATH

PENDING, RELEASED, FPA_EDIT, FINANCE_REVIEW = "PENDING", "RELEASED", "FPA_EDIT", "FINANCE_REVIEW"


class SignoffError(Exception):
    """Raised when an approval or rejection breaks the sign-off rules."""


@dataclass
class SignoffState:
    """Sign-off position for one audience's pack version, plus its action log."""
    version_id: str
    audience: str
    status: str = PENDING
    approvals: list = field(default_factory=list)
    log: list = field(default_factory=list)


def _record(state, step, role, decision, reject_type="", reason=""):
    state.log.append({"Timestamp": datetime.now().isoformat(timespec="seconds"), "Version_ID": state.version_id,
                      "Audience": state.audience, "Step": step, "Role": role, "Decision": decision,
                      "Reject_Type": reject_type, "Reason": reason})


def pending_roles(state: SignoffState) -> list:
    """Roles that may act now: the first approval group that is not yet complete (empty when released)."""
    if state.status != PENDING:
        return []
    for group in APPROVAL_PATH[state.audience]:
        missing = [r for r in group if r not in state.approvals]
        if missing:
            return missing
    return []


def new_state(version_id: str, audience: str) -> SignoffState:
    """Start sign-off for an audience's pack."""
    state = SignoffState(version_id, audience)
    _record(state, "Submitted", "FP&A", "SUBMITTED")
    return state


def approve(state: SignoffState, role: str) -> SignoffState:
    """Approve as `role` if that role is allowed to act now."""
    if state.status == RELEASED:
        raise SignoffError("Already released.")
    if role not in pending_roles(state):
        raise SignoffError(f"{role} cannot approve the {state.audience} pack now; waiting for {pending_roles(state) or state.status}.")
    state.approvals.append(role)
    _record(state, "Approval", role, "APPROVED")
    if not pending_roles(state):
        state.status = RELEASED
    return state


def reject(state: SignoffState, role: str, reject_type: str, reason: str) -> SignoffState:
    """Reject with a required type (WORDING or NUMBERS) and reason; routes the pack back accordingly."""
    if reject_type not in ("WORDING", "NUMBERS"):
        raise SignoffError("Reject type must be WORDING or NUMBERS.")
    if not reason.strip():
        raise SignoffError("A rejection needs a logged reason.")
    if role not in pending_roles(state):
        raise SignoffError(f"{role} cannot reject the {state.audience} pack now.")
    _record(state, "Rejection", role, "REJECTED", reject_type, reason)
    state.approvals = []
    state.status = FPA_EDIT if reject_type == "WORDING" else FINANCE_REVIEW
    return state


def resubmit(state: SignoffState, new_version_id: str = "") -> SignoffState:
    """Resubmit after a rejection. A NUMBERS rejection needs a NEW version id (numbers re-locked)."""
    if state.status == FPA_EDIT:
        _record(state, "Resubmitted", "FP&A", "SUBMITTED", reason="Wording corrected; sign-off restarts")
    elif state.status == FINANCE_REVIEW:
        if not new_version_id or new_version_id == state.version_id:
            raise SignoffError("A numbers rejection needs a new run (new version id) before resubmitting.")
        state.version_id = new_version_id
        _record(state, "Resubmitted", "Finance", "SUBMITTED", reason="Numbers re-locked; packs regenerated")
    else:
        raise SignoffError(f"Nothing to resubmit while status is {state.status}.")
    state.status, state.approvals = PENDING, []
    return state


def record_edit(state: SignoffState, editor: str, note: str) -> SignoffState:
    """Any edit after approvals clears them and restarts sign-off."""
    if state.approvals or state.status == RELEASED:
        _record(state, "Edit", editor, "EDITED", reason=f"{note} (approvals reset)")
        state.approvals, state.status = [], PENDING
    return state


def save_state(state: SignoffState, path) -> None:
    Path(path).write_text(json.dumps(state.__dict__, indent=2), encoding="utf-8")


def load_state(path) -> SignoffState:
    return SignoffState(**json.loads(Path(path).read_text(encoding="utf-8")))


def log_dataframe(states) -> pd.DataFrame:
    """Action log of one state or a list of states as a table."""
    states = states if isinstance(states, (list, tuple)) else [states]
    return pd.DataFrame([row for s in states for row in s.log])
