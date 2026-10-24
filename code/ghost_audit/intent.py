"""Decide whether a request authorizes deletion.

Rule-based first (multilingual keyword lexicon with explicit negation cues);
an optional LLM hook can be supplied for texts the rules cannot place.
"""

from __future__ import annotations

import re

# A non-deletion cue anywhere in the text wins: these are explicit statements that
# the person wants to keep the account or is asking for something else.
NON_DELETION_CUES = [
    r"not asking for deletion",
    r"not a request to (erase|delete)",
    r"\bdo not (close|delete)",
    r"\bdon'?t delete",
    r"leave the account active",
    r"keep the account",
    r"continue using the service",
    r"leave my profile",
    r"without removing",
    r"marketing lists only",
    r"stop the promotional",
    r"\bunsubscribe\b",
    r"download my account information",
    r"portable format",
    r"copy of (the information|last year|my data)",
    r"what personal information you hold",
    r"information request",
    r"resetting the login",
    r"\bcorrect (it|my)\b",
    r"\bmisspelled\b",
    r"\bchange the email address\b",
    r"\breplace it with the new one\b",
]

DELETION_CUES = [
    r"\bdelet", r"\berase", r"\beras", r"\bremove", r"\bclose (my|this|the) account", r"\bclosed\b",
    r"would like it closed", r"close the account", r"permanent account[- ]deletion",
    r"suppression", r"supprim", r"eliminaci[oó]n", r"eliminar", r"exclus[aã]o", r"excluir",
    r"삭제", r"削除", r"löschen", r"cancellazione",
]


def classify(text: str, llm=None) -> tuple[bool, str]:
    """Return (authorizes_deletion, reason)."""
    lowered = text.lower().replace("’", "'")
    for cue in NON_DELETION_CUES:
        if re.search(cue, lowered):
            return False, f"non-deletion cue: {cue}"
    for cue in DELETION_CUES:
        if re.search(cue, lowered):
            return True, f"deletion cue: {cue}"
    if llm is not None:
        verdict = llm(text)
        if verdict is not None:
            return verdict, "llm"
    # Every request reaching the privacy queue is a deletion follow-up unless it
    # says otherwise (e.g. "confirm the profile is gone").
    return True, "default: deletion follow-up"
