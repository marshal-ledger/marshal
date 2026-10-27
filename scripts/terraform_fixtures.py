"""Generate the Terraform plan fixtures in tests/fixtures/terraform/ from public examples.

Each fixture is ``terraform show -json`` of a saved plan of a public Terraform example,
pinned to a commit, with pinned Terraform and provider versions. A plan makes a few
read-only AWS lookups (the account id, an IAM managed policy, a Bedrock model's ARN), so
the script answers them locally: LocalStack for STS and IAM, and a small stub for
Bedrock's GetFoundationModel, which returns the ARN AWS publishes for the model id. No
AWS account or credentials are involved. The examples' own files are not changed; a
``marshal_override.tf`` sets the provider versions, fake credentials and local endpoints.

    python scripts/terraform_fixtures.py            # needs Docker
    python scripts/terraform_fixtures.py --only acwwat-basic

Unlike the other golden fixtures, these are committed: they can't be fetched, only
generated. tests/fixtures/terraform/README.md credits the sources and their licenses.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "fixtures" / "terraform"
TERRAFORM = "hashicorp/terraform:1.13.4"
LOCALSTACK = "localstack/localstack:4.9.2"
PYTHON = "python:3.12.7-alpine"
NETWORK = "marshal-tf-fixtures"
PROVIDERS = {
    "aws": "hashicorp/aws", "awscc": "hashicorp/awscc", "random": "hashicorp/random", "time": "hashicorp/time",
    "opensearch": "opensearch-project/opensearch", "archive": "hashicorp/archive", "null": "hashicorp/null",
}
SOURCES = [
    {
        "repo": "aws-ia/terraform-aws-bedrock",
        "commit": "4b74b7dd5110292cb34ca911448581153484f13f",
        "license": "Apache-2.0",
        "providers": {"aws": "6.70.0", "awscc": "1.107.0", "random": "3.9.1", "time": "0.14.2", "opensearch": "2.2.0"},
        "examples": {
            "examples/agent-only": "aws-ia-agent-only",
            "examples/agent-collaborator": "aws-ia-agent-collaborator",
            "examples/agent-with-guardrails": "aws-ia-agent-with-guardrails",
            "examples/agent-with-knowledge-base": "aws-ia-agent-with-knowledge-base",
        },
    },
    {
        "repo": "acwwat/terraform-amazon-bedrock-agent-example",
        "commit": "04197c18bcb28c70d068b5aabea5108c0f9b809d",
        "license": "Apache-2.0",
        "providers": {"aws": "5.100.0", "archive": "2.8.1", "null": "3.3.2"},
        "examples": {"1_basic": "acwwat-basic", "2_knowledge_base": "acwwat-knowledge-base"},
    },
]

BEDROCK_STUB = '''
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith("/foundation-models/"):
            self.send_error(404)
            return
        model = unquote(self.path.split("/foundation-models/", 1)[1].split("?", 1)[0])
        body = json.dumps({"modelDetails": {
            "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/" + model, "modelId": model, "modelName": model,
            "providerName": model.split(".", 1)[0].capitalize(), "inputModalities": ["TEXT"], "outputModalities": ["TEXT"],
            "responseStreamingSupported": True, "customizationsSupported": [], "inferenceTypesSupported": ["ON_DEMAND"],
            "modelLifecycle": {"status": "ACTIVE"}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
'''


def override(providers: dict[str, str]) -> str:
    required = "\n".join(f'    {name} = {{ source = "{PROVIDERS[name]}", version = "{version}" }}'
                         for name, version in providers.items())
    text = f"""# Added by scripts/terraform_fixtures.py: pinned providers, fake credentials, local endpoints.
terraform {{
  required_providers {{
{required}
  }}
}}

provider "aws" {{
  region                      = "us-east-1"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  endpoints {{
    sts     = "http://localstack:4566"
    iam     = "http://localstack:4566"
    bedrock = "http://bedrock-stub:8080"
  }}
}}
"""
    if "awscc" in providers:
        text += """
provider "awscc" {
  region                  = "us-east-1"
  access_key              = "test"
  secret_key              = "test"
  skip_metadata_api_check = true
  endpoints = {
    sts             = "http://localstack:4566"
    cloudcontrolapi = "http://localstack:4566"
  }
}
"""
    return text


def run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs)


def terraform(checkout: Path, example: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "run", "--rm", "--network", NETWORK, "-v", f"{checkout}:/w", "-w", f"/w/{example}",
         "-e", "TF_IN_AUTOMATION=1", TERRAFORM, *args],
        text=True, capture_output=True,
    )


def checkout(repo: str, commit: str, into: Path) -> None:
    run("git", "init", "-q", str(into))
    run("git", "-C", str(into), "fetch", "-q", "--depth", "1", f"https://github.com/{repo}.git", commit)
    run("git", "-C", str(into), "-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD")


def start_services(stub_dir: Path) -> None:
    subprocess.run(["docker", "network", "create", NETWORK], capture_output=True)
    (stub_dir / "stub.py").write_text(BEDROCK_STUB)
    run("docker", "run", "-d", "--rm", "--name", "marshal-tf-localstack", "--network", NETWORK,
        "--network-alias", "localstack", "-e", "SERVICES=sts,iam", LOCALSTACK)
    run("docker", "run", "-d", "--rm", "--name", "marshal-tf-bedrock-stub", "--network", NETWORK,
        "--network-alias", "bedrock-stub", "-v", f"{stub_dir}:/stub", PYTHON, "python", "/stub/stub.py")
    for _ in range(60):
        logs = subprocess.run(["docker", "logs", "marshal-tf-localstack"], capture_output=True, text=True)
        if "Ready." in logs.stdout + logs.stderr:
            return
        time.sleep(1)
    raise SystemExit("LocalStack didn't start")


def stop_services() -> None:
    for name in ("marshal-tf-localstack", "marshal-tf-bedrock-stub"):
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "network", "rm", NETWORK], capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", action="append", help="fixture name to generate; repeatable")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    failed = []
    with tempfile.TemporaryDirectory() as work:
        work_dir = Path(work)
        stub_dir = work_dir / "stub"
        stub_dir.mkdir()
        start_services(stub_dir)
        try:
            for source in SOURCES:
                wanted = {e: n for e, n in source["examples"].items() if not args.only or n in args.only}
                if not wanted:
                    continue
                repo_dir = work_dir / source["repo"].replace("/", "_")
                checkout(source["repo"], source["commit"], repo_dir)
                for example, name in wanted.items():
                    (repo_dir / example / "marshal_override.tf").write_text(override(source["providers"]))
                    for step in (["init", "-input=false", "-no-color"],
                                 ["plan", "-input=false", "-no-color", "-refresh=false", "-out=tfplan"]):
                        result = terraform(repo_dir, example, *step)
                        if result.returncode:
                            failed.append(name)
                            print(f"{name}: terraform {step[0]} failed:\n{(result.stdout + result.stderr)[-1500:]}", file=sys.stderr)
                            break
                    else:
                        shown = terraform(repo_dir, example, "show", "-json", "tfplan")
                        plan = json.loads(shown.stdout)
                        plan.pop("timestamp", None)  # the only value that changes between runs
                        (OUT / f"{name}.plan.json").write_text(json.dumps(plan, separators=(",", ":")) + "\n")
                        print(f"{name}: {len(plan.get('resource_changes') or [])} resources")
                    shutil.rmtree(repo_dir / example / ".terraform", ignore_errors=True)
        finally:
            stop_services()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
