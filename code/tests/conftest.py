"""Shared fixtures: build a tiny, fully synthetic dataset directory per test.

The real dataset is only used by the sample regression test; every other test
writes its own minimal files so each rule can be exercised in isolation.
"""

from __future__ import annotations

import csv
import shutil
import sys
from pathlib import Path

import pytest

CODE = Path(__file__).resolve().parent.parent
ROOT = CODE.parent
sys.path.insert(0, str(CODE))

from ghost_audit.engine import Auditor  # noqa: E402

SUBJECT = {
    "user_id": "user_101",
    "name": "Test Person",
    "email": "test.person@example.com",
    "phone": "+1 555 010 1010",
    "email_hash": "a" * 64,
    "updated_at": "2026-01-01T00:00:00Z",
}
ACTIVITY_COLS = ["activity_id", "event_time", "event_type", "source_system", "target_system",
                 "resource_id", "reported_result", "details"]
REQUEST_COLS = ["case_id", "email", "requested_at", "reported_status", "reported_at", "audit_at", "request_text"]


def _write_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def request(case_id="case_1", text="Please delete my account and all my personal data.",
            requested_at="2026-03-01T08:00:00Z", reported_status="COMPLETE",
            reported_at="2026-03-02T02:00:00Z", audit_at="2026-03-05T12:00:00Z", email=SUBJECT["email"]) -> dict:
    return {"case_id": case_id, "email": email, "requested_at": requested_at, "reported_status": reported_status,
            "reported_at": reported_at, "audit_at": audit_at, "request_text": text}


@pytest.fixture
def make_auditor(tmp_path):
    """make_auditor(evidence=[(file_id, relpath, captured_at, content)], activity=[...], requests=[...], images={})."""

    def build(evidence=(), activity=(), requests=(), image_results=None, audit_log=()):
        ds = tmp_path / "dataset"
        (ds / "evidence").mkdir(parents=True, exist_ok=True)
        _write_csv(ds / "user_identities.csv", list(SUBJECT), [SUBJECT])
        _write_csv(ds / "user_audit_log.csv", ["audit_id", "user_id", "event_time", "field_name", "old_value", "new_value"],
                   list(audit_log))
        _write_csv(ds / "system_activity.csv", ACTIVITY_COLS, [{c: a.get(c, "") for c in ACTIVITY_COLS} for a in activity])
        shutil.copy(ROOT / "dataset" / "deletion_rules.csv", ds / "deletion_rules.csv")
        manifest = []
        for file_id, rel, captured_at, content in evidence:
            p = ds / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                p.write_bytes(content)
            else:
                p.write_text(content, encoding="utf-8")
            manifest.append({"file_id": file_id, "file_path": rel, "origin": "test", "captured_at": captured_at})
        _write_csv(ds / "evidence_files.csv", ["file_id", "file_path", "origin", "captured_at"], manifest)
        req_file = ds / "deletion_requests.csv"
        _write_csv(req_file, REQUEST_COLS, list(requests))
        images = image_results or {}
        extractor = (lambda path: images.get(path.name)) if images else None
        return Auditor(ds, image_extractor=extractor, request_files=[req_file])

    return build


def sql_search_doc(resource_id="search_100", email=SUBJECT["email"], indexed_at="2026-02-01T00:00:00Z") -> str:
    """A one-row search index snapshot holding the subject's email."""
    return (
        "CREATE TABLE search_documents (resource_id TEXT, user_id TEXT, display_name TEXT, email TEXT, indexed_at TEXT);\n"
        f"INSERT INTO search_documents VALUES('{resource_id}','user_101','Test Person','{email}','{indexed_at}');\n"
    )
