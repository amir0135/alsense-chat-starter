import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    demo_login: bool = True
    enabled: bool = True
    db: str = "sessions.sqlite3"
    ttl_seconds: int = 3600
    agent_mode: str = "demo"
    foundry_endpoint: str = ""
    deployment: str = ""
    cloud_demo: bool = False
    demo_access_code: str = ""
    telemetry_connection: str = ""
    flows_file: str = ""

    def validate(self) -> None:
        if not self.demo_login:
            raise ValueError("Real sign-in is not wired. Supply a trusted authentication provider before deployment.")
        if self.agent_mode not in {"demo", "foundry"}:
            raise ValueError("AGENT_MODE must be demo or foundry.")
        if self.flows_file and not Path(self.flows_file).is_file():
            raise ValueError("FLOWS_FILE must point to an existing troubleshooting flows YAML file.")
        if self.ttl_seconds < 60:
            raise ValueError("SESSION_TTL_SECONDS must be at least 60.")
        if self.agent_mode == "foundry" and not (self.foundry_endpoint and self.deployment):
            raise ValueError("Foundry mode requires FOUNDRY_PROJECT_ENDPOINT and MODEL_DEPLOYMENT.")
        if self.cloud_demo and len(self.demo_access_code) < 32:
            raise ValueError("Cloud demo requires a high-entropy DEMO_ACCESS_CODE of at least 32 characters.")


def load_settings() -> Settings:
    def flag(name: str, default: str) -> bool:
        value = os.environ.get(name, default).lower()
        if value not in {"true", "false"}:
            raise ValueError(f"{name} must be true or false.")
        return value == "true"

    settings = Settings(
        demo_login=flag("DEMO_LOGIN", "true"),
        enabled=flag("CHAT_ENABLED", "true"),
        db=os.environ.get("SESSION_DB", "sessions.sqlite3"),
        ttl_seconds=int(os.environ.get("SESSION_TTL_SECONDS", "3600")),
        agent_mode=os.environ.get("AGENT_MODE", "demo"),
        foundry_endpoint=os.environ.get("FOUNDRY_PROJECT_ENDPOINT", ""),
        deployment=os.environ.get("MODEL_DEPLOYMENT", ""),
        cloud_demo=flag("CLOUD_DEMO", "false"),
        demo_access_code=os.environ.get("DEMO_ACCESS_CODE", ""),
        telemetry_connection=os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", ""),
        flows_file=os.environ.get("FLOWS_FILE", ""),
    )
    settings.validate()
    return settings
