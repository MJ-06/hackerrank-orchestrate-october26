"""Deterministic deletion-audit engine.

Pipeline per case:
  1. intent      - does the request authorize deletion?
  2. identity    - current + historical identifiers for the subject
  3. discovery   - every resource linked to those identifiers in evidence/activity
  4. state       - each resource's state at audit_at from the latest evidence
  5. policy      - required treatment from deletion_rules.csv semantics
  6. planning    - findings -> remediation -> verification, canonically ordered
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path

from . import intent as intent_mod
from .evidence import RESOURCE_RE, load_all, system_for
from .identity import IdentityIndex, norm_email, norm_phone

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
USER_RE = re.compile(r"\buser_\d+\b")
HASH_RE = re.compile(r"\b[0-9a-f]{64}\b")
PHONE_RE = re.compile(r"\+\d[\d\s()\-]{6,}\d")

OP_ORDER = [
    "REPORT_REMAINING_RECORD", "REPORT_EXPOSED_PII", "REPORT_ACTIVE_DATA_FLOW", "REPORT_RESTORE_RISK",
    "DISABLE_DATA_FLOW", "BLOCK_RESTORE", "DELETE_RECORD", "REDACT_RECORD", "ANONYMIZE_RECORD",
    "VERIFY_RECORD_ABSENT", "VERIFY_RECORD_PRESENT", "VERIFY_RECORD_REDACTED", "VERIFY_RECORD_ANONYMIZED",
    "VERIFY_DATA_FLOW_DISABLED", "VERIFY_RESTORE_BLOCKED",
]
PHASE_ORDER = {"FINDING": 0, "REMEDIATION": 1, "VERIFICATION": 2}
OP_STATE = {
    "REPORT_REMAINING_RECORD": "PRESENT", "REPORT_EXPOSED_PII": "PRESENT", "REPORT_ACTIVE_DATA_FLOW": "ACTIVE",
    "REPORT_RESTORE_RISK": "ACTIVE", "DISABLE_DATA_FLOW": "DISABLED", "BLOCK_RESTORE": "BLOCKED",
    "DELETE_RECORD": "ABSENT", "REDACT_RECORD": "REDACTED", "ANONYMIZE_RECORD": "ANONYMIZED",
    "VERIFY_RECORD_ABSENT": "ABSENT", "VERIFY_RECORD_PRESENT": "PRESENT", "VERIFY_RECORD_REDACTED": "REDACTED",
    "VERIFY_RECORD_ANONYMIZED": "ANONYMIZED", "VERIFY_DATA_FLOW_DISABLED": "DISABLED",
    "VERIFY_RESTORE_BLOCKED": "BLOCKED",
}
OP_PHASE = {op: ("FINDING" if op.startswith("REPORT") else "VERIFICATION" if op.startswith("VERIFY") else "REMEDIATION") for op in OP_ORDER}

# Treatment -> (finding op, remediation op, verification op)
TREATMENT_OPS = {
    "DELETE": ("REPORT_REMAINING_RECORD", "DELETE_RECORD", "VERIFY_RECORD_ABSENT"),
    "REDACT": ("REPORT_EXPOSED_PII", "REDACT_RECORD", "VERIFY_RECORD_REDACTED"),
    "ANONYMIZE": ("REPORT_EXPOSED_PII", "ANONYMIZE_RECORD", "VERIFY_RECORD_ANONYMIZED"),
    "DISABLE": ("REPORT_ACTIVE_DATA_FLOW", "DISABLE_DATA_FLOW", "VERIFY_DATA_FLOW_DISABLED"),
    "BLOCK": ("REPORT_RESTORE_RISK", "BLOCK_RESTORE", "VERIFY_RESTORE_BLOCKED"),
    "RETAIN": (None, None, "VERIFY_RECORD_PRESENT"),
}
WORKER_TREATMENT = {"DELETE_RECORD": "DELETE", "REDACT_RECORD": "REDACT", "ANONYMIZE_RECORD": "ANONYMIZE"}

PII_FIELDS = {
    "email", "phone", "email_hash", "full_name", "name", "display_name", "requester_name", "requester_email",
    "requester_phone", "customer_email", "owner_email", "owner_phone", "matched_value", "uploaded_by",
}
TRUE = {"true", "1", "yes", "enabled", "active"}
FALSE = {"false", "0", "no", "disabled", "stopped", "blocked"}


def _truthy(v) -> bool | None:
    """Parse booleans written as true/1/enabled/... (None when undecidable)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in TRUE:
        return True
    if s in FALSE:
        return False
    return None


