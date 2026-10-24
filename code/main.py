"""Entry point: audit every request in dataset/deletion_requests.csv and write ./output.csv.

Usage:
    python code/main.py                        # full run -> output.csv at repo root
    python code/main.py --requests sample      # run the 32 public samples -> code/evaluation/sample_predictions.csv
    python code/main.py --no-llm               # never call a model API; cached screenshot results only
    python code/main.py --list-gemini-models   # flash models available to GEMINI_API_KEY
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghost_audit.engine import Auditor  # noqa: E402
from ghost_audit.llm import ClaudeHelper, list_gemini_models, load_dotenv  # noqa: E402

USAGE_FILE = ROOT / "code" / "evaluation" / "usage_summary.json"


def run(requests_file: Path, out_file: Path, trace_file: Path | None = None, use_llm: bool = True) -> list[dict]:
    """Audit every request in requests_file and write the submission CSV to out_file."""
    load_dotenv(ROOT)
    helper = ClaudeHelper(ROOT / "code" / "cache", allow_network=use_llm)
    auditor = Auditor(ROOT / "dataset", image_extractor=helper.read_screenshot, request_files=[ROOT / "dataset" / "deletion_requests.csv", ROOT / "dataset" / "sample_deletion_requests.csv"])
    with open(requests_file, newline="", encoding="utf-8") as fh:
        cases = list(csv.DictReader(fh))
    rows, traces = [], []
    for case in sorted(cases, key=lambda c: c["case_id"]):
        status, steps, trace = auditor.audit(case)
        rows.append({
            "case_id": case["case_id"],
            "deletion_status": status,
            "steps_json": json.dumps(steps, separators=(",", ":"), ensure_ascii=False),
        })
        traces.append(trace)
    with open(out_file, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["case_id", "deletion_status", "steps_json"])
        w.writeheader()
        w.writerows(rows)
    if trace_file:
        trace_file.write_text(json.dumps(traces, indent=1, default=sorted), encoding="utf-8")
    usage = {"requests": len(rows), "per_model": helper.meter.summary(), "calls": helper.meter.calls}
    if out_file.name == "output.csv":
        USAGE_FILE.write_text(json.dumps(usage, indent=1), encoding="utf-8")
    if auditor.skipped:
        print(f"note: {len(auditor.skipped)} evidence files not parsed: {auditor.skipped}", file=sys.stderr)
    return rows


def main() -> None:
    """CLI entry point (see module docstring for usage)."""
    ap =argparse.ArgumentParser()
    ap.add_argument("--requests", choices=["full", "sample"], default="full")
    ap.add_argument("--no-llm", action="store_true", help="never call the API; use cached vision results only")
    ap.add_argument("--list-gemini-models", action="store_true",
                    help="print Gemini 'flash' models that support generateContent, then exit")
    args = ap.parse_args()
    if args.list_gemini_models:
        load_dotenv(ROOT)
        names = list_gemini_models()
        if names is None:
            sys.exit("could not list Gemini models (is GEMINI_API_KEY set?)")
        print("\n".join(names))
        return
    if args.requests == "sample":
        out = run(ROOT / "dataset" / "sample_deletion_requests.csv",
                  ROOT / "code" / "evaluation" / "sample_predictions.csv",
                  ROOT / "code" / "evaluation" / "sample_trace.json", use_llm=not args.no_llm)
    else:
        out = run(ROOT / "dataset" / "deletion_requests.csv", ROOT / "output.csv", use_llm=not args.no_llm)
    print(f"wrote {len(out)} rows")


if __name__ == "__main__":
    main()
