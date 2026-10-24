# Evaluation Report: Ghost Data Still Exists

All numbers in this report come from real runs of the code in this repository:

```bash
python code/main.py --requests sample --no-llm   # 32 public samples, cache only
python code/evaluation/main.py -v                # score against dataset/sample_output.csv
python code/main.py                              # full 518-request run -> output.csv
python -m pytest code/tests                      # unit, scenario and regression tests
```

The evaluator ([`code/evaluation/main.py`](main.py)) compares predictions with the
public answers in `dataset/sample_output.csv`. It uses these metrics:

| Metric | Definition |
|---|---|
| Status accuracy | `deletion_status` exact match per case |
| Step precision / recall / F1 | Multiset match on the full step tuple `(phase, system_id, resource_id, operation, state, evidence_id)` |
| Field accuracy | Per-field agreement of steps aligned by position (`step_no`) |
| Sequence accuracy | Whole `steps_json` array identical (after JSON parsing) and status correct |

## 1. Results on the 32 public samples

| Metric | Value |
|---|---|
| Cases | 32 |
| **Status accuracy** | **1.000** (32 / 32) |
| Step precision | 1.000 |
| Step recall | 0.984 |
| **Step F1** | **0.992** |
| Sequence accuracy | 0.906 (29 / 32) |

### Field accuracy

| Field | Accuracy |
|---|---|
| phase | 0.984 |
| system_id | 0.959 |
| resource_id | 0.959 |
| operation | 0.959 |
| state | 0.959 |
| evidence_id | 0.979 |

Field accuracy is lower than step F1 because it is positional. One missing step
shifts every later step in that case by one position, so a single omission
counts against several fields.

### Per-status breakdown

| Gold status | Cases | Status correct | Exact sequence | Gold steps | Predicted steps |
|---|---|---|---|---|---|
| COMPLETE | 10 | 10 | 9 | 34 | 33 |
| INCOMPLETE | 22 | 22 | 20 | 159 | 157 |
| NOT_APPLICABLE | 0 in the samples | - | - | - | - |

Precision is 1.000: every step we emit is in the gold answer. All three remaining
errors are omissions, analysed below.

## 2. Error analysis (every remaining miss)

| Case | Gold has | We produce | Why |
|---|---|---|---|
| case_04 | `VERIFY_RECORD_ABSENT account_004` citing `evidence_54` | The other step (`analytics_901` anonymized), but not this one | `account_004` is **not named in any participant-facing file**. The gold proves it absent by omission: the evidence_54 failover replica (`customer_profiles_failover_replica`) does not list it. The only way to recover the id is from the numbering convention (`user_04` → `account_004`). The engine applies that convention only when the account appears in some evidence or worker event, so that an unrelated id cannot be invented. |
| case_18 | `VERIFY_RECORD_PRESENT audit_record_6645` (post-remediation, no evidence) | Everything else in the case | `audit_record_6645` appears **nowhere** in `dataset/` apart from `sample_output.csv`. It cannot be recovered from participant data without reading the answers, which the rules forbid. |
| case_20 | `VERIFY_RECORD_PRESENT audit_record_5569` (post-remediation, no evidence) | Everything else in the case | Same as case_18: the id appears only in the gold file. |

We checked this with `grep -r <id> dataset/` (excluding `sample_output.csv`). All
three ids return no matches. With the current evidence, step F1 0.992 is the
ceiling unless the engine guesses identifiers.

## 3. Custom evaluation cases

Each case is a **real fixture**: a minimal synthetic dataset (identities,
activity, rules, manifest and evidence files) is written to a temporary
directory, and the production `Auditor` runs on it. Source:
[`code/tests/test_scenarios.py`](../tests/test_scenarios.py), with fixture builder
[`code/tests/conftest.py`](../tests/conftest.py).

