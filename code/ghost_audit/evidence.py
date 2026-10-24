"""Evidence ingestion: turn every supplied evidence file into flat observations.

Each observation is one statement about one resource at one point in time:

    {
        "file_id": "evidence_92",
        "ts": "2026-09-15T09:31:40Z",     # when the state was observed
        "captured_at": "...",             # manifest capture time of the file
        "system": "account_service",
        "resource_id": "account_3136",
        "fields": {...},                  # flat key -> value facts about the resource
        "source": "service_resource_export:profile_lookups",
    }

Parsers are format-generic (SQL snapshots, JSON trees, key=value logs, pipe
tables, PDFs) so new files of a known shape need no code changes.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

RESOURCE_RE = re.compile(
    r"\b(account|search|analytics|support|export|data_flow|backup|audit_record|tombstone|"
    r"cache|marketing|email_delivery|object|legal_hold)_\d+\b"
)
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
KV_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=(?:"((?:[^"\\]|\\.)*)"|([^\s"]+))')

PREFIX_SYSTEM = [
    ("audit_record_", "security_service"),
    ("data_flow_", "integration_service"),
    ("email_delivery_", "email_delivery_service"),
    ("legal_hold_", "legal_service"),
    ("tombstone_", "identity_service"),
    ("analytics_", "analytics_platform"),
    ("account_", "account_service"),
    ("search_", "search_service"),
    ("support_", "support_platform"),
    ("export_", "export_service"),
    ("backup_", "backup_service"),
    ("cache_", "cache_service"),
    ("marketing_", "marketing_platform"),
    ("object_", "object_storage"),
]


def system_for(resource_id: str) -> str:
    for prefix, system in PREFIX_SYSTEM:
        if resource_id.startswith(prefix):
            return system
    return "unknown"


def _obs(file_id, captured_at, ts, resource_id, fields, source, system=None):
    return {
        "file_id": file_id,
        "captured_at": captured_at,
        "ts": ts or captured_at,
        "system": system or system_for(resource_id),
        "resource_id": resource_id,
        "fields": fields,
        "source": source,
    }


# --------------------------------------------------------------------------- SQL

# Table/collection name fragments that identify which system a snapshot covers,
# so a resource missing from a covering snapshot can be proven absent.
TABLE_SYSTEMS = [
    ("customer_profiles", "account_service"),
    ("search_documents", "search_service"),
    ("analytics", "analytics_platform"),
    ("support", "support_platform"),
    ("export", "export_service"),
    ("connector", "integration_service"),
    ("backup", "backup_service"),
    ("security_audit", "security_service"),
    ("deleted_customer_references", "identity_service"),
]


def parse_sql(path: Path, file_id: str, captured_at: str):
    con = sqlite3.connect(":memory:")
    text = path.read_text(encoding="utf-8")
    try:
        con.executescript(text)
    except sqlite3.Error:
        # Fall back to statement-by-statement execution for partially valid dumps.
        for stmt in text.split(";\n"):
            try:
                con.execute(stmt)
            except sqlite3.Error:
                pass
    out = []
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    scope = {}
    if "snapshot_scope" in tables:
        scope = dict(con.execute("SELECT origin_system, captured_at FROM snapshot_scope"))
    covered = set(scope)
    for table in tables:
        if table == "snapshot_scope":
            continue
        for marker, system in TABLE_SYSTEMS:
            if marker in table:
                covered.add(system)
        cur = con.execute(f'SELECT * FROM "{table}"')
        cols = [d[0] for d in cur.description]
        for row in cur:
            rec = dict(zip(cols, row))
            if "record_json" in rec:
                try:
                    inner = json.loads(rec.pop("record_json"))
                except json.JSONDecodeError:
                    inner = {}
                rec.update(_flatten(inner))
            rid = rec.get("resource_id")
            if not rid:
                continue
            system = rec.get("origin_system") or system_for(rid)
            ts = rec.get("updated_at") or rec.get("indexed_at") or captured_at
            # A snapshot reflects the state at capture time, not at last write.
            ts = max(ts, scope.get(system, captured_at)) if ts else captured_at
            rec["_table"] = table
            out.append(_obs(file_id, captured_at, ts, rid, rec, f"sql:{table}", system))
    # Tables in scope that do not contain a resource are evidence of absence
    # only for resources that we know about elsewhere; recorded for the engine.
    for system in {o["system"] for o in out}:
        covered.add(system)
    out.append({"file_id": file_id, "captured_at": captured_at, "covers": sorted(covered), "kind": "coverage"})
    con.close()
    return out


def _flatten(d, prefix=""):
    flat = {}
    for k, v in d.items():
        if isinstance(v, dict):
            flat.update(_flatten(v, ""))
        else:
            flat[prefix + k] = v
    return flat


# --------------------------------------------------------------------------- JSON


def parse_json(path: Path, file_id: str, captured_at: str):
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []

    def walk(node, ctx, trail):
        if isinstance(node, dict):
            local = dict(ctx)
            for key in ("captured_at", "exported_at", "observed_at", "recorded_at", "data_store", "system", "collection"):
                if key in node and isinstance(node[key], str):
                    local[key] = node[key]
            if "resource_id" in node:
                fields = {k: v for k, v in node.items() if not isinstance(v, (dict, list))}
                for k, v in node.items():
                    if isinstance(v, dict) and k not in ("hits",):
                        fields.update(_flatten(v))
                    elif isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v):
                        fields[k] = v
                rid = node["resource_id"]
                if isinstance(rid, str) and RESOURCE_RE.fullmatch(rid):
                    system = node.get("system") or local.get("data_store") or trail_system(trail) or system_for(rid)
                    if system_for(rid) != "unknown" and system not in KNOWN_SYSTEMS:
                        system = system_for(rid)
                    ts = node.get("observed_at") or node.get("recorded_at") or local.get("captured_at") or local.get("exported_at")
                    if "hits" in node:
                        docs = (node.get("hits") or {}).get("documents") or []
                        fields["hit_count"] = len(docs)
                    out.append(_obs(file_id, captured_at, ts, rid, fields, "json:" + "/".join(trail[-2:]), system))
            for k, v in node.items():
                walk(v, local, trail + [k])
        elif isinstance(node, list):
            for v in node:
                walk(v, ctx, trail)

    walk(data, {}, [])
    covered = set()
    for snap in data.get("snapshots", []) if isinstance(data, dict) else []:
        if isinstance(snap, dict) and snap.get("data_store") and isinstance(snap.get("records"), list):
            covered.add(snap["data_store"])
    if covered:
        out.append({"file_id": file_id, "captured_at": captured_at, "covers": sorted(covered), "kind": "coverage"})
    return out


KNOWN_SYSTEMS = {s for _, s in PREFIX_SYSTEM}


def trail_system(trail):
    for t in reversed(trail):
        if t in KNOWN_SYSTEMS:
            return t
    return None


# --------------------------------------------------------------------------- logs


def _line_service(line: str) -> str | None:
    for system in KNOWN_SYSTEMS | {"profile_service", "support_replica"}:
        if re.search(rf"(?<![A-Za-z_]){system}(?![A-Za-z_])", line):
            return system
    return None


def _line_trace(line: str, kv: dict) -> str | None:
    for key in ("trace_id", "requestId"):
        if key in kv:
            return kv[key]
    m = re.search(r"\b(tr-[A-Za-z0-9-]+|rb-[0-9-]+)\b", line)
    return m.group(1) if m else None


NOISE_EVENTS = {"cache_lookup", "connection_acquired", "dependency_retry", "dependency_recovered", "cache_refresh", "consumer_lag"}


def parse_log(path: Path, file_id: str, captured_at: str):
    """Group log lines by trace id and fold every key=value into the trace's resource."""
    traces: dict[str, dict] = {}
    order = []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        tsm = TS_RE.match(line)
        ts = tsm.group(0) if tsm else captured_at
        kv = {}
        payload = line
        jm = re.search(r"\{.*\}\s*$", line)
        if jm:
            try:
                kv.update({k: v for k, v in json.loads(jm.group(0)).items() if not isinstance(v, (dict, list))})
            except json.JSONDecodeError:
                pass
        # Unescape nested message="k=\"v\"" payloads before scanning.
        payload = payload.replace('\\"', '"')
        for k, v1, v2 in KV_RE.findall(payload):
            kv.setdefault(k, v1 if v1 != "" or v2 == "" else v2)
        # "[tr-xxx] event_name:" and "| event |" styles carry the event outside k=v.
        if "event" not in kv:
            em = re.search(r"\]\s+([a-z_]+):", line) or re.search(r"\|\s*([a-z_]+)\s*\|\s*instance=", line)
            if em:
                kv["event"] = em.group(1)
        if kv.get("event") in NOISE_EVENTS:
            continue
        trace = _line_trace(line, kv)
        service = kv.get("service") or kv.get("logger") or kv.get("function") or _line_service(line)
        rid = kv.get("resource_id")
        if trace and trace.startswith("collector"):
            trace = None
        if trace:
            t = traces.get(trace)
            if t is None:
                t = traces[trace] = {"fields": {}, "rids": [], "ts": ts, "service": service}
                order.append(trace)
            for k, v in kv.items():
                if k in ("resource_id",):
                    continue
                if k == "event":
                    t["fields"].setdefault("events", []).append(v)
                else:
                    t["fields"][k] = v
            if rid:
                if rid not in t["rids"]:
                    t["rids"].append(rid)
                t["ts"] = ts
        elif rid:
            fields = dict(kv)
            fields.pop("resource_id", None)
            if "event" in fields:
                fields["events"] = [fields["event"]]
            out.append(_obs(file_id, captured_at, kv.get("recorded_at") or ts, rid, fields, "log:line", kv.get("system")))
    for trace in order:
        t = traces[trace]
        for rid in t["rids"]:
            fields = dict(t["fields"])
            fields["trace_id"] = trace
            out.append(_obs(file_id, captured_at, t["ts"], rid, fields, "log:trace", fields.get("system")))
    return out


