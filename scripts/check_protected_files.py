"""Fail when confidential or local-only files are tracked by Git.

    python scripts/check_protected_files.py            # files in the index (what the next commit contains)
    python scripts/check_protected_files.py --history  # every path in every commit, for CI and before sharing

A .gitignore rule does not remove a file that was already committed, so the history check matters before a
branch or repository is shared. Standard library only, so it runs before any dependency is installed.
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Customer material, credentials and local state. Customer documents arrive as PDFs, Office files and mail.
PROTECTED = [
    ".env",
    ".env.*",
    "*.local.*",
    "compose.override.yaml",
    "compose.override.yml",
    "*.sqlite3",
    "*.sqlite3-*",
    "*.db",
    "data/*",
    ".azure/*",
    "infra/*.json",
    "*.pdf",
    "*.doc",
    "*.docx",
    "*.xls",
    "*.xlsx",
    "*.ppt",
    "*.pptx",
    "*.msg",
    "*.eml",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
]
ALLOWED = {".env.example", "compose.override.example.yaml"}
# Paths that must stay ignored, so new local files never get staged by accident.
MUST_BE_IGNORED = [
    ".env",
    "flows.local.yaml",
    "scenarios.local.yaml",
    "compose.override.yaml",
    "data/sessions.sqlite3",
    ".azure/demo/demo-access-code",
]


def protected(path: str) -> bool:
    if path in ALLOWED:
        return False
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, rule) or fnmatch.fnmatch(name, rule) for rule in PROTECTED)


def git(*args: str) -> list[str]:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True)
    return [line for line in result.stdout.splitlines() if line]


def is_git_checkout() -> bool:
    try:
        return git("rev-parse", "--is-inside-work-tree") == ["true"]
    except (OSError, subprocess.CalledProcessError):
        return False


def problems(history: bool = False) -> list[str]:
    found = [f"tracked: {path}" for path in git("ls-files") if protected(path)]
    if history:
        seen = set(git("log", "--all", "--format=", "--name-only"))
        found += [f"in history: {path}" for path in sorted(seen) if protected(path) and f"tracked: {path}" not in found]
    for path in MUST_BE_IGNORED:
        ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT).returncode == 0
        if not ignored:
            found.append(f"not ignored by .gitignore: {path}")
    return found


def main(argv: list[str]) -> int:
    if not is_git_checkout():
        print("Not a Git checkout: nothing to check.")
        return 0
    found = problems(history="--history" in argv)
    if found:
        print("Protected or local-only files are tracked by Git:")
        for item in found:
            print(f"  - {item}")
        print("Remove them from the index (git rm --cached <path>). If they were ever committed, rewrite the")
        print("history before sharing: a .gitignore rule does not remove committed files.")
        return 1
    scope = "the index and the full history" if "--history" in argv else "the index"
    print(f"No protected local files in {scope}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
