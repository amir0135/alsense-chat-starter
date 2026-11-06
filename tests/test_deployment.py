import importlib.util
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def deploy_module():
    spec = importlib.util.spec_from_file_location("starter_deploy", ROOT / "infra" / "deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_az_wrapper_does_not_use_shell_or_change_default_subscription(deploy_module, monkeypatch):
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"id": "chosen-sub"}))

    monkeypatch.setattr(deploy_module.subprocess, "run", run)
    assert deploy_module.az("account", "show", "--subscription", "chosen-sub") == {"id": "chosen-sub"}
    assert seen[0][0][:5] == ["az", "account", "show", "--subscription", "chosen-sub"]
    assert "shell" not in seen[0][1]
    assert seen[0][1]["check"] is True


def test_code_file_is_private_and_cannot_be_silently_overwritten(deploy_module, tmp_path):
    path = tmp_path / "demo-code"
    deploy_module.write_private(path, "synthetic-secret")
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        deploy_module.write_private(path, "replacement")
    assert path.read_text() == "synthetic-secret"


def test_the_model_defaults_agree_across_the_templates_scripts_and_docs(deploy_module, monkeypatch):
    seen = {}

    def parse_args(parser, *_):
        seen.update({name: parser.get_default(name) for name in ("model", "model_version", "model_capacity")})
        raise SystemExit(0)

    monkeypatch.setattr(deploy_module.argparse.ArgumentParser, "parse_args", parse_args)
    with pytest.raises(SystemExit):
        deploy_module.main()
    model, version, capacity = seen["model"], seen["model_version"], seen["model_capacity"]
    bicep = (ROOT / "infra" / "main.bicep").read_text()
    params = dict(re.findall(r"^param (model\w+) \w+ = '?([^'\s]+)'?$", bicep, re.MULTILINE))
    assert params == {
        "modelName": model,
        "modelVersion": version,
        "modelDeployment": model,
        "modelCapacity": str(capacity),
    }
    assert f"# MODEL_DEPLOYMENT={model}\n" in (ROOT / ".env.example").read_text()
    guide = (ROOT / "docs" / "azure-demo.md").read_text()
    assert f"**{model}** (version {version})" in guide and f"pinned version {version}, {capacity}K tokens" in guide
    assert f"default capacity of {capacity} ({capacity}K TPM)" in guide


def test_infrastructure_cloud_auth_and_export_boundary():
    template = (ROOT / "infra" / "resources.bicep").read_text()
    assert "{ name: 'CLOUD_DEMO', value: 'true' }" in template
    assert "{ name: 'DEMO_ACCESS_CODE', secretRef: 'demo-access-code' }" in template
    assert "value: demoAccessCode" in template
    assert "publicNetworkAccess: 'Disabled'" in template
    assert "disableLocalAuth: true" in template
    assert "adminUserEnabled: false" in template
    assert "maxReplicas: 1" in template
    assert "allowInsecure: false" in template
    outputs = [line for line in template.splitlines() if line.startswith("output ")]
    assert all("demoAccessCode" not in line for line in outputs)
    assert ".azure" in (ROOT / ".dockerignore").read_text()
    assert ".azure/" in (ROOT / ".gitignore").read_text()
