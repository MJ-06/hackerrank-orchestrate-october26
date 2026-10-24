"""Current + historical identity resolution."""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path


def norm_email(v: str) -> str:
    return v.strip().lower()


def norm_phone(v: str) -> str:
    return re.sub(r"\D", "", v)


class IdentityIndex:
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
        return sorted(self.by_email.get(norm_email(email), set()))

    def identifiers(self, uid: str) -> dict:
        return self.users.get(uid, {"user_id": uid, "names": set(), "emails": set(), "phones": set(), "hashes": set()})

    def audit_ids(self, uid: str) -> list[str]:
        return [r["audit_id"] for r in self.history.get(uid, [])]
