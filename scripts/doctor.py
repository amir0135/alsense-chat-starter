"""Setup doctor: checks everything the demo needs and says exactly what to fix.

    uv run python scripts/doctor.py         # full check
    uv run python scripts/doctor.py --ci    # skip the port and running-app checks

Works with any Python 3.8+ for the first checks, so it can explain a missing interpreter or dependencies.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_protected_files  # noqa: E402

TESTED_UV = (0, 9, 11)
IN_CONTAINER = os.environ.get("ALSENSE_CONTAINER") == "1"
MARKS = {"ok": "✓", "warn": "!", "fail": "✗", "info": "·"}
ASCII = {"ok": "[ok]", "warn": "[!!]", "fail": "[XX]", "info": "[..]"}


class Report:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str, str]] = []

    def add(self, state: str, message: str, hint: str = "") -> None:
        self.lines.append((state, message, hint))

    @property
    def failed(self) -> bool:
        return any(state == "fail" for state, _, _ in self.lines)

    def render(self) -> str:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            "".join(MARKS.values()).encode(encoding)
            marks = MARKS
        except (UnicodeEncodeError, LookupError):
            marks = ASCII
        out = []
        for state, message, hint in self.lines:
            out.append(f"{marks[state]} {message}")
            if hint:
                out.extend(f"    {line}" for line in hint.splitlines())
        return "\n".join(out)


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser matching `uv run --env-file`: KEY=VALUE lines, comments and optional quotes."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def effective_environment() -> tuple[dict[str, str], bool]:
    env_file = ROOT / ".env"
    file_values = read_env_file(env_file) if env_file.is_file() else {}
    # Like `uv run --env-file`, variables already set in the shell win over the file.
    return {**file_values, **os.environ}, env_file.is_file()


def check_python(report: Report) -> bool:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info >= (3, 12):
        report.add("ok", f"Python {version} supported")
        return True
    report.add(
        "fail",
        f"Python {version} is too old: the app needs Python 3.12+",
        "Let uv provide it: uv run python scripts/doctor.py (uv installs Python 3.12 from .python-version).",
    )
    return False


def check_uv(report: Report) -> None:
    if IN_CONTAINER:
        return
    uv = shutil.which("uv")
    if not uv:
        report.add(
            "fail",
            "uv is not installed",
            "Install it: https://docs.astral.sh/uv/getting-started/installation/\n"
            "Or skip local setup and use Docker: docker compose up --build",
        )
        return
    output = subprocess.run([uv, "--version"], capture_output=True, text=True).stdout
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
    found = tuple(int(part) for part in match.groups()) if match else (0, 0, 0)
    tested = ".".join(map(str, TESTED_UV))
    if found < TESTED_UV[:2] + (0,):
        report.add(
            "warn", f"uv {'.'.join(map(str, found))} is older than the tested {tested}", "Update: uv self update"
        )
    else:
        report.add("ok", f"uv {'.'.join(map(str, found))} available (tested with {tested})")
    # uv.lock records PyPI; checking it against a configured mirror would always report a mismatch.
    clean = {key: value for key, value in os.environ.items() if key not in {"UV_DEFAULT_INDEX", "UV_INDEX_URL"}}
    lock = subprocess.run([uv, "lock", "--check", "--offline"], cwd=ROOT, capture_output=True, text=True, env=clean)
    if lock.returncode == 0:
        report.add("ok", "uv.lock matches pyproject.toml")
    else:
        report.add("warn", "uv.lock does not match pyproject.toml", "Update it deliberately: uv lock")


def check_dependencies(report: Report) -> bool:
    missing = []
    for module in ("fastapi", "uvicorn", "pydantic", "yaml", "alsense_chat"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        report.add(
            "fail",
            f"Dependencies missing ({', '.join(missing)})",
            "Install the pinned versions: uv run python scripts/dev.py setup",
        )
        return False
    report.add("ok", "Dependencies installed from the lock file")
    return True


def check_env_file(report: Report, exists: bool) -> None:
    if IN_CONTAINER:
        report.add("info", "Running in a container: configuration comes from Docker Compose and .env")
        return
    if exists:
        report.add("ok", ".env found")
    else:
        report.add("warn", ".env not found: using safe synthetic defaults", "Create it: cp .env.example .env")


def check_settings(report: Report, environ: dict[str, str]):
    from alsense_chat.config import load_settings

    try:
        settings = load_settings(environ, validate=False)
    except ValueError as exc:
        report.add("fail", f"Configuration invalid: {exc}", "Compare .env with .env.example.")
        return None
    problems = settings.problems()
    for problem in problems:
        report.add("fail", problem, "Fix it in .env (see the comments in .env.example).")
    if not problems:
        report.add("ok", "Configuration valid")
    return settings


def check_mode(report: Report, settings) -> None:
    if settings.agent_mode == "demo":
        report.add("ok", "Scripted assistant selected: no AI model, no Azure credentials needed")
    elif settings.foundry_endpoint and settings.deployment:
        report.add("ok", f"Azure AI Foundry mode: deployment {settings.deployment}")
        az = shutil.which("az")
        signed_in = (
            bool(az)
            and subprocess.run([az, "account", "show", "--output", "none"], capture_output=True, timeout=30).returncode
            == 0
        )
        if signed_in or os.environ.get("AZURE_CLIENT_ID"):
            report.add("ok", "Azure credentials available")
        else:
            report.add(
                "warn",
                "No Azure sign-in found for Foundry mode",
                "Run az login, or set AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET.",
            )
    if settings.data_source == "synthetic":
        report.add("ok", "Synthetic data source: nothing connects to Alsense")
    else:
        from alsense_chat.adapters import build_services
        from alsense_chat.adapters.errors import IntegrationError

        try:
            build_services(settings.data_source)
        except IntegrationError as exc:
            report.add("fail", str(exc))
        else:
            report.add("ok", "Alsense adapters configured")
    if settings.synthetic_faults:
        report.add("warn", f"Synthetic faults on for every session: {', '.join(sorted(settings.synthetic_faults))}")


def check_flows(report: Report, settings):
    from alsense_chat.flows import FlowError, available, load_flows

    try:
        flows = load_flows(settings.flows_file)
    except (FlowError, OSError) as exc:
        report.add(
            "fail", "Troubleshooting flows invalid", f"{exc}\nCheck: uv run python scripts/dev.py validate-flows"
        )
        return None
    except Exception as exc:  # Report, never crash: the remaining checks still matter.
        report.add("fail", f"Troubleshooting flows unreadable ({type(exc).__name__})", "Check FLOWS_FILE.")
        return None
    summary = ", ".join(f"{flow_id} v{flow['version']}" for flow_id, flow in flows.items() if available(flow))
    if not settings.flows_file:
        report.add("ok", f"Sample flow loaded: {summary or 'none offered'}")
        return flows
    report.add("ok", f"Local flow file loaded: {summary or 'none offered'}")
    path = Path(settings.flows_file).resolve()
    if check_protected_files.is_git_checkout() and path.is_relative_to(ROOT):
        relative = str(path.relative_to(ROOT))
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", relative], cwd=ROOT, capture_output=True)
        if tracked.returncode == 0:
            report.add("fail", f"{relative} is tracked by Git", "Customer flows must stay local: git rm --cached it.")
        else:
            report.add("ok", f"{relative} is not tracked by Git")
    return flows


def check_scenarios(report: Report, settings, flows) -> None:
    from alsense_chat.adapters.synthetic import PERSONAS
    from alsense_chat.scenarios import ScenarioError, load_scenarios, unavailable_reason

    # Demo personas exist only with synthetic identities; with a real sign-in, scenarios report unavailable.
    synthetic = settings.data_source == "synthetic"
    personas = {name for name, value in PERSONAS.items() if value["internal"]} if synthetic else None
    try:
        scenarios = load_scenarios(settings.scenarios_file, personas)
    except (ScenarioError, OSError) as exc:
        report.add("fail", "Demo scenarios invalid", str(exc))
        return
    available = personas if personas is not None else set()
    ready = [
        s
        for s in scenarios
        if flows is not None and not unavailable_reason(s, flows, settings.features, settings.data_source, available)
    ]
    state = "ok" if len(ready) == len(scenarios) else "warn"
    report.add(state, f"Demo scenarios: {len(ready)} of {len(scenarios)} available")


def check_database(report: Report, settings) -> None:
    from alsense_chat.store import SCHEMA_VERSION, SchemaError, Store

    try:
        status = Store(settings.db).status()
    except SchemaError as exc:
        report.add("fail", str(exc))
    except Exception as exc:  # Any database failure is a setup problem to report, not to raise.
        report.add(
            "fail",
            f"Database not writable at {settings.db} ({type(exc).__name__})",
            "Check SESSION_DB in .env and that its folder is writable.",
        )
    else:
        report.add("ok", f"Database writable: {settings.db} (schema v{status['schema_version']} of {SCHEMA_VERSION})")


def running_app(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            health = json.load(response)
    except (OSError, ValueError):
        return None
    return health if isinstance(health, dict) and "data_source" in health else None


def check_port(report: Report, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            busy = True
        else:
            busy = False
    if not busy:
        report.add("ok", f"Port {port} available")
        return False
    if running_app(port):
        report.add("ok", f"The demo is already running on port {port}")
        return True
    report.add("fail", f"Port {port} is used by another program", "Stop that program, or set APP_PORT in .env.")
    return False


def check_running_app(report: Report, port: int) -> None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/system", timeout=3) as response:
            status = json.load(response)
    except urllib.error.HTTPError as exc:
        hint = "Restart it to pick up this version: stop it, then run uv run python scripts/dev.py start"
        if exc.code == 404:
            report.add("warn", f"The app on port {port} is an older version of this demo", hint)
        else:
            report.add("warn", f"The running app answered /api/system with HTTP {exc.code}", hint)
        return
    except (OSError, ValueError):
        report.add("warn", "The running app did not answer /api/system")
        return
    label = status.get("overall", {}).get("label", "unknown")
    state = "ok" if status.get("overall", {}).get("state") != "attention" else "warn"
    report.add(state, f"Running app reports: {label} (http://127.0.0.1:{port}/system)")


def check_git(report: Report) -> None:
    if IN_CONTAINER or not check_protected_files.is_git_checkout():
        report.add("info", "Not a Git checkout: tracked-file check skipped")
        return
    found = check_protected_files.problems()
    if found:
        report.add("fail", "Protected local files are tracked or not ignored", "\n".join(found))
    else:
        report.add("ok", "No protected local file tracked by Git")


def main(argv: list[str]) -> int:
    ci = "--ci" in argv
    report = Report()
    environ, env_exists = effective_environment()
    print("Alsense chat starter - setup doctor\n")
    python_ok = check_python(report)
    check_uv(report)
    running = False
    port = 8001
    if python_ok and check_dependencies(report):
        check_env_file(report, env_exists)
        settings = check_settings(report, environ)
        if settings is not None:
            port = settings.app_port
            check_mode(report, settings)
            flows = check_flows(report, settings)
            check_scenarios(report, settings, flows)
            check_database(report, settings)
            if IN_CONTAINER:
                running = running_app(port) is not None
            elif not ci:
                running = check_port(report, port)
    check_git(report)
    if running:
        check_running_app(report, port)
    print(report.render())
    print()
    if report.failed:
        print("Not ready: fix the items marked above, then run the doctor again.")
        return 1
    if running:
        print(f"Ready: the demo is running at http://127.0.0.1:{port}")
    else:
        print(f"Ready: uv run python scripts/dev.py start   ->   http://127.0.0.1:{port}")
        print("   or: docker compose up --build")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