# --------------------------------------------------------------------------- text / pdf


def parse_pipe_text(path: Path, file_id: str, captured_at: str):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.lstrip("- ").split("|")]
        if len(parts) >= 6 and TS_RE.fullmatch(parts[0]) and RESOURCE_RE.fullmatch(parts[2]):
            ts, system, rid, record_type, result, subject = parts[:6]
            fields = {"record_type": record_type, "result": result, "subject_reference": subject}
            out.append(_obs(file_id, captured_at, ts, rid, fields, "text:handover", system))
    return out


def pdf_text(path: Path) -> str:
    if shutil.which("pdftotext"):
        try:
            return subprocess.run(
                ["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=60
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
    try:  # optional pure-python fallback
        from pypdf import PdfReader  # type: ignore

        return "\n".join(p.extract_text() or "" for p in PdfReader(str(path)).pages)
    except Exception:
        return ""


def parse_document_text(text: str, file_id: str, captured_at: str, source: str):
    """Business documents: every resource id mentioned is tied to the identifiers in the same document."""
    out = []
    rids = sorted(set(m.group(0) for m in RESOURCE_RE.finditer(text)))
    for rid in rids:
        out.append(_obs(file_id, captured_at, captured_at, rid, {"document_text": text}, source))
    return out


def parse_screenshot(result: dict, file_id: str, captured_at: str):
    """Vision transcription -> one observation per record shown on screen."""
    from .llm import normalize_status, screenshot_to_text

    out = []
    full_text = screenshot_to_text(result)
    seen = set()
    for rec in result.get("records") or []:
        rid = str(rec.get("resource_id") or "").strip()
        if not RESOURCE_RE.fullmatch(rid):
            continue
        seen.add(rid)
        fields = {"identifiers": [str(i) for i in rec.get("identifiers") or []], "screen_status": rec.get("status")}
        status = normalize_status(rec.get("status", ""))
        if status:
            fields["result"] = status
        for k, v in (rec.get("fields") or {}).items():
            fields[f"screen_{re.sub(r'[^a-z0-9]+', '_', str(k).lower()).strip('_')}"] = v
        system = rec.get("system") if rec.get("system") in KNOWN_SYSTEMS else None
        out.append(_obs(file_id, captured_at, captured_at, rid, fields, "image", system))
    # Ids mentioned only in free text still get linked through the shared text.
    for m in RESOURCE_RE.finditer(full_text):
        if m.group(0) not in seen:
            seen.add(m.group(0))
            out.append(_obs(file_id, captured_at, captured_at, m.group(0), {"document_text": full_text}, "image"))
    return out


# --------------------------------------------------------------------------- loader


def load_manifest(dataset: Path):
    with open(dataset / "evidence_files.csv", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_all(dataset: Path, image_extractor=None):
    """Return (observations, documents, skipped) for every manifest entry."""
    observations, documents, skipped = [], {}, []
    for row in load_manifest(dataset):
        path = dataset / row["file_path"]
        fid, cap = row["file_id"], row["captured_at"]
        if not path.exists():
            skipped.append((fid, "missing"))
            continue
        suffix = path.suffix.lower()
        try:
            if suffix == ".sql":
                observations += parse_sql(path, fid, cap)
            elif suffix == ".json":
                observations += parse_json(path, fid, cap)
            elif suffix == ".log":
                observations += parse_log(path, fid, cap)
            elif suffix == ".txt":
                observations += parse_pipe_text(path, fid, cap)
            elif suffix == ".pdf":
                text = pdf_text(path)
                documents[fid] = text
                observations += parse_document_text(text, fid, cap, "pdf")
            elif suffix in (".png", ".jpg", ".jpeg"):
                result = image_extractor(path) if image_extractor else None
                if result:
                    observations += parse_screenshot(result, fid, cap)
                    documents[fid] = result
                else:
                    skipped.append((fid, "image-not-extracted"))
        except Exception as exc:  # keep the run alive; report the file
            skipped.append((fid, f"error: {exc}"))
    return observations, documents, skipped
