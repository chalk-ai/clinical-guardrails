#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["chalkcompute==2.13.5"]
# ///
"""Deploy the Fernhollow Health clinical-guardrails demo as a ScalingGroup.

The assistant reaches Claude through the Chalk model gateway. Requirements on the target
environment:
  - the Chalk router is enabled (`chalk router status`)
  - a secret holds a router API key (`chalk router api-key create`), CHALK_ROUTER_API_KEY by default

Credentials for the deploy itself come from your existing `chalk login` (or the
CHALK_* env vars):
    ./deploy.py [--env-id <environment>] [--name clinical-guardrails]
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_FILES = ["agent.py", "knowledge.py", "server.py", "index.html"]
# Written by eval.py; the page shows the latest recorded evaluation when it exists.
OPTIONAL_FILES = ["eval_results.json"]


def configure_chalk(env_id: str | None) -> None:
    """Resolve deployment routing settings using the SDK's ambient login."""
    from chalkcompute import ConnectClient

    if env_id:
        os.environ["CHALK_ENVIRONMENT"] = env_id
    client = ConnectClient()
    try:
        client._ensure_auth()
        # Only routing settings are needed by the deployed app; credentials stay in the SDK.
        os.environ["CHALK_API_SERVER"] = client._api_server
        os.environ["CHALK_ENVIRONMENT_ID"] = client._env_id
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="clinical-guardrails")
    parser.add_argument("--env-id", default=None)
    # The fast model classifies and answers every turn; the judge model is the output guard.
    parser.add_argument("--fast-model", default="anthropic/claude-haiku-5-5")
    parser.add_argument("--judge-model", default="anthropic/claude-sonnet-5-5")
    parser.add_argument("--key-secret", default="CHALK_ROUTER_API_KEY")
    parser.add_argument("--ready-timeout", type=float, default=900.0)
    args = parser.parse_args()

    configure_chalk(args.env_id)
    from chalkcompute import Image, ReadinessProbe, ScalingGroup, Secret

    image = Image.debian_slim(python_version="3.12").pip_install(
        ["anthropic==1.13.0", "fastapi==0.116.1", "uvicorn[standard]==0.35.0"]
    )
    # The app files total more than the 32 KiB inline `copy` budget, so they ride a volume
    # mounted at /app when the replica starts.
    files = APP_FILES + [name for name in OPTIONAL_FILES if (HERE / name).exists()]
    for name in files:
        image = image.add_local_file(str(HERE / name), f"/app/{name}", strategy="volume")
    image = image.workdir("/app")

    sg = ScalingGroup(
        image=image,
        name=args.name,
        cpu="1",
        memory="2Gi",
        env={
            # Not secrets: where the model gateway lives and which environment's router to use.
            "CHALK_API_SERVER": os.environ["CHALK_API_SERVER"],
            "CHALK_ENVIRONMENT_ID": os.environ["CHALK_ENVIRONMENT_ID"],
            "FAST_MODEL": args.fast_model,
            "JUDGE_MODEL": args.judge_model,
            "PORT": "8080",
            "ROUTER_API_KEY": Secret.from_chalk_env(args.key_secret),
        },
        entrypoint=["python", "/app/server.py"],
        port=8080,
        readiness_probe=ReadinessProbe.http("/api/config"),
        # Conversations live in the replica's memory and every turn must reach that replica.
        min_replicas=1,
        max_replicas=1,
    )
    t0 = time.monotonic()
    try:
        sg.deploy(build_timeout=1200.0, ready_timeout=args.ready_timeout)
    finally:
        print(f"deploy returned after {time.monotonic() - t0:.0f}s")
    print("web_url:", sg.web_url)


if __name__ == "__main__":
    main()
