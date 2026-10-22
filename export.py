"""Create an allowlisted, history-free export. Never traverse the enclosing repository."""

from __future__ import annotations

import fnmatch
import sys
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
FILES = [
    ".gitignore",
    ".dockerignore",
    ".env.example",
    ".python-version",
    "README.md",
    "PRODUCT.md",
    "DESIGN.md",
    "pyproject.toml",
    "uv.lock",
    "export.py",
    "Dockerfile",
    "compose.yaml",
    "compose.override.example.yaml",
    "Makefile",
    ".devcontainer/devcontainer.json",
    ".devcontainer/Dockerfile",
    ".vscode/tasks.json",
    ".github/workflows/ci.yml",
    ".gitleaks.toml",
    "fixtures/README.md",
    "evals/README.md",
    "docs/architecture.drawio",
    "infra/main.bicep",
    "infra/resources.bicep",
    "infra/deploy.sh",
    "infra/deploy.py",
]
# Each pattern must match at least one file, so a renamed folder can't silently drop out of the export.
PATTERNS = [
    "docs/*.md",
    "src/alsense_chat/*.py",
    "src/alsense_chat/*.yaml",
    "src/alsense_chat/adapters/**/*.py",
    "src/alsense_chat/static/*.html",
    "src/alsense_chat/static/*.css",
    "src/alsense_chat/static/*.js",
    "tests/*.py",
    "scripts/*.py",
    "fixtures/**/*.json",
    "evals/synthetic/*.yaml",
]
# Customer material, credentials and local state never leave the machine, whatever the patterns say.
NEVER = [".env", "*.local.*", "compose.override.yaml", "*.sqlite3*", "*.pem", "*.key", "*.pfx"]


def files() -> list[Path]:
    paths = [ROOT / name for name in FILES]
    for pattern in PATTERNS:
        matches = sorted(path for path in ROOT.glob(pattern) if path.is_file() and "__pycache__" not in path.parts)
        if not matches:
            raise ValueError(f"Export pattern matched nothing: {pattern}")
        paths += matches
    if any(not path.is_file() or path.is_symlink() for path in paths):
        raise ValueError("Export requires every allowlisted file; symlinks are not allowed.")
    blocked = [path for path in paths if any(fnmatch.fnmatch(path.name, rule) for rule in NEVER)]
    if blocked:
        raise ValueError(
            f"Refusing to export local or secret files: {[str(path.relative_to(ROOT)) for path in blocked]}"
        )
    return sorted(set(paths))


def export(destination: Path) -> None:
    paths = files()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "x") as archive:
        for path in paths:
            archive.write(path, Path("alsense-chat-starter") / path.relative_to(ROOT))


if __name__ == "__main__":
    export(Path(sys.argv[1]) if len(sys.argv) == 2 else ROOT / "dist" / "alsense-chat-starter.zip")
