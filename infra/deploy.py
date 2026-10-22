"""Login-based Bicep bootstrap. Only the standalone allowlisted export is sent to ACR."""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def az(*args: str, capture: bool = True):
    command = ["az", *args, "--only-show-errors"]
    if capture:
        result = subprocess.run(command + ["-o", "json"], check=True, capture_output=True, text=True)
        return json.loads(result.stdout) if result.stdout.strip() else None
    subprocess.run(command, check=True)


def write_private(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)


def health(url: str) -> None:
    for _ in range(60):
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=10) as response:
                body = json.load(response)
            if body.get("access_code_required") is True and body.get("mode") == "foundry":
                return
            raise RuntimeError("Deployed configuration is not the protected Foundry demo.")
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5)
    raise RuntimeError("App did not become healthy. Inspect Container Apps logs; resources remain for diagnosis.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--name", default="alsense-demo")
    parser.add_argument("--location", default="northeurope")
    parser.add_argument("--ai-location", default="swedencentral")
    parser.add_argument("--model", default="gpt-5-mini")
    parser.add_argument("--model-version", default="2025-08-07")
    parser.add_argument("--model-capacity", type=int, default=10)
    parser.add_argument("--image-tag", help="Reuse an existing image in this stack's registry instead of rebuilding.")
    parser.add_argument("--what-if", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Approve resource creation after previewing with --what-if.")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{2,19}", args.name):
        parser.error("--name must be 3-20 lowercase letters/digits/hyphens, starting with a letter.")
    if args.image_tag and not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}", args.image_tag):
        parser.error("--image-tag is not a valid container image tag.")
    account = az("account", "show", "--subscription", args.subscription)
    subscription = account["id"]
    print(f"Target: {account['name']} ({subscription}); app {args.location}; model {args.ai_location}", flush=True)
    rg = f"rg-{args.name}"
    groups = az("group", "list", "--subscription", subscription, "--query", f"[?name=='{rg}']")
    if groups and groups[0].get("tags", {}).get("application") != "alsense-synthetic-starter":
        raise RuntimeError(f"Refusing to modify unrelated resource group {rg}. Choose a different --name.")

    state = ROOT / ".azure" / args.name
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    code_file = state / "demo-access-code"
    # A preview must not replace the access code used by an existing deployment.
    if code_file.exists():
        code = code_file.read_text().strip()
    else:
        code = secrets.token_urlsafe(32)
        if not args.what_if:
            write_private(code_file, code)
    parameters = {
        "environmentName": args.name,
        "location": args.location,
        "aiLocation": args.ai_location,
        "modelName": args.model,
        "modelVersion": args.model_version,
        "modelDeployment": args.model,
        "modelCapacity": args.model_capacity,
        "demoAccessCode": code,
        "imageTag": "",
    }
    with tempfile.TemporaryDirectory(prefix="alsense-deploy-") as temporary:
        folder = Path(temporary)
        params_file = folder / "parameters.json"

        def save_params():
            params_file.write_text(
                json.dumps(
                    {
                        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                        "contentVersion": "1.0.0.0",
                        "parameters": {name: {"value": value} for name, value in parameters.items()},
                    }
                )
            )
            params_file.chmod(0o600)

        save_params()
        common = [
            "--subscription",
            subscription,
            "--location",
            args.location,
            "--name",
            f"{args.name}-deployment",
            "--template-file",
            str(ROOT / "infra" / "main.bicep"),
            "--parameters",
            f"@{params_file}",
        ]
        if args.what_if:
            # Include the app in the preview. A dry run does not build an image or create resources.
            parameters["imageTag"] = "preview"
            save_params()
            az("deployment", "sub", "what-if", *common, capture=False)
            return
        if not args.yes:
            print("Creates billable resources and scoped managed-identity role assignments.")
            if input("Type deploy to proceed: ").strip() != "deploy":
                raise RuntimeError("Deployment cancelled.")
        providers = [
            "Microsoft.App",
            "Microsoft.ContainerRegistry",
            "Microsoft.CognitiveServices",
            "Microsoft.KeyVault",
            "Microsoft.ManagedIdentity",
            "Microsoft.OperationalInsights",
            "Microsoft.Insights",
        ]
        for namespace in providers:
            provider = az("provider", "show", "--subscription", subscription, "--namespace", namespace)
            if provider["registrationState"] != "Registered":
                az(
                    "provider",
                    "register",
                    "--subscription",
                    subscription,
                    "--namespace",
                    namespace,
                    "--wait",
                    capture=False,
                )
        print("Provisioning registry, model, identity, secrets and monitoring...", flush=True)
        initial = az("deployment", "sub", "create", *common)
        outputs = initial["properties"]["outputs"]
        registry = outputs["registryName"]["value"]
        image_tag = args.image_tag or time.strftime("%Y%m%d%H%M%S", time.gmtime()) + "-" + secrets.token_hex(3)
        # Never build from the enclosing NDA repository or include local credentials/history.
        if args.image_tag:
            az(
                "acr",
                "repository",
                "show",
                "--subscription",
                subscription,
                "--name",
                registry,
                "--image",
                f"alsense-chat:{image_tag}",
            )
            print(f"Reusing verified image tag {image_tag}.", flush=True)
        else:
            archive = folder / "starter.zip"
            subprocess.run([sys.executable, str(ROOT / "export.py"), str(archive)], check=True)
            with ZipFile(archive) as zipped:
                zipped.extractall(folder / "build")
            context = folder / "build" / "alsense-chat-starter"
            print("Building the clean standalone image in Azure Container Registry...", flush=True)
            az(
                "acr",
                "build",
                "--subscription",
                subscription,
                "--registry",
                registry,
                "--image",
                f"alsense-chat:{image_tag}",
                str(context),
                capture=False,
            )
        parameters["imageTag"] = image_tag
        save_params()
        print("Deploying the protected application...", flush=True)
        deployed = az("deployment", "sub", "create", *common)
        outputs = deployed["properties"]["outputs"]
        (state / "outputs.json").write_text(json.dumps(outputs, indent=2))
        url = outputs["appUrl"]["value"]
        print("Waiting for the protected app's health check...", flush=True)
        health(url)
        print(f"Demo URL: {url}\nAccess code is in {code_file} (never printed or sent to the model).")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        # Azure errors name the actionable failure; never echo secure parameter contents.
        if error.stderr:
            print(error.stderr, file=sys.stderr)
        sys.exit(error.returncode)
