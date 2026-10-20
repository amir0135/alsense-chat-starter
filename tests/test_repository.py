"""Clone-and-run guarantees: pinned tooling, safe defaults and bootstrap scripts that run anywhere."""

import ast
import importlib.util
import json
import re
import socket
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ["scripts/dev.py", "scripts/doctor.py", "scripts/check_protected_files.py", "export.py"]


def load(relative: str):
    spec = importlib.util.spec_from_file_location(Path(relative).stem, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_scripts_run_on_old_system_pythons():
    for relative in BOOTSTRAP:
        source = (ROOT / relative).read_text()
        ast.parse(source, feature_version=(3, 8))
        assert "from __future__ import annotations" in source, relative
        assert ".removeprefix(" not in source and ".removesuffix(" not in source, relative


def test_images_are_pinned_by_digest_and_uv_versions_agree():
    dockerfile = (ROOT / "Dockerfile").read_text()
    devcontainer = (ROOT / ".devcontainer" / "Dockerfile").read_text()
    for text in (dockerfile, devcontainer):
        for image in re.findall(r"(?:FROM|--from=)\s*(\S+)", text):
            assert re.search(r"@sha256:[0-9a-f]{64}$", image), image
    versions = {
        re.search(r"uv:(\d+\.\d+\.\d+)", dockerfile).group(1),
        re.search(r"uv:(\d+\.\d+\.\d+)", devcontainer).group(1),
        re.search(r'version: "(\d+\.\d+\.\d+)"', (ROOT / ".github/workflows/ci.yml").read_text()).group(1),
        ".".join(map(str, load("scripts/doctor.py").TESTED_UV)),
    }
    assert len(versions) == 1, versions
    assert (ROOT / ".python-version").read_text().strip() == "3.12"


def test_compose_publishes_on_loopback_only_and_keeps_data_in_a_volume():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    app = compose["services"]["app"]
    assert all(port.startswith("127.0.0.1:") for port in app["ports"])
    assert "demo-data:/data" in app["volumes"] and "demo-data" in compose["volumes"]
    assert app["read_only"] is True and app["environment"]["FLOWS_FILE"] == ""
    assert {"path": ".env", "required": False} in app["env_file"]


def test_docker_context_is_an_allowlist():
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    assert lines[lines.index("*")] == "*"
    allowed = {line[1:] for line in lines if line.startswith("!")}
    assert allowed == {"pyproject.toml", "uv.lock", ".python-version", "src/", "scripts/"}


def test_devcontainer_forwards_the_demo_port_and_runs_setup():
    config = json.loads((ROOT / ".devcontainer" / "devcontainer.json").read_text())
    assert config["forwardPorts"] == [8001]
    assert "scripts/dev.py setup" in config["postCreateCommand"]


def test_protected_file_rules():
    module = load("scripts/check_protected_files.py")
    for path in (
        "flows.local.yaml",
        ".env",
        ".env.production",
        "data/sessions.sqlite3",
        "docs/tree.pdf",
        "Support Tickets.xlsx",
        ".azure/demo/demo-access-code",
        "compose.override.yaml",
    ):
        assert module.protected(path), path
    for path in (".env.example", "compose.override.example.yaml", "src/alsense_chat/app.py", "fixtures/a.json"):
        assert not module.protected(path), path
    assert module.main(["--history"]) == 0


def test_doctor_reads_env_files_like_uv(tmp_path):
    doctor = load("scripts/doctor.py")
    env = tmp_path / ".env"
    env.write_text('# comment\nAGENT_MODE=demo\nexport APP_PORT="9001"\nEMPTY=\nNOT A LINE\n')
    assert doctor.read_env_file(env) == {"AGENT_MODE": "demo", "APP_PORT": "9001", "EMPTY": ""}


def test_doctor_flags_a_port_held_by_another_program():
    doctor = load("scripts/doctor.py")
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        report = doctor.Report()
        doctor.check_port(report, busy.getsockname()[1])
    assert report.failed and "used by another program" in report.render()


def test_doctor_reports_bad_flow_files_instead_of_crashing(tmp_path):
    doctor = load("scripts/doctor.py")
    from alsense_chat.config import Settings

    bad = tmp_path / "flows.local.yaml"
    bad.write_text("flows:\n  x:\n    title: X\n    start: [a]\n    nodes:\n      a: {outcome: Done, kind: resolve}\n")
    report = doctor.Report()
    assert doctor.check_flows(report, Settings(flows_file=str(bad))) is None
    assert report.failed and "start must name a node" in report.render()


def test_doctor_falls_back_to_ascii_marks(monkeypatch):
    doctor = load("scripts/doctor.py")
    report = doctor.Report()
    report.add("ok", "fine")
    monkeypatch.setattr("sys.stdout", type("Out", (), {"encoding": "ascii"})())
    assert report.render() == "[ok] fine"
