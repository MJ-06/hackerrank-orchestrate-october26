"""Current + historical identity resolution.

A request email may be a current or a past address, and old systems may still
hold past names, phones or email hashes. The index merges the identity snapshot
with every old/new value in the audit log so all of them resolve to one user_id.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path


def norm_email(v: str) -> str:
    """Case- and whitespace-insensitive email key."""
    return v.strip().lower()


def norm_phone(v: str) -> str:
    """Digits only, so '+34 612 847 593' and '+34612847593' match."""
    return re.sub(r"\D", "", v)


class IdentityIndex:
    """user_id -> every known name/email/phone/hash, plus email -> user_ids."""

    def __init__(self, dataset: Path):
        self.users: dict[str, dict] = {}
        self.by_email: dict[str, set[str]] = defaultdict(set)
        self.history: dict[str, list[dict]] = defaultdict(list)
        with open(dataset / "user_identities.csv", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                uid = row["user_id"]
                self.users[uid] = {
                    "user_id": uid,
                    "names": {row["name"]},
                    "emails": {norm_email(row["email"])},
                    "phones": {norm_phone(row["phone"])},
                    "hashes": {row["email_hash"].lower()},
                }
                self.by_email[norm_email(row["email"])].add(uid)
        audit_path = dataset / "user_audit_log.csv"
        if audit_path.exists():
            with open(audit_path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    uid = row["user_id"]
                    user = self.users.setdefault(
                        uid, {"user_id": uid, "names": set(), "emails": set(), "phones": set(), "hashes": set()}
                    )
                    self.history[uid].append(row)
                    field = row["field_name"]
                    for value in (row["old_value"], row["new_value"]):
                        if not value:
                            continue
                        if field == "email":
                            user["emails"].add(norm_email(value))
                            self.by_email[norm_email(value)].add(uid)
                        elif field == "phone":
                            user["phones"].add(norm_phone(value))
                        elif field == "email_hash":
                            user["hashes"].add(value.lower())
                        elif field in ("name", "full_name"):
                            user["names"].add(value)

    def resolve(self, email: str) -> list[str]:
        """User ids that ever held this email (current or historical), sorted."""
        return sorted(self.by_email.get(norm_email(email), set()))

    def identifiers(self, uid: str) -> dict:
        """All known identifier sets for uid (empty sets for an unknown user)."""
        return self.users.get(uid, {"user_id": uid, "names": set(), "emails": set(), "phones": set(), "hashes": set()})
