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


PRICE_PARAMETERS = {
    "modelPriceInput": "MODEL_PRICE_INPUT",
    "modelPriceCachedInput": "MODEL_PRICE_CACHED_INPUT",
    "modelPriceOutput": "MODEL_PRICE_OUTPUT",
}


def preview(deploy_module, monkeypatch, tmp_path, *flags: str) -> dict:
    """Preview a deployment with Azure faked, and return the template parameters it sends."""
    sent = []

    def az(*args, capture=True):
        if args[:2] == ("account", "show"):
            return {"id": "synthetic-subscription", "name": "Synthetic subscription"}
        if args[:3] == ("deployment", "sub", "what-if"):
            file = Path(args[args.index("--parameters") + 1].removeprefix("@"))
            sent.append(json.loads(file.read_text())["parameters"])
        return []

    monkeypatch.setattr(deploy_module, "az", az)
    monkeypatch.setattr(deploy_module, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["deploy.py", "--subscription", "synthetic-subscription", "--what-if", *flags])
    deploy_module.main()
    [parameters] = sent
    return {name: parameter["value"] for name, parameter in parameters.items()}


def test_model_prices_reach_the_container_only_when_given(deploy_module, monkeypatch, tmp_path, capsys):
    flags = ("--price-input", "1.25", "--price-cached-input", " 0.125 ", "--price-output", "10")
    priced = preview(deploy_module, monkeypatch, tmp_path, *flags)
    assert "Estimated cost at US dollars per million tokens: input 1.25, cached input 0.125, output 10" in (
        capsys.readouterr().out
    )
    unpriced = preview(deploy_module, monkeypatch, tmp_path)
    assert "Estimated cost: not configured." in capsys.readouterr().out
    assert [priced[name] for name in PRICE_PARAMETERS] == ["1.25", "0.125", "10"]
    assert [unpriced[name] for name in PRICE_PARAMETERS] == ["", "", ""]
    main = (ROOT / "infra" / "main.bicep").read_text()
    assert set(priced) <= set(re.findall(r"^param (\w+) ", main, re.MULTILINE)), "every parameter is declared"
    resources = (ROOT / "infra" / "resources.bicep").read_text()
    for parameter, variable in PRICE_PARAMETERS.items():
        assert f"param {parameter} string = ''" in main and f"    {parameter}: {parameter}\n" in main
        assert f"{{ name: '{variable}', value: {parameter} }}" in resources
    # Each variable is set only when its price was given; the app refuses a partial set at startup.
    assert "price => !empty(price.value)" in resources and "], modelPrices)" in resources


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (
            ("--price-input", "cheap", "--price-cached-input", "0.1", "--price-output", "5"),
            "--price-input must be a non-negative number of US dollars per million tokens.",
        ),
        (
            ("--price-input", "1", "--price-cached-input", "-0.1", "--price-output", "5"),
            "--price-cached-input must be a non-negative number of US dollars per million tokens.",
        ),
        (
            ("--price-input", "1", "--price-cached-input", "0.1", "--price-output", "nan"),
            "--price-output must be a non-negative number of US dollars per million tokens.",
        ),
        (
            ("--price-input", "1", "--price-output", "5"),
            "Pass --price-input, --price-cached-input and --price-output together, or none.",
        ),
    ],
)
def test_model_prices_are_checked_before_anything_reaches_azure(deploy_module, monkeypatch, capsys, flags, message):
    calls = []
    monkeypatch.setattr(deploy_module, "az", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr("sys.argv", ["deploy.py", "--subscription", "synthetic-subscription", *flags])
    with pytest.raises(SystemExit) as stopped:
        deploy_module.main()
    assert stopped.value.code == 2 and message in capsys.readouterr().err
    assert calls == []
