"""Unit tests: intent classification, evidence parsers, step ordering, retention parsing."""

from __future__ import annotations

import json

import pytest

from ghost_audit import intent
from ghost_audit.engine import order_steps, retention_until
from ghost_audit.evidence import parse_json, parse_log, parse_pipe_text, parse_sql
from ghost_audit.llm import parse_gemini_response, screenshot_to_text

# --------------------------------------------------------------------------- intent


@pytest.mark.parametrize("text", [
    "Please delete my account and every record that holds my data.",
    "I would like the account associated with this address to be erased.",
    "Merci de procéder à la suppression de mon compte.",
    "Solicito la eliminación de mi cuenta.",
    "Bitte löschen Sie mein Konto.",
    "계정을 삭제해 주세요.",
])
def test_intent_deletion(text):
    assert intent.classify(text)[0] is True


@pytest.mark.parametrize("text", [
    "Please unsubscribe me from the newsletter but keep the account.",
    "Could you send a copy of my data in a portable format?",
    "My surname is misspelled, please correct it.",
    "Stop the promotional emails. I am not asking for deletion.",
])
def test_intent_not_deletion(text):
    assert intent.classify(text)[0] is False


@pytest.mark.xfail(strict=True, reason="known limitation: the export cue 'copy of my data' outranks a deletion verb; "
                                       "no request in the dataset uses this phrasing (see evaluation_report.md)")
def test_intent_deletion_mentioning_copies():
    assert intent.classify("Please delete my account and every copy of my data.")[0] is True


def test_negation_beats_deletion_keyword():
    # "delete" appears, but the explicit negation wins.
    assert intent.classify("Don't delete anything, just unsubscribe me.")[0] is False


# --------------------------------------------------------------------------- parsers


def test_parse_sql(tmp_path):
    p = tmp_path / "snap.sql"
    p.write_text(
        "CREATE TABLE customer_profiles_primary (resource_id TEXT, user_id TEXT, email TEXT, updated_at TEXT);\n"
        "INSERT INTO customer_profiles_primary VALUES('account_777','user_777','x@example.com','2026-01-02T00:00:00Z');\n",
        encoding="utf-8")
    out = parse_sql(p, "ev_1", "2026-01-03T00:00:00Z")
    rows = [o for o in out if "resource_id" in o]
    cov = [o for o in out if o.get("kind") == "coverage"]
    assert len(rows) == 1 and rows[0]["resource_id"] == "account_777"
    assert rows[0]["system"] == "account_service"
    # A snapshot reflects state at capture time, not at the row's last write.
    assert rows[0]["ts"] == "2026-01-03T00:00:00Z"
    assert "account_service" in cov[0]["covers"]


def test_parse_json(tmp_path):
    p = tmp_path / "bundle.json"
    p.write_text(json.dumps({"snapshots": [{"data_store": "export_service", "captured_at": "2026-02-01T00:00:00Z",
                                            "records": [{"resource_id": "export_55", "owner_email": "x@example.com",
                                                         "meta": {"download_available": False}}]}]}), encoding="utf-8")
    out = parse_json(p, "ev_2", "2026-02-02T00:00:00Z")
    rec = [o for o in out if o.get("resource_id") == "export_55"][0]
    assert rec["system"] == "export_service"
    assert rec["ts"] == "2026-02-01T00:00:00Z"
    assert rec["fields"]["download_available"] is False  # nested dicts are flattened
    assert [o for o in out if o.get("kind") == "coverage"][0]["covers"] == ["export_service"]


