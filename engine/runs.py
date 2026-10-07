"""Automatic run numbering: Finance never has to label or rename a file.

The first trial balance for a period is run 1 (first draft). When a different file is dropped for the same
period it becomes run 2 and is compared with run 1. Dropping the same file again keeps the run number
(for example after FP&A adds notes). Snapshots are stored under outputs/_runs/<period>/.
"""
import hashlib
import json

import pandas as pd


def _fingerprint(tb: pd.DataFrame) -> str:
    """Hash of the trial balance content (order independent)."""
    ordered = tb.sort_values(["ERP_Account_Code", "BU"])[["ERP_Account_Code", "BU", "Amount", "Memo"]]
    return hashlib.sha256(ordered.to_csv(index=False).encode()).hexdigest()


def register_run(runs_dir, tb: pd.DataFrame):
    """Record this export and return (run_no, previous_tb or None)."""
    runs_dir.mkdir(parents=True, exist_ok=True)
    registry_path = runs_dir / "runs.json"
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else []
    fingerprint = _fingerprint(tb)
    if registry and registry[-1]["hash"] == fingerprint:
        run_no = registry[-1]["run_no"]
    else:
        run_no = len(registry) + 1
        registry.append({"run_no": run_no, "hash": fingerprint})
        tb.to_csv(runs_dir / f"tb_run_{run_no:02d}.csv", index=False)
        registry_path.write_text(json.dumps(registry, indent=2))
    prev_file = runs_dir / f"tb_run_{run_no - 1:02d}.csv"
    previous = pd.read_csv(prev_file, dtype={"Period": str, "ERP_Account_Code": str, "BU": str}, keep_default_na=False) \
        if run_no > 1 and prev_file.exists() else None
    if previous is not None:
        previous["Amount"] = pd.to_numeric(previous["Amount"])
    return run_no, previous
