import json
from pathlib import Path

from helpers import app_client, flow, login

ROOT = Path(__file__).resolve().parents[1]
STATES = {"synthetic", "configured", "unavailable", "future", "disabled", "attention"}


def test_system_reports_every_capability_without_secrets(tmp_path):
    secrets = {
        "telemetry_connection": "InstrumentationKey=00000000-0000-0000-0000-000000000000;IngestionEndpoint=https://secret-ingest.example/",
        "foundry_endpoint": "https://secret-endpoint.example/api/projects/p",
        "demo_access_code": "a-synthetic-access-code-that-is-32-chars-long",
    }
    # Without `with`, the lifespan (which would start real Azure Monitor exporters) does not run.
    status = app_client(tmp_path, cloud_demo=True, **secrets).get("/api/system").json()
    text = json.dumps(status)
    assert not any(value in text for value in secrets.values())
    labels = {item["label"]: item for item in status["items"]}
    assert {item["state"] for item in status["items"]} <= STATES
    for name in (
        "Assistant",
        "Data source",
        "Report service",
        "Health-check service",
        "Support submission",
        "Azure AI Search",
        "Authentication",
        "Session database",
        "Troubleshooting flows",
    ):
        assert name in labels
    assert labels["Data source"]["value"] == "Synthetic APIs"
    assert labels["Azure AI Search"]["state_label"] == "Future integration"
    assert labels["Authentication"]["value"] == "Demo identities (Alice, Bob, Carol) behind an access code"
    assert status["notice"].startswith("Hosted reference demo. Uses synthetic data")


def test_system_shows_demo_ready_and_flow_versions(tmp_path):
    with app_client(tmp_path) as client:
        status = client.get("/api/system").json()
    assert status["overall"]["label"] == "Demo ready"
    assert status["notice"] == "Local reference demo. Uses synthetic data and does not connect to Alsense."
    assert status["flows"] == [
        {
            "title": "Site seems disconnected",
            "version": "1.0.0",
            "status": "approved",
            "owner": "Synthetic operations team",
            "offered": True,
        }
    ]
    assert status["version"] and status["commit"]
    labels = {item["label"]: item for item in status["items"]}
    assert labels["Session database"]["value"] == "Connected · SQLite schema v2"
    assert labels["Assistant"]["value"] == "Scripted demo assistant (no AI model)"


def test_system_flags_global_faults_and_disabled_features(tmp_path):
    with app_client(tmp_path, "api_timeout", features=frozenset({"troubleshooting"})) as client:
        status = client.get("/api/system").json()
    labels = {item["label"]: item for item in status["items"]}
    assert status["overall"]["label"] == "Demo ready · faults on"
    assert labels["Synthetic faults"]["value"] == "api_timeout"
    assert labels["Report service"]["state"] == labels["Support submission"]["state"] == "disabled"


def test_system_page_and_permanent_synthetic_banner(tmp_path):
    with app_client(tmp_path) as client:
        page = client.get("/system")
        assert page.status_code == 200 and "System status" in page.text
        index = client.get("/").text
    for html in (page.text, index):
        assert "Uses synthetic data and does not connect to Alsense." in html
    assert 'href="/system"' in index


def test_correlation_ids_are_returned_and_shown_in_why(tmp_path):
    with app_client(tmp_path) as client:
        headers = login(client)
        assert len(client.get("/api/health").headers["x-correlation-id"]) == 16
        assert (
            client.get("/api/health", headers={"X-Correlation-ID": "bad id!"}).headers["x-correlation-id"] != "bad id!"
        )
        response = client.post(
            "/api/chat", headers={**headers, "X-Correlation-ID": "trace-1234"}, json={"message": "/sites *"}
        )
        assert response.headers["x-correlation-id"] == "trace-1234"
        assert {"label": "Reference", "value": "trace-1234"} in json.loads(response.text.split("data: ")[-1])["why"]


def test_why_this_result_is_user_safe_evidence(tmp_path):
    with app_client(tmp_path) as client:
        result = flow(client, login(client), ["no", "demo nrth", "yes"])
    why = {row["label"]: row["value"] for row in result["why"]}
    assert why["Assistant"] == "Guided check · decision tree, no AI model"
    assert why["Flow"] == "Site seems disconnected · v1.0.0"
    assert why["Site resolved"] == "Demo North"
    assert why["Health result"] == "Primary Gateway Offline"
    assert why["Outcome"] == "Guidance for you · connectivity guidance"
    assert why["Data"] == "Synthetic demo data"
    text = json.dumps(result["why"])
    assert not any(value in text for value in ("site-a", "customer-a", "tool-", "check_site_health", "resolve_sites"))