def test_parse_log_groups_by_trace(tmp_path):
    p = tmp_path / "svc.log"
    p.write_text(
        "2026-03-01T10:00:00Z INFO service=search_service trace_id=tr-1 event=lookup_started email=x@example.com\n"
        "2026-03-01T10:00:01Z INFO service=search_service trace_id=tr-1 event=lookup_completed resource_id=search_9 result=FOUND\n"
        "2026-03-01T10:00:02Z INFO service=search_service event=cache_lookup resource_id=search_9\n",  # noise event
        encoding="utf-8")
    out = parse_log(p, "ev_3", "2026-03-02T00:00:00Z")
    assert len(out) == 1
    o = out[0]
    assert o["resource_id"] == "search_9" and o["source"] == "log:trace"
    # Fields from every line of the trace are folded onto the resource.
    assert o["fields"]["email"] == "x@example.com" and o["fields"]["result"] == "FOUND"
    assert o["fields"]["events"] == ["lookup_started", "lookup_completed"]


def test_parse_pipe_text(tmp_path):
    p = tmp_path / "handover.txt"
    p.write_text("HANDOVER\n- 2026-04-01T09:00:00Z | support_platform | support_42 | SUPPORT_MESSAGE | NOT_FOUND | user_101\n",
                 encoding="utf-8")
    out = parse_pipe_text(p, "ev_4", "2026-04-02T00:00:00Z")
    assert out == [{
        "file_id": "ev_4", "captured_at": "2026-04-02T00:00:00Z", "ts": "2026-04-01T09:00:00Z",
        "system": "support_platform", "resource_id": "support_42",
        "fields": {"record_type": "SUPPORT_MESSAGE", "result": "NOT_FOUND", "subject_reference": "user_101"},
        "source": "text:handover",
    }]


# --------------------------------------------------------------------------- vision helpers


def test_gemini_response_and_null_fields():
    text = json.dumps({"screen": "Admin", "records": [{"resource_id": "backup_1", "status": None, "identifiers": None}]})
    result, usage = parse_gemini_response({"candidates": [{"content": {"parts": [{"text": text}]}}],
                                           "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}})
    assert usage == {"input_tokens": 10, "output_tokens": 5}
    assert "backup_1" in screenshot_to_text(result)  # JSON nulls must not crash flattening
    assert parse_gemini_response({"candidates": []}) is None


# --------------------------------------------------------------------------- ordering


def test_order_steps_phase_then_operation():
    steps = order_steps([
        ("search_service", "search_1", "VERIFY_RECORD_ABSENT", ""),
        ("search_service", "search_1", "DELETE_RECORD", ""),
        ("integration_service", "data_flow_1", "DISABLE_DATA_FLOW", ""),
        ("search_service", "search_1", "REPORT_REMAINING_RECORD", "ev_1"),
    ])
    assert [s["operation"] for s in steps] == [
        "REPORT_REMAINING_RECORD", "DISABLE_DATA_FLOW", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"]
    assert [s["step_no"] for s in steps] == [1, 2, 3, 4]
    assert steps[2]["state"] == "ABSENT" and steps[0]["phase"] == "FINDING"


# --------------------------------------------------------------------------- retention


def _obs(rid, fields):
    return {"resource_id": rid, "fields": fields}


def test_retention_until_field():
    assert retention_until(_obs("audit_record_1", {"retention_until": "2033-12-31T23:59:59Z"})) == "2033-12-31"


def test_retention_until_document_text():
    doc = "Event reference audit_record_003\nsubject reference user_03\nrecorded 25 Aug 2026\nretain until 25 Aug 2027 04"
    assert retention_until(_obs("audit_record_003", {"document_text": doc})) == "2027-08-25"


def test_retention_text_belongs_to_nearest_record():
    # The retention date follows audit_record_2, so it must not be credited to audit_record_1.
    doc = "audit_record_1 recorded 1 Jan 2026\naudit_record_2 retain until 1 Jan 2030"
    assert retention_until(_obs("audit_record_1", {"document_text": doc})) is None
    assert retention_until(_obs("audit_record_2", {"document_text": doc})) == "2030-01-01"


def test_retention_until_missing():
    assert retention_until(_obs("audit_record_1", {"screen_status": "Valid"})) is None
