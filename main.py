"""FP&A automation CLI.

Examples
  python main.py --mock                              run on data/input/trial_balance.xlsx with the offline mock AI
  python main.py --mock --tb-file <day 4 export>     a different file for the same period is run 2 and is compared automatically
  python main.py --abs 50000 --rel 0.10              change the materiality thresholds
  python main.py --sheets-workbook data/input/sheets_inputs.xlsx --mock
  python main.py --mock --demo-approve               simulate the approvals (history locks automatically on release)
  python main.py --lock 2026-06_r2                   manual roll-forward of a RELEASED run
  python main.py --check-ollama                      ping Ollama and list installed models (diagnostics)

stdout carries ONLY JSON (so other tools can parse it); progress messages go to stderr.
Exit code 0 = success, 2 = BLOCKED or FAILED, 3 = lock refused.
"""
import argparse
import json
import sys

from engine.config import (DEFAULT_ABS_THRESHOLD, DEFAULT_AI_TIMEOUT, DEFAULT_NUM_CTX, DEFAULT_OLLAMA_MODEL,
                           DEFAULT_OLLAMA_URL, DEFAULT_REL_THRESHOLD, REPORTING_PERIOD)
from engine.pipeline import approve_all, lock_after_release, run_pipeline, to_jsonable


def parse_args(argv=None):
    """Define and parse the command-line options."""
    p = argparse.ArgumentParser(description="FP&A multi-horizon variance, KPI and executive narrative engine")
    p.add_argument("--mock", action="store_true", help="use the offline mock AI instead of Ollama")
    p.add_argument("--tb-file", help="ERP trial balance export (default data/input/trial_balance.xlsx)")
    p.add_argument("--previous-tb-file", help="compare with this earlier export instead of the automatic run registry")
    p.add_argument("--period", default=REPORTING_PERIOD, help="reporting month YYYY-MM")
    p.add_argument("--abs", dest="abs_threshold", type=float, default=DEFAULT_ABS_THRESHOLD, help="absolute materiality threshold ($)")
    p.add_argument("--rel", dest="rel_threshold", type=float, default=DEFAULT_REL_THRESHOLD, help="relative materiality threshold (0.05 = 5%%)")
    p.add_argument("--reasons", help="optional revision-reasons CSV override")
    p.add_argument("--sheets-workbook", help="workbook exported from Google Sheets (mappings, notes, reasons, KPI targets)")
    p.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL)
    p.add_argument("--model", default=DEFAULT_OLLAMA_MODEL)
    p.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX, help="Ollama context window in tokens")
    p.add_argument("--ai-timeout", type=int, default=DEFAULT_AI_TIMEOUT, help="seconds to wait per AI call")
    p.add_argument("--check-ollama", action="store_true", help="just ping the Ollama server, list its models and exit")
    p.add_argument("--base-dir", help="project folder (default: this folder)")
    p.add_argument("--ai-json", action="append", default=[], metavar="AUDIENCE=FILE",
                   help="use AI output produced on another platform for an audience, e.g. --ai-json Board=board.json (repeatable)")
    p.add_argument("--no-deck", action="store_true", help="skip building the PowerPoint decks")
    p.add_argument("--demo-approve", action="store_true", help="simulate Finance Director, CFO and CEO approvals (locks history on release)")
    p.add_argument("--lock", metavar="VERSION_ID", help="roll forward manually: lock a RELEASED run (e.g. 2026-06_r2) into history")
    return p.parse_args(argv)


def main(argv=None) -> int:
    """Run the requested action and print JSON. Returns the process exit code."""
    args = parse_args(argv)
    if args.check_ollama:
        from engine.llm_narrative import check_ollama_connection
        print(json.dumps(check_ollama_connection(args.ollama_url), indent=2))
        return 0
    if args.lock:
        try:
            print(json.dumps(lock_after_release(args.lock, args.base_dir), indent=2))
            return 0
        except Exception as exc:  # lock must never half-succeed
            print(json.dumps({"status": "LOCK_REFUSED", "error": str(exc)}, indent=2))
            return 3
    result = run_pipeline(tb_file=args.tb_file, use_mock=args.mock, abs_threshold=args.abs_threshold, rel_threshold=args.rel_threshold,
                          base_dir=args.base_dir, period=args.period, previous_tb_file=args.previous_tb_file, reasons_path=args.reasons,
                          sheets_workbook=args.sheets_workbook, ollama_url=args.ollama_url, model=args.model, num_ctx=args.num_ctx,
                          ai_timeout=args.ai_timeout, build_decks=not args.no_deck, ai_override=dict(x.split('=', 1) for x in args.ai_json))
    if args.demo_approve and result["status"] == "SUCCESS":
        result["signoff_demo"] = approve_all(result["version_id"], args.base_dir)
    print(json.dumps(to_jsonable(result), indent=2))
    return 0 if result["status"] == "SUCCESS" else 2


if __name__ == "__main__":
    sys.exit(main())
