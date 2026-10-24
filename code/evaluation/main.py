"""Score predictions against labelled answers (default: the 32 public samples).

Metrics:
  - case accuracy:     deletion_status exact match
  - step P/R/F1:       multiset match of (phase, system_id, resource_id, operation, state, evidence_id)
  - field accuracy:    per-field agreement of steps aligned by position
  - sequence accuracy: steps_json identical (after JSON parsing) for the case
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FIELDS = ["phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def load(path: Path) -> dict:
    with open(path, newline="", encoding="utf-8") as fh:
        return {r["case_id"]: r for r in csv.DictReader(fh)}


def key(step: dict) -> tuple:
    return tuple(step.get(f) for f in FIELDS)


def score(pred: dict, gold: dict, verbose: bool = False) -> dict:
    status_ok = tp = fp = fn = seq_ok = 0
    field_hits, field_total = Counter(), 0
    failures = []
    for cid, g in sorted(gold.items()):
        p = pred.get(cid, {"deletion_status": "", "steps_json": "[]"})
        gs, ps = json.loads(g["steps_json"]), json.loads(p["steps_json"])
        status_ok += p["deletion_status"] == g["deletion_status"]
        gc, pc = Counter(map(key, gs)), Counter(map(key, ps))
        inter = sum((gc & pc).values())
        tp += inter
        fp += sum(pc.values()) - inter
        fn += sum(gc.values()) - inter
        same = [key(s) for s in gs] == [key(s) for s in ps] and p["deletion_status"] == g["deletion_status"]
        seq_ok += same
        for i in range(max(len(gs), len(ps))):
            field_total += 1
            for f in FIELDS:
                if i < len(gs) and i < len(ps) and gs[i].get(f) == ps[i].get(f):
                    field_hits[f] += 1
        if not same:
            failures.append((cid, g, p, gc - pc, pc - gc))
    n = len(gold)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    res = {
        "cases": n,
        "status_accuracy": status_ok / n,
        "step_precision": prec,
        "step_recall": rec,
        "step_f1": 2 * prec * rec / (prec + rec) if prec + rec else 0.0,
        "sequence_accuracy": seq_ok / n,
        "field_accuracy": {f: field_hits[f] / field_total for f in FIELDS} if field_total else {},
    }
    if verbose:
        for cid, g, p, missing, extra in failures:
            print(f"--- {cid}: gold={g['deletion_status']} pred={p['deletion_status']}")
            for m in missing:
                print("   missing", m)
            for e in extra:
                print("   extra  ", e)
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", default=str(ROOT / "code" / "evaluation" / "sample_predictions.csv"))
    ap.add_argument("--gold", default=str(ROOT / "dataset" / "sample_output.csv"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    res = score(load(Path(args.pred)), load(Path(args.gold)), args.verbose)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    sys.exit(main())
