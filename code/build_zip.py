"""Build code.zip for submission.

Layout (as in the starter's packaging command): README.md and .env.example at
the archive root, plus the whole code/ tree, including code/cache so the
screenshot transcriptions ship and the run reproduces offline with no API key.

Excluded: .env and any other secrets, dataset/, virtual environments, Python
caches, nested archives. After writing, the archive is scanned for API-key
patterns and for the actual key values in .env (compared in memory, never printed).

    python code/build_zip.py            # -> ./code.zip
"""

from __future__ import annotations

import os
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "code.zip"
ROOT_FILES = ["README.md", ".env.example"]
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".venv", "venv", "node_modules", ".git", "dataset"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".zip", ".tar", ".gz", ".tgz", ".7z", ".rar"}
# Google API keys, Anthropic/OpenAI-style secret keys, bearer tokens. The prefixes
# are split so this file does not itself match a plain `grep` for them.
KEY_PATTERNS = [re.compile(b"AI" + rb"za[0-9A-Za-z_\-]{20,}"), re.compile(b"s" + rb"k-[A-Za-z0-9_\-]{16,}"),
                re.compile(rb"Bearer\s+[A-Za-z0-9._\-]{20,}")]


def wanted(path: Path) -> bool:
    """True for files that belong in the archive."""
    rel = path.relative_to(ROOT)
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return False
    if path.name == ".env" or path.name.startswith(".env.") and path.name != ".env.example":
        return False
    return path.suffix.lower() not in EXCLUDE_SUFFIXES


def env_secret_values() -> list[bytes]:
    """Values of *_KEY / *_TOKEN entries in the local .env, to prove none leaked. Never printed."""
    env = ROOT / ".env"
    values = []
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if re.search(r"(KEY|TOKEN|SECRET)$", k.strip()) and len(v) >= 8:
                    values.append(v.encode())
    for k, v in os.environ.items():
        if re.search(r"(API_KEY|AUTH_TOKEN)$", k) and len(v) >= 8:
            values.append(v.encode())
    return values


def build() -> list[str]:
    """Write code.zip and return the archived paths."""
    files = [ROOT / f for f in ROOT_FILES if (ROOT / f).exists()]
    files += sorted(p for p in (ROOT / "code").rglob("*") if p.is_file() and wanted(p))
    names = []
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            name = p.relative_to(ROOT).as_posix()
            zf.write(p, name)
            names.append(name)
    return names


def scan() -> list[str]:
    """Names of archived files containing a key pattern or a real .env secret value."""
    secrets = env_secret_values()
    hits = []
    with zipfile.ZipFile(OUT) as zf:
        for info in zf.infolist():
            data = zf.read(info)
            if any(p.search(data) for p in KEY_PATTERNS) or any(s in data for s in secrets):
                hits.append(info.filename)
            if info.filename.split("/")[-1] == ".env" or info.filename.startswith("dataset/"):
                hits.append(info.filename)
    return hits


def main() -> int:
    names = build()
    hits = scan()
    for n in names:
        print(n)
    print(f"\n{len(names)} files, {OUT.stat().st_size:,} bytes -> {OUT.name}")
    if hits:
        OUT.unlink()
        print(f"REFUSED: secret or forbidden content in {sorted(set(hits))}; code.zip deleted", file=sys.stderr)
        return 1
    print("secret scan: clean (no API-key patterns, no .env values, no .env, no dataset/)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
