import re
from pathlib import Path

import pytest

from alsense_chat.adapters.synthetic import FAULTS
from alsense_chat.config import ENVIRONMENT, FEATURES, Settings, load_settings

ROOT = Path(__file__).resolve().parents[1]
# Read by libraries or tooling rather than by load_settings, but documented for operators.
DOCUMENTED_ELSEWHERE = {"AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "UV_DEFAULT_INDEX"}


def example_names() -> set[str]:
    text = (ROOT / ".env.example").read_text()
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.MULTILINE))


def test_env_example_documents_every_setting_and_nothing_else():
    names = example_names()
    assert set(ENVIRONMENT) - names == set(), "Add these settings to .env.example"
    assert names - set(ENVIRONMENT) - DOCUMENTED_ELSEWHERE == set(), "Remove or wire these .env.example entries"


def test_env_example_runs_synthetic_mode_without_secrets():
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", (ROOT / ".env.example").read_text(), re.MULTILINE))
    settings = load_settings(values)
    assert (settings.agent_mode, settings.data_source, settings.synthetic_faults) == ("demo", "synthetic", frozenset())
    assert settings.features == FEATURES
    assert not settings.demo_access_code and not settings.foundry_endpoint
    assert values.get("FLOWS_FILE", "") == ""


def test_defaults_need_no_environment():
    settings = load_settings({})
    assert (settings.app_port, settings.log_level, settings.db) == (8001, "INFO", "data/sessions.sqlite3")
    assert not settings.reset_on_start


def test_values_are_parsed():
    settings = load_settings(
        {
            "SYNTHETIC_FAULTS": "api_timeout, report_failed",
            "ENABLE_REPORTS": "false",
            "LOG_LEVEL": "debug",
            "RESET_DEMO_DATA_ON_START": "true",
            "APP_PORT": "9001",
        }
    )
    assert settings.synthetic_faults == {"api_timeout", "report_failed"}
    assert settings.features == {"troubleshooting", "support"}
    assert (settings.log_level, settings.reset_on_start, settings.app_port) == ("DEBUG", True, 9001)


@pytest.mark.parametrize(
    "environ, problem",
    [
        ({"DEMO_LOGIN": "yes"}, "DEMO_LOGIN must be true or false"),
        ({"SESSION_TTL_SECONDS": "an hour"}, "SESSION_TTL_SECONDS must be a whole number"),
        ({"APP_PORT": "70000"}, "APP_PORT must be between"),
        ({"LOG_LEVEL": "LOUD"}, "LOG_LEVEL must be one of"),
        ({"DATA_SOURCE": "mainframe"}, "DATA_SOURCE must be synthetic or alsense"),
        ({"SYNTHETIC_FAULTS": "gremlins"}, "unknown names"),
        ({"SCENARIOS_FILE": "missing.local.yaml"}, "SCENARIOS_FILE must point"),
    ],
)
def test_bad_values_fail_clearly(environ, problem):
    with pytest.raises(ValueError, match=problem):
        load_settings(environ)


def test_azure_mode_never_falls_back_to_the_scripted_assistant():
    with pytest.raises(ValueError, match="Foundry mode requires FOUNDRY_PROJECT_ENDPOINT and MODEL_DEPLOYMENT"):
        load_settings({"AGENT_MODE": "foundry", "FOUNDRY_PROJECT_ENDPOINT": "https://example.invalid"})


def test_every_problem_is_reported_at_once():
    problems = Settings(agent_mode="foundry", ttl_seconds=5, log_level="LOUD").problems()
    assert len(problems) == 3


def test_fault_catalog_is_documented_in_env_example():
    text = (ROOT / ".env.example").read_text()
    assert all(name in text for name in FAULTS)
