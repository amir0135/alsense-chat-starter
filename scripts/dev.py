"""Cross-platform task runner: the same commands on macOS, Linux and Windows, without Bash or make.

    uv run python scripts/dev.py <task> [options]

Tasks:
    setup           Install the pinned dependencies, create .env and the data folder, then run the doctor
    start           Run the demo on http://127.0.0.1:APP_PORT (alias: demo). Add --reload while editing code
    test            Run all tests, contract checks and evaluations
    lint            Check formatting, lint, types, widget JavaScript and protected files
    format          Apply formatting and safe lint fixes
    reset           Delete the local demo database (sessions, drafts, audit trail). --docker resets Compose
    doctor          Check the setup and explain what to fix
    validate-flows  Validate the shipped flows and FLOWS_FILE, or the files you name
    evals           Run the evaluation cases in evals/ (add --mode model, or folders such as evals/local)
    fixtures        Regenerate the contract fixtures and JSON Schemas in fixtures/
    export          Write a history-free zip of the starter to dist/

Behind a corporate package mirror, set UV_DEFAULT_INDEX (shell or .env) and run setup without uv's own sync:
    uv run --no-sync python scripts/dev.py setup
The exact locked versions then install from the mirror with hash checking. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from doctor import read_env_file, running_app  # noqa: E402

SAMPLE_FLOWS = "src/alsense_chat/troubleshooting.yaml"


def settings() -> dict[str, str]:
    path = ROOT / ".env"
    values = read_env_file(path) if path.is_file() else {}
    return {**values, **os.environ}


def mirror() -> str:
    values = settings()
    return values.get("UV_DEFAULT_INDEX", "") or values.get("UV_INDEX_URL", "")


def uv() -> str:
    found = shutil.which("uv")
    if not found:
        sys.exit(
            "uv is not installed: https://docs.astral.sh/uv/getting-started/installation/\n"
            "Or run the demo without a local setup: docker compose up --build"
        )
    return found


def run(*command: str, env: dict[str, str] | None = None) -> int:
    print(
        "$ " + " ".join(Path(command[0]).name if index == 0 else part for index, part in enumerate(command)), flush=True
    )
    return subprocess.call(list(command), cwd=ROOT, env=env)


def uv_run(*args: str) -> list[str]:
    # With a mirror, never let uv re-sync from the PyPI URLs recorded in uv.lock.
    return [uv(), "run", "--no-sync" if mirror() else "--frozen", *args]


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def setup(_: argparse.Namespace, extra: list[str]) -> int:
    tool = uv()
    if not (ROOT / ".env").exists():
        shutil.copyfile(ROOT / ".env.example", ROOT / ".env")
        print("Created .env from .env.example: synthetic mode, no secrets.")
    (ROOT / "data").mkdir(exist_ok=True)
    index = mirror()
    if index:
        print(f"Installing the locked versions from {index}, hash-checked.")
        env = {**os.environ, "UV_DEFAULT_INDEX": index}
        with tempfile.TemporaryDirectory() as folder:
            requirements = str(Path(folder) / "requirements.txt")
            steps = [
                [tool, "venv", "--allow-existing"],
                [tool, "export", "--frozen", "--format", "requirements-txt", "--no-emit-project", "-o", requirements],
                [tool, "pip", "sync", "--require-hashes", requirements],
                [tool, "pip", "install", "--no-deps", "--editable", "."],
            ]
            for step in steps:
                if run(*step, env=env):
                    return 1
    elif run(tool, "sync", "--frozen"):
        return 1
    print()
    return run(*uv_run("python", "scripts/doctor.py"))


def start(args: argparse.Namespace, _: list[str]) -> int:
    port = int(args.port or settings().get("APP_PORT") or 8001)
    if port_in_use(port):
        if running_app(port):
            print(f"The demo is already running: http://127.0.0.1:{port}")
            return 0
        print(f"Port {port} is used by another program. Stop it, or set APP_PORT in .env.")
        return 1
    env_file = ["--env-file", ".env"] if (ROOT / ".env").is_file() else []
    command = uv_run(*env_file, "uvicorn", "alsense_chat.app:create_app", "--factory")
    command += ["--host", args.host, "--port", str(port)]
    if args.reload:
        command += ["--reload", "--reload-dir", "src"]
    print(f"Starting the demo: http://127.0.0.1:{port}   (Ctrl+C stops it)")
    try:
        return run(*command)
    except KeyboardInterrupt:
        return 0


def test(_: argparse.Namespace, extra: list[str]) -> int:
    code = run(*uv_run("pytest", *extra))
    local = settings().get("FLOWS_FILE", "")
    if local:
        code = run(*uv_run("python", "-m", "alsense_chat.flows", local)) or code
    return code


def lint(_: argparse.Namespace, extra: list[str]) -> int:
    failures = 0
    for command in (["ruff", "format", "--check", "."], ["ruff", "check", "."], ["mypy", "src"]):
        failures += bool(run(*uv_run(*command)))
    node = shutil.which("node")
    if node:
        for script in sorted((ROOT / "src" / "alsense_chat" / "static").glob("*.js")):
            failures += bool(run(node, "--check", str(script.relative_to(ROOT))))
    else:
        print("node not found: skipped the JavaScript syntax check (CI runs it).")
    failures += bool(run(sys.executable, "scripts/check_protected_files.py"))
    return 1 if failures else 0


def format_code(_: argparse.Namespace, extra: list[str]) -> int:
    return run(*uv_run("ruff", "format", ".")) or run(*uv_run("ruff", "check", "--fix", "."))


def reset(args: argparse.Namespace, _: list[str]) -> int:
    if args.docker:
        return run("docker", "compose", "down", "--volumes")
    values = settings()
    port = int(values.get("APP_PORT") or 8001)
    if running_app(port) and not args.force:
        print(f"A demo is running on port {port}. Stop it first (Ctrl+C), then reset, or add --force.")
        return 1
    database = Path(values.get("SESSION_DB") or "data/sessions.sqlite3")
    database = database if database.is_absolute() else ROOT / database
    removed = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(database) + suffix)
        if candidate.is_file():
            candidate.unlink()
            removed.append(candidate.name)
    if removed:
        print(f"Removed {', '.join(removed)}: sessions, drafts and the audit trail start empty.")
    else:
        print(f"No demo data at {database}: nothing to reset.")
    return 0


def doctor(_: argparse.Namespace, extra: list[str]) -> int:
    return run(*uv_run("python", "scripts/doctor.py", *extra))


def validate_flows(args: argparse.Namespace, _: list[str]) -> int:
    local = settings().get("FLOWS_FILE", "")
    files = args.files or [SAMPLE_FLOWS, *([local] if local else [])]
    return run(*uv_run("python", "-m", "alsense_chat.flows", *files))


def evals(_: argparse.Namespace, extra: list[str]) -> int:
    # Model mode reads AGENT_MODE and the Foundry settings from .env; scripted mode never needs them.
    env_file = ["--env-file", ".env"] if (ROOT / ".env").is_file() else []
    return run(*uv_run(*env_file, "python", "-m", "alsense_chat.evaluation", *extra))


def fixtures(_: argparse.Namespace, extra: list[str]) -> int:
    return run(*uv_run("python", "scripts/fixtures.py"))


def export(_: argparse.Namespace, extra: list[str]) -> int:
    return run(sys.executable, "export.py", *extra)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dev.py", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    tasks = parser.add_subparsers(dest="task", required=True, metavar="task")
    tasks.add_parser("setup", help="install pinned dependencies, create .env, run the doctor").set_defaults(run=setup)
    for name in ("start", "demo"):
        command = tasks.add_parser(name, help="run the demo locally")
        command.add_argument("--port", type=int, help="defaults to APP_PORT, then 8001")
        command.add_argument("--host", default="127.0.0.1", help="interface to listen on (default 127.0.0.1)")
        command.add_argument("--reload", action="store_true", help="restart on code changes")
        command.set_defaults(run=start)
    tasks.add_parser("test", help="tests, contract checks and evaluations").set_defaults(run=test)
    tasks.add_parser("lint", help="formatting, lint, types, JavaScript, protected files").set_defaults(run=lint)
    tasks.add_parser("format", help="apply formatting and safe fixes").set_defaults(run=format_code)
    command = tasks.add_parser("reset", help="delete the local demo database")
    command.add_argument("--docker", action="store_true", help="reset Docker Compose instead (down --volumes)")
    command.add_argument("--force", action="store_true", help="reset even if a demo answers on APP_PORT")
    command.set_defaults(run=reset)
    tasks.add_parser("doctor", help="check the setup").set_defaults(run=doctor)
    command = tasks.add_parser("validate-flows", help="validate troubleshooting flow files")
    command.add_argument("files", nargs="*")
    command.set_defaults(run=validate_flows)
    tasks.add_parser("evals", help="run the evaluation cases").set_defaults(run=evals)
    tasks.add_parser("fixtures", help="regenerate contract fixtures").set_defaults(run=fixtures)
    tasks.add_parser("export", help="history-free zip in dist/").set_defaults(run=export)
    args, extra = parser.parse_known_args(argv)
    return args.run(args, extra)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