def _is_null(v) -> bool:
    """True for empty or explicitly scrubbed values ('[REDACTED]', 'no direct identifier')."""
    return v is None or (isinstance(v, str) and v.strip().lower() in ("", "null", "none", "[redacted]", "no direct identifier"))


# --------------------------------------------------------------------------- retention

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
DMY_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})")
RETAIN_UNTIL_RE = re.compile(r"retain(?:ed)?\s+until\s*:?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})", re.I)


def _to_iso_date(value) -> str | None:
    """'2033-12-31T23:59:59Z' or '25 Aug 2027' -> '2033-12-31' / '2027-08-25'."""
    s = str(value or "")
    m = ISO_DATE_RE.search(s)
    if m:
        return "-".join(m.groups())
    m = DMY_DATE_RE.search(s)
    if m and m.group(2).lower() in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return None


def retention_until(obs: dict) -> str | None:
    """Retention end date an observation supplies for its own record, if any."""
    f = obs["fields"]
    for k, v in f.items():
        if "retention" in k and ("until" in k or "end" in k or "expires" in k):
            d = _to_iso_date(v)
            if d:
                return d
    # Free text: a "retain until <date>" that follows this record's id, before any other record id.
    doc = str(f.get("document_text") or "")
    for m in re.finditer(re.escape(obs["resource_id"]) + r"\b", doc):
        tail = doc[m.end():]
        other = RESOURCE_RE.search(tail)
        r = RETAIN_UNTIL_RE.search(tail, 0, other.start() if other else len(tail))
        if r:
            return _to_iso_date(r.group(1))
    return None


# --------------------------------------------------------------------------- state


def observe_state(obs: dict) -> dict:
    """Normalize one observation into {exists, identifiable, active} (None = unknown)."""
    f = obs["fields"]
    rid = obs["resource_id"]
    st = {"exists": None, "identifiable": None, "active": None}
    result = str(f.get("result") or "").upper()
    if result in ("FOUND", "IDENTIFIABLE", "PRESENT", "ACTIVE", "BLOCKED", "DISABLED", "ANONYMIZED", "REDACTED", "HIT"):
        st["exists"] = True
    elif result in ("NOT_FOUND", "ABSENT", "DELETED", "MISS"):
        st["exists"] = False
    if result == "IDENTIFIABLE":
        st["identifiable"] = True
    elif result in ("ANONYMIZED", "REDACTED"):
        st["identifiable"] = False
    if result == "ACTIVE":
        st["active"] = True
    elif result in ("BLOCKED", "DISABLED"):
        st["active"] = False

    for key in ("status_code", "http_status"):
        if key in f and f[key] is not None:
            code = str(f[key])
            st["exists"] = code.startswith("2")
    if "rows" in f and f["rows"] is not None and str(f["rows"]).isdigit():
        st["exists"] = int(f["rows"]) > 0
    if "hit_count" in f:
        st["exists"] = f["hit_count"] > 0
    if str(f.get("result", "")).lower() == "hit":
        st["exists"] = True
    if str(f.get("result", "")).lower() == "miss":
        st["exists"] = False
    if "profile" in f and f["profile"] is None and "status_code" in f:
        st["exists"] = False
    for key in ("download_available", "available"):
        if key in f and _truthy(f[key]) is not None:
            st["exists"] = _truthy(f[key])

    events = f.get("events") or []
    if st["exists"] is None:
        if obs["source"].startswith(("sql:", "json:", "pdf", "image")):
            st["exists"] = True
        elif any(e.endswith(("_completed", "_read", "_evaluated", "_pii_scan", "_check")) for e in events):
            st["exists"] = True

    # Direct identifiers still on the record?
    if st["identifiable"] is None and st["exists"]:
        keys = PII_FIELDS | ({"customer_reference", "user_id", "correlation_key"} if rid.startswith("analytics_") else set())
        present = [k for k in keys if k in f and not _is_null(f[k])]
        text = " ".join(str(f.get(k) or "") for k in ("message_text", "message_excerpt"))
        if f.get("direct_identifiers") is not None and str(f["direct_identifiers"]).isdigit():
            st["identifiable"] = int(f["direct_identifiers"]) > 0
        elif present or EMAIL_RE.search(text):
            st["identifiable"] = True
        elif any(k in f for k in keys) or "message_text" in f:
            st["identifiable"] = False

    # Flows and restore paths.
    if rid.startswith("data_flow_"):
        en = _truthy(f.get("enabled"))
        if en is not None:
            st["active"] = en
    if rid.startswith("backup_"):
        ra = _truthy(f.get("restore_allowed"))
        if ra is not None:
            st["active"] = ra
        elif _truthy(f.get("eligible")) is not None:
            st["active"] = _truthy(f.get("eligible")) and not _truthy(f.get("deletion_block"))
    return st


