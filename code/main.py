"""Entry point: audit every request in dataset/deletion_requests.csv and write ./output.csv.

Usage:
    python3 code/main.py                       # full run -> output.csv at repo root
    python3 code/main.py --requests sample     # run the 32 public samples -> code/evaluation/sample_predictions.csv
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


def run(requests_file: Path, out_file: Path, trace_file: Path | None = None) -> list[dict]:
    auditor = Auditor(ROOT / "dataset", request_files=[ROOT / "dataset" / "deletion_requests.csv", ROOT / "dataset" / "sample_deletion_requests.csv"])
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
    if auditor.skipped:
        print(f"note: {len(auditor.skipped)} evidence files not parsed: {auditor.skipped}", file=sys.stderr)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--requests", choices=["full", "sample"], default="full")
    args = ap.parse_args()
    if args.requests == "sample":
        out = run(ROOT / "dataset" / "sample_deletion_requests.csv",
                  ROOT / "code" / "evaluation" / "sample_predictions.csv",
                  ROOT / "code" / "evaluation" / "sample_trace.json")
    else:
        out = run(ROOT / "dataset" / "deletion_requests.csv", ROOT / "output.csv")
    print(f"wrote {len(out)} rows")


if __name__ == "__main__":
    main()
