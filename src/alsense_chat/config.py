import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .adapters.synthetic.faults import FAULTS

ROOT = Path(__file__).resolve().parent
FEATURES = frozenset({"reports", "troubleshooting", "support"})
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

# Every variable load_settings reads. tests/test_config.py keeps this list and .env.example in sync.
ENVIRONMENT = (
    "AGENT_MODE",
    "DATA_SOURCE",
    "APP_PORT",
    "LOG_LEVEL",
    "SESSION_DB",
    "RESET_DEMO_DATA_ON_START",
    "SESSION_TTL_SECONDS",
    "DEMO_LOGIN",
    "CHAT_ENABLED",
    "CLOUD_DEMO",
    "DEMO_ACCESS_CODE",
    "FOUNDRY_PROJECT_ENDPOINT",
    "MODEL_DEPLOYMENT",
    "FLOWS_FILE",
    "SCENARIOS_FILE",
    "ENABLE_REPORTS",
    "ENABLE_TROUBLESHOOTING",
    "ENABLE_SUPPORT_REQUESTS",
    "SYNTHETIC_FAULTS",
    "APPLICATIONINSIGHTS_CONNECTION_STRING",
    "APP_COMMIT",
)


@dataclass(frozen=True)
class Settings:
    demo_login: bool = True
    enabled: bool = True
    db: str = "data/sessions.sqlite3"
    ttl_seconds: int = 3600
    agent_mode: str = "demo"
    foundry_endpoint: str = ""
    deployment: str = ""
    cloud_demo: bool = False
    demo_access_code: str = ""
    telemetry_connection: str = ""
    flows_file: str = ""
    scenarios_file: str = ""
    data_source: str = "synthetic"
    synthetic_faults: frozenset[str] = frozenset()
    features: frozenset[str] = FEATURES
    app_port: int = 8001
    log_level: str = "INFO"
    reset_on_start: bool = False
    app_commit: str = ""

    def problems(self) -> list[str]:
        found = []
        if not self.demo_login:
            found.append("Real sign-in is not wired. Supply a trusted authentication provider before deployment.")
        if self.agent_mode not in {"demo", "foundry"}:
            found.append("AGENT_MODE must be demo (scripted, no AI model) or foundry (Azure AI Foundry).")
        if self.agent_mode == "foundry" and not (self.foundry_endpoint and self.deployment):
            found.append("Foundry mode requires FOUNDRY_PROJECT_ENDPOINT and MODEL_DEPLOYMENT.")
        if self.data_source not in {"synthetic", "alsense"}:
            found.append("DATA_SOURCE must be synthetic or alsense.")
        if self.synthetic_faults and self.data_source != "synthetic":
            found.append("SYNTHETIC_FAULTS only applies to DATA_SOURCE=synthetic.")
        unknown = sorted(self.synthetic_faults - set(FAULTS))
        if unknown:
            found.append(f"SYNTHETIC_FAULTS has unknown names {unknown}. Choose from: {', '.join(FAULTS)}.")
        if self.flows_file and not Path(self.flows_file).is_file():
            found.append("FLOWS_FILE must point to an existing troubleshooting flows YAML file.")
        if self.scenarios_file and not Path(self.scenarios_file).is_file():
            found.append("SCENARIOS_FILE must point to an existing demo scenarios YAML file.")
        if self.ttl_seconds < 60:
            found.append("SESSION_TTL_SECONDS must be at least 60.")
        if not 1 <= self.app_port <= 65535:
            found.append("APP_PORT must be between 1 and 65535.")
        if self.log_level not in LOG_LEVELS:
            found.append(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}.")
        if self.cloud_demo and len(self.demo_access_code) < 32:
            found.append("Cloud demo requires a high-entropy DEMO_ACCESS_CODE of at least 32 characters.")
        return found

    def validate(self) -> None:
        found = self.problems()
        if found:
            raise ValueError("\n".join(found))


def load_settings(environ: Mapping[str, str] | None = None, *, validate: bool = True) -> Settings:
    source = os.environ if environ is None else environ

    def get(name: str, default: str) -> str:
        assert name in ENVIRONMENT, name
        return source.get(name, default).strip()

    def flag(name: str, default: str) -> bool:
        value = get(name, default).lower()
        if value not in {"true", "false"}:
            raise ValueError(f"{name} must be true or false.")
        return value == "true"

    def number(name: str, default: str) -> int:
        value = get(name, default)
        if not value.isdigit():
            raise ValueError(f"{name} must be a whole number.")
        return int(value)

    switches = {
        "reports": "ENABLE_REPORTS",
        "troubleshooting": "ENABLE_TROUBLESHOOTING",
        "support": "ENABLE_SUPPORT_REQUESTS",
    }
    settings = Settings(
        demo_login=flag("DEMO_LOGIN", "true"),
        enabled=flag("CHAT_ENABLED", "true"),
        db=get("SESSION_DB", "data/sessions.sqlite3"),
        ttl_seconds=number("SESSION_TTL_SECONDS", "3600"),
        agent_mode=get("AGENT_MODE", "demo"),
        foundry_endpoint=get("FOUNDRY_PROJECT_ENDPOINT", ""),
        deployment=get("MODEL_DEPLOYMENT", ""),
        cloud_demo=flag("CLOUD_DEMO", "false"),
        demo_access_code=get("DEMO_ACCESS_CODE", ""),
        telemetry_connection=get("APPLICATIONINSIGHTS_CONNECTION_STRING", ""),
        flows_file=get("FLOWS_FILE", ""),
        scenarios_file=get("SCENARIOS_FILE", ""),
        data_source=get("DATA_SOURCE", "synthetic"),
        synthetic_faults=frozenset(name for name in get("SYNTHETIC_FAULTS", "").replace(",", " ").split()),
        features=frozenset(feature for feature, name in switches.items() if flag(name, "true")),
        app_port=number("APP_PORT", "8001"),
        log_level=get("LOG_LEVEL", "INFO").upper(),
        reset_on_start=flag("RESET_DEMO_DATA_ON_START", "false"),
        app_commit=get("APP_COMMIT", ""),
    )
    if validate:
        settings.validate()
    return settings