| # | Case | Input | Expected | Result |
|---|---|---|---|---|
| C1 | Unsubscribe only | Search doc holding the subject's email; request: *"Please unsubscribe me from marketing emails. Keep the account active."* | `NOT_APPLICABLE`, `[]` | pass |
| C2 | Remaining copy | SQL search snapshot (before audit_at) still holding the email | `INCOMPLETE`: REPORT_REMAINING_RECORD (cites ev_1) → DELETE_RECORD → VERIFY_RECORD_ABSENT | pass |
| C3 | Evidence after audit_at | Early snapshot shows the record present; a later snapshot, **captured after audit_at**, shows it gone | `INCOMPLETE`, citing only the early file; the late file is never cited | pass |
| C4 | Flow before record | Active connector `data_flow_7` plus a remaining search doc | Remediation order is `DISABLE_DATA_FLOW` then `DELETE_RECORD` | pass |
| C5 | Rule 12 on screenshots | Screenshot shows two security audit rows: one without a retention period, one with `retention_until 2030-01-01` | `COMPLETE`, with exactly one `VERIFY_RECORD_PRESENT` (for the row with retention) | pass |
| C6 | Anonymized orphan | `analytics_200` has no identifier left; a deletion-worker `ANONYMIZE_RECORD` event runs 30 min before this request's COMPLETE report | Linked through the policy batch: `COMPLETE`, `VERIFY_RECORD_ANONYMIZED analytics_200` | pass |
| C7 | Screenshot is not proof of compliance | SQL row shows `data_flow_7` `enabled=false`; a later screenshot only lists it with a "Healthy" badge | `VERIFY_DATA_FLOW_DISABLED` cites the SQL file, never the screenshot | pass |

The suite also contains unit tests for the intent classifier (6 languages, plus
negations), every parser (SQL, JSON, log traces, pipe-text handovers), Gemini
response parsing with JSON nulls, step ordering and `retention_until`. It
also has an offline regression on the 32 public samples.

```text
python -m pytest code/tests -q
29 passed, 1 xfailed
```

The one `xfail` is strict and documents a known limitation (section 6).

## 4. Full-dataset run (518 requests)

| Status | Rows |
|---|---|
| INCOMPLETE | 350 |
| COMPLETE | 158 |
| NOT_APPLICABLE | 10 |
| **Total** | **518** |

The run produces 3,220 steps (708 FINDING, 708 REMEDIATION, 1,804 VERIFICATION).
The most common operations are VERIFY_RECORD_ABSENT (831), VERIFY_RECORD_PRESENT (427),
REPORT_REMAINING_RECORD / DELETE_RECORD (383 each) and REPORT_EXPOSED_PII (188).
A validation script checked every row: consecutive `step_no` values, phases in order, only the
allowed operation/state pairs, exactly the seven step fields, `[]` for NOT_APPLICABLE, and no cited
evidence captured after the case's `audit_at`. It found 0 violations. The run takes about 5 seconds on a laptop once
the screenshot cache is filled.

## 5. Ablation: rules only vs. rules plus screenshot vision

The 11 screenshots (`evidence_04, 09, 14, 19, 23, 28, 32, 37, 42, 46, 60`)
are the only evidence that needs a model. "Rules only" is the output with no
screenshot transcription available (commit `33d0d51`). "With vision" is the
current `output.csv`.

| | COMPLETE | INCOMPLETE | NOT_APPLICABLE |
|---|---|---|---|
| Rules only | 162 | 346 | 10 |
| With vision | 158 | 350 | 10 |

5 of 518 rows change. Each one is a record that a screenshot shows still
present, which no other file revealed:

| Case | Change | What the screenshot showed |
|---|---|---|
| case_33, case_52 | COMPLETE → INCOMPLETE | `export_1260` with status "Ready" for download, owner = the subject (export console, evidence_23) |
| case_35 | COMPLETE → INCOMPLETE | `search_428` still indexed with the subject's name and email on 1 Sep (evidence_09). This is newer than the 27 Aug read replica (evidence_10) that had shown it absent. |
| case_63 | COMPLETE → INCOMPLETE | `marketing_001` "Live" with the subject's email and phone (marketing console, evidence_60) |
| case_34 | adds a finding | `account_5762` still shown for the subject in the admin console (evidence_04) |

