"""Non-secret readiness report for the /system page and the setup doctor.

It shows where every capability's data comes from, so nobody mistakes a synthetic result for Alsense data.
Never add endpoints, keys, connection strings, tokens or access codes here.
"""

import functools
import importlib.metadata
import re
import subprocess

from .adapters.synthetic.faults import FAULTS
from .config import ROOT, Settings
from .flows import available
from .scenarios import unavailable_reason

REPOSITORY = ROOT.parents[1]
STATES = {
    "synthetic": "Synthetic",
    "configured": "Configured",
    "unavailable": "Unavailable",
    "future": "Future integration",
    "disabled": "Disabled",
    "attention": "Needs attention",
}


def app_version() -> str:
    try:
        return importlib.metadata.version("alsense-chat-starter")
    except importlib.metadata.PackageNotFoundError:
        pyproject = REPOSITORY / "pyproject.toml"
        match = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.MULTILINE) if pyproject.is_file() else None
        return match.group(1) if match else "unknown"


@functools.cache
def git_commit() -> str:
    if not (REPOSITORY / ".git").exists():
        return ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def _item(key: str, label: str, value: str, state: str, detail: str = "") -> dict:
    return {"id": key, "label": label, "value": value, "state": state, "state_label": STATES[state], "detail": detail}


def _service(settings: Settings, kind: str, feature: str = "") -> tuple[str, str]:
    if feature and feature not in settings.features:
        return "Turned off", "disabled"
    return ("Synthetic", "synthetic") if kind == "synthetic" else ("Alsense API", "configured")


def system_status(state) -> dict:
    settings: Settings = state.settings
    kind = state.services.kind
    flows: dict = state.flows
    offered = [flow_id for flow_id, flow in flows.items() if available(flow)]
    scenario_reasons = [unavailable_reason(s, flows, settings.features, kind) for s in state.scenarios]

    items = []
    if settings.agent_mode == "foundry":
        items.append(
            _item(
                "assistant",
                "Assistant",
                f"Azure AI Foundry · {settings.deployment}",
                "configured",
                "Uses your Foundry deployment with backend-owned tools. Connectivity is checked per request.",
            )
        )
    else:
        items.append(
            _item(
                "assistant",
                "Assistant",
                "Scripted demo assistant (no AI model)",
                "synthetic",
                "Deterministic replies from the backend tools. Set AGENT_MODE=foundry for a model.",
            )
        )
    items.append(
        _item(
            "data_source",
            "Data source",
            "Synthetic APIs" if kind == "synthetic" else "Alsense APIs",
            "synthetic" if kind == "synthetic" else "configured",
            "No call reaches Alsense." if kind == "synthetic" else "",
        )
    )
    for key, label, feature in (
        ("sites", "Site directory", ""),
        ("health", "Health-check service", "troubleshooting"),
        ("reports", "Report service", "reports"),
        ("support", "Support submission", "support"),
        ("knowledge", "Guidance search", ""),
    ):
        value, item_state = _service(settings, kind, feature)
        items.append(_item(key, label, value, item_state))
    items.append(
        _item(
            "search",
            "Azure AI Search",
            "Not configured",
            "future",
            "Guidance comes from the synthetic search adapter until an index is connected.",
        )
    )

    if "troubleshooting" not in settings.features:
        items.append(_item("flows", "Troubleshooting flows", "Turned off", "disabled"))
    elif settings.flows_file:
        items.append(
            _item(
                "flows",
                "Troubleshooting flows",
                f"Local file · {len(offered)} offered",
                "configured",
                "Loaded from FLOWS_FILE. Keep it out of Git.",
            )
        )
    else:
        items.append(_item("flows", "Troubleshooting flows", f"Synthetic sample · {len(offered)} offered", "synthetic"))

    ready = sum(1 for reason in scenario_reasons if not reason)
    items.append(
        _item(
            "scenarios",
            "Demo scenarios",
            f"{ready} of {len(scenario_reasons)} available",
            "configured" if ready else "attention",
            "Loaded from SCENARIOS_FILE." if settings.scenarios_file else "Synthetic scenarios.",
        )
    )

    try:
        database = state.store.status()
    except Exception:  # Any database failure is reported, never raised, on this page.
        items.append(
            _item(
                "database",
                "Session database",
                "Unavailable",
                "unavailable",
                "Run the setup doctor: uv run python scripts/doctor.py",
            )
        )
    else:
        current = database["current"]
        items.append(
            _item(
                "database",
                "Session database",
                f"Connected · SQLite schema v{database['schema_version']}",
                "configured" if current else "attention",
                "Local single-instance storage." if current else "Schema is out of date.",
            )
        )

    personas = ", ".join(name.removeprefix("Synthetic ") for name in state.persona_names)
    items.append(
        _item(
            "auth",
            "Authentication",
            f"Demo identities ({personas})" + (" behind an access code" if settings.cloud_demo else ""),
            "synthetic",
            "Real sign-in is not implemented.",
        )
    )
    items.append(
        _item(
            "telemetry",
            "Telemetry",
            "Application Insights" if settings.telemetry_connection else "Off",
            "configured" if settings.telemetry_connection else "disabled",
        )
    )
    faults = sorted(settings.synthetic_faults)
    items.append(
        _item(
            "faults",
            "Synthetic faults",
            ", ".join(faults) if faults else "None",
            "attention" if faults else "synthetic",
            "Applied to every session by SYNTHETIC_FAULTS. Scenarios add faults to their own session only.",
        )
    )

    blocked = [item for item in items if item["state"] in {"unavailable", "attention"} and item["id"] != "faults"]
    overall = "Needs attention" if blocked else ("Demo ready · faults on" if faults else "Demo ready")
    return {
        "version": app_version(),
        "commit": settings.app_commit or git_commit() or "not recorded",
        "overall": {"label": overall, "state": "attention" if blocked else "configured"},
        "notice": notice(settings, kind),
        "items": items,
        "flows": [
            {
                "title": flow["title"],
                "version": flow["version"],
                "status": flow["status"],
                "owner": flow["owner"],
                "offered": available(flow),
            }
            for flow in flows.values()
        ],
        "features": sorted(settings.features),
        "fault_catalog": FAULTS,
    }


def notice(settings: Settings, kind: str) -> str:
    if kind != "synthetic":
        return "Connected to the configured Alsense APIs."
    where = "Hosted" if settings.cloud_demo else "Local"
    return f"{where} reference demo. Uses synthetic data and does not connect to Alsense."