# --------------------------------------------------------------------------- linking


def obs_tokens(obs: dict, phones_known: set[str]) -> set[str]:
    """Identifier tokens found anywhere in an observation's values: e:email, u:user_id, h:hash, p:phone.

    Phones are only tokenized when they belong to a known identity, since loose
    digit runs (order numbers, ids) would otherwise create false links.
    """
    toks = set()
    for k, v in obs["fields"].items():
        if v is None or k in ("resource_id",):
            continue
        values = v if isinstance(v, list) else [v]
        for item in values:
            s = str(item)
            for m in EMAIL_RE.findall(s):
                toks.add("e:" + norm_email(m))
            for m in USER_RE.findall(s):
                toks.add("u:" + m)
            for m in HASH_RE.findall(s.lower()):
                toks.add("h:" + m)
            for m in PHONE_RE.findall(s):
                d = norm_phone(m)
                if d in phones_known:
                    toks.add("p:" + d)
    return toks


class Auditor:
    """Loads all evidence once, indexes it by resource and identifier, then audits requests."""

    def __init__(self, dataset: Path, image_extractor=None, intent_llm=None, request_files=()):
        self.dataset = dataset
        self.identity = IdentityIndex(dataset)
        self.intent_llm = intent_llm
        obs, self.documents, self.skipped = load_all(dataset, image_extractor)
        self.observations = [o for o in obs if "resource_id" in o]
        self.coverage = [o for o in obs if o.get("kind") == "coverage"]
        self.rules = list(csv.DictReader(open(dataset / "deletion_rules.csv", newline="", encoding="utf-8")))
        self.activity = list(csv.DictReader(open(dataset / "system_activity.csv", newline="", encoding="utf-8")))
        phones = set()
        for u in self.identity.users.values():
            phones |= u["phones"]
        self.by_resource = defaultdict(list)
        self.token_index = defaultdict(set)
        for o in self.observations:
            o["tokens"] = obs_tokens(o, phones)
            self.by_resource[o["resource_id"]].append(o)
            for t in o["tokens"]:
                self.token_index[t].add(o["resource_id"])
        self.worker_ops = defaultdict(list)
        self.worker_stream = []
        for a in self.activity:
            if a["event_type"] == "LINK_RESOURCE_IDENTITY":
                for m in EMAIL_RE.findall(a["details"]):
                    self.token_index["e:" + norm_email(m)].add(a["resource_id"])
                for m in USER_RE.findall(a["details"]):
                    self.token_index["u:" + m].add(a["resource_id"])
            if a["source_system"] == "deletion_worker":
                self.worker_stream.append(a)
                if a["event_type"] in WORKER_TREATMENT:
                    self.worker_ops[a["resource_id"]].append(a)
        self.linked_anywhere = set().union(*self.token_index.values()) if self.token_index else set()
        # Requests filed by other subjects are part of the input and help attribute
        # de-identified records that a shared deletion batch touched.
        self.requests = []
        for rf in request_files:
            with open(rf, newline="", encoding="utf-8") as fh:
                self.requests += list(csv.DictReader(fh))
        self._link_cache = {}
        self.account_of, self.orphans_of = self._assign_batches()

    # ------------------------------------------------------------------ discovery
    def subject_tokens(self, uid: str) -> set[str]:
        """Every current and historical identifier token of one user."""
        ident = self.identity.identifiers(uid)
        toks = {"u:" + uid}
        toks |= {"e:" + e for e in ident["emails"]}
        toks |= {"h:" + h for h in ident["hashes"]}
        toks |= {"p:" + p for p in ident["phones"] if p}
        return toks

    def linked_resources(self, uids) -> set[str]:
        """Resources whose evidence or activity mentions any identifier of these users."""
        key = tuple(sorted(uids))
        if key in self._link_cache:
            return set(self._link_cache[key])
        toks = set()
        for uid in uids:
            toks |= self.subject_tokens(uid)
        rids = set()
        for t in toks:
            rids |= self.token_index.get(t, set())
        # One hop through resource references (e.g. search doc -> source account).
        for o in self.observations:
            if o["resource_id"] in rids:
                continue
            for k, v in o["fields"].items():
                if k != "resource_id" and isinstance(v, str) and RESOURCE_RE.fullmatch(v) and v in rids:
                    rids.add(o["resource_id"])
        # Convention used by the account service: account_<NNN> for user_<NN>.
        for uid in uids:
            num = uid.split("_")[1]
            if int(num) >= 100:
                continue  # only the zero-padded legacy range is unambiguous
            cand = f"account_{int(num):03d}"
            if cand not in rids and (cand in self.worker_ops or cand in self.by_resource):
                owner = [o for o in self.by_resource.get(cand, []) if o["fields"].get("user_id") not in (None, uid)]
                if not owner:
                    rids.add(cand)
        self._link_cache[key] = set(rids)
        return rids

    def pick_account(self, accounts: list[str], case: dict) -> list[str]:
        """Choose the account(s) that this request concerns when the subject had several."""
        if len(accounts) <= 1:
            return accounts
        lo, hi = case["requested_at"], case["audit_at"]

        def score(rid):
            acts = [a["event_time"] for a in self.worker_ops.get(rid, []) if lo <= a["event_time"] <= hi]
            obs = [o["ts"] for o in self.by_resource.get(rid, []) if lo <= o["ts"] <= hi]
            first = min(acts + obs) if acts or obs else "9999"
            return (0 if acts else 1 if obs else 2, first)

        best = min(accounts, key=score)
        return [best]

    # ------------------------------------------------------------------ policy
    def treatment(self, rid: str) -> str:
        """Required action from deletion_rules.csv: DELETE, REDACT, ANONYMIZE, RETAIN, DISABLE or BLOCK.

        Audit records and legal holds are retained (rules 12-13); backups keep
        the archive but block restore (rule 14); support messages with shared
        context are redacted while subject-only attachments are deleted (5-6).
        """
        if rid.startswith("audit_record_") or rid.startswith("legal_hold_"):
            return "RETAIN"
        if rid.startswith("data_flow_"):
            return "DISABLE"
        if rid.startswith("backup_"):
            return "BLOCK"
        worker = [a["event_type"] for a in self.worker_ops.get(rid, [])]
        if worker:
            return WORKER_TREATMENT[worker[-1]]
        if rid.startswith("analytics_"):
            return "ANONYMIZE"
        if rid.startswith(("tombstone_", "email_delivery_")):
            return "REDACT"
        if rid.startswith("support_"):
            for o in self.by_resource.get(rid, []):
                f = o["fields"]
                rt = str(f.get("record_type", ""))
                doc = str(f.get("document_text", "")).lower()
                if rt == "SUPPORT_ATTACHMENT" or "file_name" in f or "file" in f or "storage_class" in f or any(
                    "attachment" in e for e in f.get("events", [])
                ) or "attachment" in doc:
                    return "DELETE"
            return "REDACT"
        return "DELETE"

    @staticmethod
    def retain_evidence(audit_at: str):
        """Filter for observations that can support RETAIN (rule: within the supplied retention period).

        Records from structured sources (SQL/JSON/logs/documents) keep their
        existing treatment. A screenshot row only counts when it shows a
        retention period covering audit_at itself; otherwise it cannot prove the
        RETAIN condition, and the step cites the evidence that does (if any).
        """
        day = audit_at[:10]

        def keep(o):
            return o["source"] != "image" or (retention_until(o) or "") >= day

        return keep

    @staticmethod
    def decisive_evidence(treat: str):
        """Filter that drops screenshot observations which cannot decide this treatment.

        A screen that merely lists a connector, backup or record proves it exists,
        but not that a flow is disabled, a restore blocked, or identifiers removed.
        Such a screenshot must not be cited as proof of compliance.
        """
        key = {"DISABLE": "active", "BLOCK": "active", "REDACT": "identifiable", "ANONYMIZE": "identifiable"}.get(treat)
        if key is None:
            return None

        def keep(o):
            return o["source"] != "image" or observe_state(o)[key] is not None

        return keep
    def state_at(self, rid: str, audit_at: str, strict: bool = True, keep=None):
        """Latest determinate observation known at audit_at (worst copy wins on ties).

        strict=True only uses files captured at or before audit_at, so every
        cited evidence id was available to the auditor at that time. `keep`
        optionally restricts which observations may be used.
        """
        cands = [o for o in self.by_resource.get(rid, []) if o["ts"] <= audit_at and (not strict or o["captured_at"] <= audit_at)
                 and (keep is None or keep(o))]
        if not cands:
            return None, None
        cands.sort(key=lambda o: (o["ts"], o["captured_at"]))
        latest_ts = None
        chosen = []
        for o in reversed(cands):
            st = observe_state(o)
            if st["exists"] is None and st["active"] is None:
                continue
            if latest_ts is None:
                latest_ts = o["ts"]
            if o["ts"] != latest_ts:
                break
            chosen.append((o, st))
        if not chosen:
            return None, None

        def badness(item):
            st = item[1]
            return (bool(st["exists"]), bool(st["identifiable"]), bool(st["active"]))

        chosen.sort(key=badness, reverse=True)
        return chosen[0]

    def absent_by_omission(self, rid: str, audit_at: str, after: str):
        """A covering snapshot taken after `after` and not listing rid proves absence."""
        system = system_for(rid)
        best = None
        for cov in self.coverage:
            if system in cov["covers"] and after < cov["captured_at"] <= audit_at:
                listed = any(o["file_id"] == cov["file_id"] for o in self.by_resource.get(rid, []))
                if not listed and (best is None or cov["captured_at"] > best["captured_at"]):
                    best = cov
        return best

    # ------------------------------------------------------------------ batches
    def _assign_batches(self):
        """Attribute un-identified account deletions and de-identified records to requests.

        The deletion workflow writes one primary-profile submission per request just
        before it reports a status (SUCCESS -> COMPLETE, PARTIAL -> PARTIALLY_COMPLETE,
        FAILED -> NOT_STARTED), then a policy batch for related records. Once a record
        is anonymized it carries no identifier, so the batch is the only link left.
        Requests that share a report time are processed in case order.
        """
        from datetime import datetime, timedelta

        def t(x):
            return datetime.strptime(x, "%Y-%m-%dT%H:%M:%SZ")

        result_for = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}
        reqs = sorted(self.requests, key=lambda r: (int(r["case_id"].split("_")[1]), r["case_id"]))
        uid_of = {r["case_id"]: self.identity.resolve(r["email"]) for r in reqs}
        linked_of = {cid: self.linked_resources(u) for cid, u in uid_of.items()}
        claimed = set()
        account_of = {}
        for r in reqs:
            accs = sorted(x for x in linked_of[r["case_id"]] if x.startswith("account_"))
            if accs:
                acc = self.pick_account(accs, r)[0]
                account_of[r["case_id"]] = acc
                claimed.add(acc)
        submissions = [a for a in self.worker_stream if a["event_type"] == "DELETE_RECORD" and a["resource_id"].startswith("account_")
                       and a["resource_id"] not in self.linked_anywhere]
        groups = defaultdict(list)
        for r in reqs:
            if r["case_id"] not in account_of and intent_mod.classify(r["request_text"], None)[0]:
                groups[(r["reported_at"], r["reported_status"])].append(r)
        for (rep, status), rs in groups.items():
            # Observed processing order within a shared report time: most recent request first.
            rs = sorted(rs, key=lambda r: (-t(r["requested_at"]).timestamp(), int(r["case_id"].split("_")[1])))
            lo, hi = t(rep) - timedelta(hours=3), t(rep) + timedelta(minutes=15)
            cands = sorted((a for a in submissions if lo <= t(a["event_time"]) <= hi and a["reported_result"] == result_for.get(status)
                            and a["resource_id"] not in claimed), key=lambda a: a["event_time"])
            for r, a in zip(rs, cands):
                account_of[r["case_id"]] = a["resource_id"]
                claimed.add(a["resource_id"])

        orphans_of = defaultdict(set)
        policy = [a for a in self.worker_stream if a["event_type"] in WORKER_TREATMENT and not a["resource_id"].startswith("account_")
                  and a["resource_id"] not in self.linked_anywhere]
        by_report = defaultdict(list)
        for r in reqs:
            by_report[r["reported_at"]].append(r)
        reports = sorted(by_report)
        batches = defaultdict(lambda: defaultdict(list))
        for a in policy:
            et = a["event_time"]
            later = [rep for rep in reports if et <= rep and t(rep) - t(et) <= timedelta(hours=2)]
            if later:
                batches[later[0]][a["resource_id"].rsplit("_", 1)[0]].append(a)
        for rep, by_prefix in batches.items():
            for prefix, acts in by_prefix.items():
                acts.sort(key=lambda a: a["event_time"])
                need = [r for r in by_report[rep] if not any(x.startswith(prefix + "_") for x in linked_of[r["case_id"]])
                        and self._preliminary_complete(r, account_of.get(r["case_id"]), linked_of[r["case_id"]])]
                need.sort(key=lambda r: int(r["case_id"].split("_")[1]))
                for r, a in zip(need, acts):
                    orphans_of[r["case_id"]].add(a["resource_id"])
        return account_of, orphans_of

    def _preliminary_complete(self, case, account, linked) -> bool:
        if not intent_mod.classify(case["request_text"], None)[0]:
            return False
        rids = {r for r in linked if not r.startswith("account_")} | ({account} if account else set())
        status, _, _ = self._evaluate(case, rids, account)
        return status == "COMPLETE"

    # ------------------------------------------------------------------ audit
    def audit(self, case: dict) -> tuple[str, list[dict], dict]:
        """Decide one request: (deletion_status, ordered steps, trace for debugging)."""
        trace = {"case_id": case["case_id"]}
        authorized, why = intent_mod.classify(case["request_text"], self.intent_llm)
        trace["intent"] = why
        if not authorized:
            return "NOT_APPLICABLE", [], trace
        uids = self.identity.resolve(case["email"])
        trace["user_ids"] = uids
        rids = self.linked_resources(uids)
        account = self.account_of.get(case["case_id"])
        if account is None:
            accounts = self.pick_account(sorted(r for r in rids if r.startswith("account_")), case)
            account = accounts[0] if accounts else None
        rids = {r for r in rids if not r.startswith("account_")} | ({account} if account else set())
        rids |= self.orphans_of.get(case["case_id"], set())
        trace["linked"] = sorted(rids)
        status, steps, _ = self._evaluate(case, rids, account)
        return status, steps, trace

    def _evaluate(self, case: dict, rids: set[str], account: str | None):
        """Compare each linked resource's state at audit_at with its required treatment."""
        audit_at = case["audit_at"]

        findings, remediation, verification = [], [], []
        violations = 0
        for rid in sorted(rids):
            system = system_for(rid)
            treat = self.treatment(rid)
            keep = self.retain_evidence(audit_at) if treat == "RETAIN" else self.decisive_evidence(treat)
            obs, st = self.state_at(rid, audit_at, keep=keep)
            if obs is None and treat == "RETAIN":
                # A retained record seen only in a later capture still needs a
                # post-remediation presence check; it just cannot be cited.
                later, lst = self.state_at(rid, "9999", strict=False, keep=keep)
                if later is not None and lst["exists"]:
                    verification.append((system, rid, "VERIFY_RECORD_PRESENT", None))
                continue
            f_op, r_op, v_op = TREATMENT_OPS[treat]
            if treat == "DELETE":
                deleted_at = min((a["event_time"] for a in self.worker_ops.get(rid, [])), default=None)
                last_seen = obs["ts"] if obs else None
                if deleted_at and obs is None:
                    cov = self.absent_by_omission(rid, audit_at, max(filter(None, [deleted_at, last_seen])))
                    if cov is not None:
                        obs, st = {"file_id": cov["file_id"]}, {"exists": False, "identifiable": None, "active": None}
            if obs is None:
                if rid == account:
                    verification.append((system, rid, "VERIFY_RECORD_ABSENT", None))
                continue
            ev = obs["file_id"]
            if treat == "RETAIN":
                if st["exists"]:
                    verification.append((system, rid, v_op, ev))
                continue
            if treat in ("DISABLE", "BLOCK"):
                bad = bool(st["active"])
            elif treat == "DELETE":
                bad = bool(st["exists"])
            else:
                bad = bool(st["exists"]) and bool(st["identifiable"])
            if bad:
                violations += 1
                findings.append((system, rid, f_op, ev))
                remediation.append((system, rid, r_op, ""))
                verification.append((system, rid, v_op, ""))
            else:
                v = "VERIFY_RECORD_ABSENT" if (treat == "DELETE" or st["exists"] is False) and treat not in ("DISABLE", "BLOCK") else v_op
                verification.append((system, rid, v, ev))

        status = "INCOMPLETE" if violations else "COMPLETE"
        if status == "INCOMPLETE":
            verification = [(s, r, op, "") for s, r, op, _ in verification]
        else:
            # A COMPLETE verdict may only rest on cited evidence.
            verification = [v for v in verification if v[3]]
        steps = order_steps(findings + remediation + verification)
        return status, steps, None


def order_steps(items) -> list[dict]:
    """(system, resource, operation, evidence) tuples -> numbered steps in canonical order.

    Phase first (FINDING, REMEDIATION, VERIFICATION), then operation order, which
    puts DISABLE/BLOCK before record changes, then system_id and resource_id.
    """
    rows = []
    for system, rid, op, ev in items:
        rows.append({
            "phase": OP_PHASE[op], "system_id": system, "resource_id": rid, "operation": op,
            "state": OP_STATE[op], "evidence_id": ev,
        })
    rows.sort(key=lambda s: (PHASE_ORDER[s["phase"]], OP_ORDER.index(s["operation"]), s["system_id"], s["resource_id"]))
    out = []
    for i, s in enumerate(rows, 1):
        out.append({"step_no": i, **s})
    return out