An earlier version also changed four more rows (case_39, 50, 53, 55). In each, a
screenshot that merely *listed* a record was cited as proof that it was compliant,
for example a connector with a "Healthy · Continuous sync" badge cited for
`VERIFY_DATA_FLOW_DISABLED`. We caught this while writing this report and fixed it (section 6).

On the 32 public samples the screenshots change nothing: no sample case depends
on a screenshot, so the sample score is the same with or without vision.

## 6. Failures found and improvements made

| Problem found | Fix | Effect |
|---|---|---|
| 11 screenshots were never read (`image-not-extracted`) | Added a vision layer: Gemini free tier, with a cached JSON transcription per image | 9 evaluation rows now use screenshot evidence |
| Gemini returns JSON `null` for fields it cannot see, which crashed flattening on 2 screenshots | Null-safe `screenshot_to_text` and `parse_screenshot` | All 11 screenshots parse; covered by a unit test |
| A screenshot table of security audit rows created `VERIFY_RECORD_PRESENT` steps for six `audit_record_*` ids with no retention period (case_45) | Rule 12 applied literally: a screenshot row supports RETAIN only if it shows a retention date covering audit_at. Added `retention_until()`, which reads `retention_until` fields and "retain until 25 Aug 2027" in PDF text | case_45 back to its evidence-backed answer, now citing the PDF retention extract (evidence_55) |
| Screenshots that only list a connector, backup or record were cited as proof that a flow was disabled, a restore blocked or identifiers removed (4 steps in case_39, 50, 53, 55). Image observations default to "exists", and an unknown `active`/`identifiable` value was read as compliant. | For DISABLE/BLOCK/REDACT/ANONYMIZE, a screenshot observation is ignored unless it decides that attribute. Structured sources are unchanged: an audit showed no gold sample step relies on undecided evidence, and the one non-image case (case_264, a log line with `decision="deny"`) is a correct citation. | The 4 false verifications are gone; sample scores unchanged; covered by custom case C7 |
| A cited evidence file could postdate `audit_at` | `state_at(strict=True)` only uses files with `captured_at ≤ audit_at` | Covered by custom case C3 |
| Anonymized records carry no identifier, so they could not be linked to a request | Policy-batch attribution: worker events shortly before a request's report time are assigned to that request | Covered by custom case C6 |

### Known limitations

* **Intent phrasing.** The non-deletion cue `copy of (the information|last year|my data)` outranks deletion verbs. *"Delete my account and every copy of my data"* would be classified NOT_APPLICABLE. No request in the dataset uses that phrasing (the two that match, case_540 and case_542, really are access requests). The limitation is pinned by a strict `xfail` test.
* **Ids that never appear.** As in section 2, records that exist only by convention (`account_004`) or only in the answers (`audit_record_6645`, `audit_record_5569`) are not reported.
* **Whole-document linking for PDFs.** Every resource id in a PDF is tied to every identifier in that PDF. This is correct for the single-subject business documents in this dataset, but would over-link a multi-subject report.
* **PII in screenshot free text.** When a screenshot shows a record only in free text (for example "user_20 · analytics_7438 · email"), the engine records that the record exists but does not judge whether that text exposes identifiers. Such a screenshot can therefore neither prove nor disprove anonymization or redaction. There is a related gap for log fields the state model does not parse. `decision="deny"` on a restore-authorization line is not read as "blocked". case_264's `VERIFY_RESTORE_BLOCKED` is still emitted, through the default that treats an unknown restore state on structured evidence as compliant, and here that is the right answer. Parsing such decision fields explicitly would remove the reliance on that default.
* **Vision quality.** Screenshot facts are only as good as the transcription. The model is asked to copy ids, emails and dates character for character, and results are cached so a run is reproducible. A wrong transcription would still flow into the answer.

### Next improvements

1. Score each deletion and non-deletion cue by proximity to the request's main verb instead of "any non-deletion cue wins".
2. Store a per-record confidence for screenshot facts, and require a second source before a screenshot alone flips COMPLETE to INCOMPLETE.
3. Split PDF text into per-record sections, using the same "following text up to the next id" rule that `retention_until` uses, before linking identifiers.
