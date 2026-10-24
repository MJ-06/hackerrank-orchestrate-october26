"""Optional Claude calls: screenshot reading (vision) and ambiguous request intent.

The deterministic engine does all reasoning; the model only turns pixels into
records. Every response is cached on disk (keyed by file hash + prompt version),
so a re-run costs nothing and works offline once the cache is filled.

Credentials come from the environment only (ANTHROPIC_API_KEY, or a local,
git-ignored .env file). Nothing secret is ever written to the repo.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from pathlib import Path

PROMPT_VERSION = "vision-v1"
DEFAULT_MODEL = "claude-haiku-5-5"
# USD per 1M tokens (input, output), Anthropic first-party list prices.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}

VISION_PROMPT = """You are reading a screenshot of an internal Northstar Shop admin console.
Transcribe every record visible in tables, detail panels and activity feeds. Do not
summarize and do not judge whether a deletion is complete; just report what the screen shows.

Return ONLY a JSON object:
{
  "screen": "<console name and page title>",
  "captured_text": "<all visible text that identifies people or resources, verbatim>",
  "records": [
    {
      "system": "<owning service if shown or implied, e.g. account_service, search_service, analytics_platform, support_platform, export_service, integration_service, backup_service, security_service, identity_service, marketing_platform, object_storage>",
      "resource_id": "<exact id as shown, e.g. backup_9287, search_428, user_17>",
      "identifiers": ["<every email, user_id, phone, name or hash shown for this record>"],
      "status": "<the record's state exactly as the screen states it, e.g. Active, Enabled, Disabled, Restorable, Blocked, Found, Not found, Deleted, Anonymized, Redacted, Available, Expired>",
      "fields": {"<label>": "<value>"}
    }
  ]
}
Copy ids, emails and dates character for character."""


def load_dotenv(root: Path) -> None:
    """Minimal .env reader so no extra dependency is needed. Existing env wins."""
    env = root / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class UsageMeter:
    def __init__(self):
        self.calls = []

    def add(self, model, purpose, usage, cached=False):
        self.calls.append({
            "model": model,
            "purpose": purpose,
            "cached": cached,
            "input_tokens": getattr(usage, "input_tokens", 0) if not isinstance(usage, dict) else usage.get("input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0) if not isinstance(usage, dict) else usage.get("output_tokens", 0),
        })

    def summary(self):
        per_model = {}
        for c in self.calls:
            m = per_model.setdefault(c["model"], {"calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0})
            m["calls"] += 1
            m["cache_hits"] += c["cached"]
            m["input_tokens"] += c["input_tokens"]
            m["output_tokens"] += c["output_tokens"]
        for model, m in per_model.items():
            pin, pout = PRICES.get(model, (0.0, 0.0))
            m["cost_usd"] = round(m["input_tokens"] / 1e6 * pin + m["output_tokens"] / 1e6 * pout, 6)
        return per_model


class ClaudeHelper:
    def __init__(self, cache_dir: Path, model: str | None = None, allow_network: bool = True):
        self.model = model or os.environ.get("GHOST_LLM_MODEL", DEFAULT_MODEL)
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.meter = UsageMeter()
        self.client = None
        if allow_network and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            try:
                import anthropic  # type: ignore

                self.client = anthropic.Anthropic()
            except ImportError:
                self.client = None

    # ------------------------------------------------------------------ cache
    def _cache_path(self, kind: str, digest: str) -> Path:
        return self.cache_dir / f"{kind}-{PROMPT_VERSION}-{digest[:16]}.json"

    # ------------------------------------------------------------------ vision
    def read_screenshot(self, path: Path) -> dict | None:
        data = path.read_bytes()
        digest = hashlib.sha256(data + PROMPT_VERSION.encode()).hexdigest()
        cache = self._cache_path("vision", digest)
        if cache.exists():
            payload = json.loads(cache.read_text(encoding="utf-8"))
            self.meter.add(payload.get("model", self.model), "vision", payload.get("usage", {}), cached=True)
            return payload["result"]
        if self.client is None:
            return None
        media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        response = self._call([
            {"type": "image", "source": {"type": "base64", "media_type": media,
                                          "data": base64.standard_b64encode(data).decode("utf-8")}},
            {"type": "text", "text": VISION_PROMPT},
        ], max_tokens=8000)
        if response is None:
            return None
        text = "".join(b.text for b in response.content if b.type == "text")
        result = parse_json_object(text)
        usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
        self.meter.add(self.model, "vision", usage)
        cache.write_text(json.dumps({"model": self.model, "file": path.name, "usage": usage, "result": result},
                                    indent=1, ensure_ascii=False), encoding="utf-8")
        return result

    def _call(self, content, max_tokens):
        import anthropic  # type: ignore

        for attempt in range(3):
            try:
                resp = self.client.messages.create(
                    model=self.model, max_tokens=max_tokens,
                    output_config={"effort": "low"},
                    messages=[{"role": "user", "content": content}],
                )
                if resp.stop_reason == "refusal":
                    return None
                return resp
            except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError):
                time.sleep(2 ** attempt)
            except anthropic.APIStatusError:
                return None
        return None


def parse_json_object(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"records": [], "captured_text": text}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"records": [], "captured_text": text}


STATUS_MAP = {
    "not found": "NOT_FOUND", "deleted": "NOT_FOUND", "absent": "NOT_FOUND", "expired": "NOT_FOUND", "removed": "NOT_FOUND",
    "found": "FOUND", "present": "FOUND", "available": "FOUND", "indexed": "FOUND", "visible": "FOUND",
    "active": "ACTIVE", "enabled": "ACTIVE", "running": "ACTIVE", "restorable": "ACTIVE", "syncing": "ACTIVE",
    "disabled": "DISABLED", "paused": "DISABLED", "stopped": "DISABLED",
    "blocked": "BLOCKED", "restore blocked": "BLOCKED",
    "anonymized": "ANONYMIZED", "redacted": "REDACTED",
}


def screenshot_to_text(result: dict) -> str:
    """Flatten a vision result into text the document parser can link on."""
    parts = [result.get("screen", ""), result.get("captured_text", "")]
    for rec in result.get("records", []):
        parts.append(" | ".join([rec.get("system", ""), rec.get("resource_id", ""), rec.get("status", "")]
                                + [str(i) for i in rec.get("identifiers", [])]
                                + [f"{k}={v}" for k, v in (rec.get("fields") or {}).items()]))
    return "\n".join(p for p in parts if p)


def normalize_status(status: str) -> str | None:
    s = (status or "").strip().lower()
    for key in sorted(STATUS_MAP, key=len, reverse=True):
        if key in s:
            return STATUS_MAP[key]
    return None
