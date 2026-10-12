"""Create an allowlisted, history-free export. Never traverse the enclosing repository."""

import sys
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent
FILES = [".gitignore", ".dockerignore", ".env.example", "README.md", "pyproject.toml",
         "export.py", "Dockerfile", ".github/workflows/tests.yml",
         "docs/architecture.md", "docs/integration.md", "docs/azure-demo.md",
         "infra/main.bicep", "infra/resources.bicep", "infra/deploy.sh", "infra/deploy.py"]


def export(destination: Path) -> None:
    paths = [ROOT / name for name in FILES]
    paths += sorted((ROOT / "src" / "alsense_chat").glob("*.py"))
    paths += [ROOT / "src" / "alsense_chat" / "alsense.semantic.yaml"]
    paths += sorted((ROOT / "tests").glob("*.py"))
    paths += [ROOT / "src" / "alsense_chat" / "static" / name for name in ("index.html", "widget.css", "widget.js")]
    if any(not path.is_file() or path.is_symlink() for path in paths):
        raise ValueError("Export requires every allowlisted file; symlinks are not allowed.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "x") as archive:
        for path in paths:
            archive.write(path, Path("alsense-chat-starter") / path.relative_to(ROOT))


if __name__ == "__main__":
    export(Path(sys.argv[1]) if len(sys.argv) == 2 else ROOT / "dist" / "alsense-chat-starter.zip")
