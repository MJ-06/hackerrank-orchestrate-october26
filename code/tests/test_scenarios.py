"""End-to-end scenario tests on synthetic fixtures (the custom evaluation cases).

Each test builds a minimal dataset, runs the real Auditor on one request and
checks the decision and the steps. They are documented in
code/evaluation/evaluation_report.md as custom cases C1-C6.
"""

from __future__ import annotations

import csv
import importlib.util
import json

from conftest import ROOT, request, sql_search_doc

from main import run  # noqa: E402  (code/ is on sys.path via conftest)


def ops(steps):
    return [(s["operation"], s["resource_id"], s["evidence_id"]) for s in steps]


def test_c1_unsubscribe_only_is_not_applicable(make_auditor):
    a = make_auditor(evidence=[("ev_1", "evidence/s.sql", "2026-03-01T00:00:00Z", sql_search_doc())],
                     requests=[request(text="Please unsubscribe me from marketing emails. Keep the account active.")])
    status, steps, _ = a.audit(a.requests[0])
    assert (status, steps) == ("NOT_APPLICABLE", [])


def test_c2_remaining_copy_makes_case_incomplete(make_auditor):
    a = make_auditor(evidence=[("ev_1", "evidence/s.sql", "2026-03-04T00:00:00Z", sql_search_doc())],
                     requests=[request()])
    status, steps, _ = a.audit(a.requests[0])
    assert status == "INCOMPLETE"
    assert ops(steps) == [("REPORT_REMAINING_RECORD", "search_100", "ev_1"),
                          ("DELETE_RECORD", "search_100", ""),
                          ("VERIFY_RECORD_ABSENT", "search_100", "")]


def test_c3_evidence_captured_after_audit_at_is_never_cited(make_auditor):
    # ev_late shows the record gone, but it was captured after audit_at, so the
    # earlier snapshot (record still present) decides the case.
    late = ("CREATE TABLE search_documents (resource_id TEXT, user_id TEXT, email TEXT, result TEXT, indexed_at TEXT);\n"
            "INSERT INTO search_documents VALUES('search_100','user_101','test.person@example.com','NOT_FOUND','2026-03-09T00:00:00Z');\n")
    a = make_auditor(evidence=[("ev_early", "evidence/a.sql", "2026-03-04T00:00:00Z", sql_search_doc()),
                               ("ev_late", "evidence/b.sql", "2026-03-10T00:00:00Z", late)],
                     requests=[request(audit_at="2026-03-05T12:00:00Z")])
    status, steps, _ = a.audit(a.requests[0])
    assert status == "INCOMPLETE"
    assert all(s["evidence_id"] != "ev_late" for s in steps)
    assert ("REPORT_REMAINING_RECORD", "search_100", "ev_early") in ops(steps)


def test_c4_active_flow_is_disabled_before_records_change(make_auditor):
    flow = ("CREATE TABLE connector_flows (resource_id TEXT, owner_email TEXT, enabled TEXT, updated_at TEXT);\n"
            "INSERT INTO connector_flows VALUES('data_flow_7','test.person@example.com','true','2026-03-03T00:00:00Z');\n")
    a = make_auditor(evidence=[("ev_1", "evidence/s.sql", "2026-03-04T00:00:00Z", sql_search_doc()),
                               ("ev_2", "evidence/f.sql", "2026-03-04T00:00:00Z", flow)],
                     requests=[request()])
    status, steps, _ = a.audit(a.requests[0])
    assert status == "INCOMPLETE"
    remediation = [s["operation"] for s in steps if s["phase"] == "REMEDIATION"]
    assert remediation == ["DISABLE_DATA_FLOW", "DELETE_RECORD"]


def test_c5_screenshot_only_audit_record_needs_retention(make_auditor):
    # Rule 12: a security audit row shown only in a screenshot, with no retention
    # period, cannot prove the RETAIN condition; one that shows it can.
    screen = {"screen": "Security audit", "records": [
        {"system": "security_service", "resource_id": "audit_record_1", "identifiers": ["user_101"], "status": "Valid",
         "fields": {}},
        {"system": "security_service", "resource_id": "audit_record_2", "identifiers": ["user_101"], "status": "Valid",
         "fields": {"retention_until": "2030-01-01"}},
    ]}
    a = make_auditor(evidence=[("ev_img", "evidence/audit.png", "2026-03-04T00:00:00Z", b"\x89PNG fake")],
                     requests=[request()], image_results={"audit.png": screen})
    status, steps, _ = a.audit(a.requests[0])
    assert status == "COMPLETE"
    assert ops(steps) == [("VERIFY_RECORD_PRESENT", "audit_record_2", "ev_img")]


def test_c6_anonymized_orphan_is_linked_through_policy_batch(make_auditor):
    # analytics_200 carries no identifier any more; only the deletion worker's
    # batch (just before this request's COMPLETE report) ties it to the case.
    anon = ("CREATE TABLE analytics_profiles (resource_id TEXT, correlation_key TEXT, updated_at TEXT);\n"
            "INSERT INTO analytics_profiles VALUES('analytics_200',NULL,'2026-03-02T01:40:00Z');\n")
    worker = {"activity_id": "activity_1", "event_time": "2026-03-02T01:30:00Z", "event_type": "ANONYMIZE_RECORD",
              "source_system": "deletion_worker", "target_system": "analytics_platform", "resource_id": "analytics_200",
              "reported_result": "SUCCESS", "details": "Policy batch processed a record."}
    a = make_auditor(evidence=[("ev_1", "evidence/an.sql", "2026-03-03T00:00:00Z", anon)],
                     activity=[worker], requests=[request(reported_at="2026-03-02T02:00:00Z")])
    status, steps, _ = a.audit(a.requests[0])
    assert status == "COMPLETE"
    assert ops(steps) == [("VERIFY_RECORD_ANONYMIZED", "analytics_200", "ev_1")]


def _evaluator():
    """Load code/evaluation/main.py by path (its module name clashes with code/main.py)."""
    spec = importlib.util.spec_from_file_location("ghost_evaluator", ROOT / "code" / "evaluation" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_public_samples_offline(tmp_path):
    """Regression: the 32 public samples, cache only (no network), keep status accuracy 1.0."""
    out = tmp_path / "pred.csv"
    run(ROOT / "dataset" / "sample_deletion_requests.csv", out, use_llm=False)
    ev = _evaluator()
    res = ev.score(ev.load(out), ev.load(ROOT / "dataset" / "sample_output.csv"))
    assert res["cases"] == 32
    assert res["status_accuracy"] == 1.0
    assert res["step_precision"] == 1.0
    assert res["step_f1"] >= 0.99
    with open(out, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            steps = json.loads(r["steps_json"])
            assert [s["step_no"] for s in steps] == list(range(1, len(steps) + 1))
