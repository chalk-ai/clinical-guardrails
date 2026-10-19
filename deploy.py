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
    ./deploy.py
"""

from __future__ import annotations

import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_FILES = ["agent.py", "knowledge.py", "server.py", "index.html"]
# Written by eval.py; the page shows the latest recorded evaluation when it exists.
OPTIONAL_FILES = ["eval_results.json"]


def main() -> None:
    # The fast model classifies and answers every turn; the judge model is the output guard.

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
        name='clinical-guardrails',
        cpu="1",
        memory="2Gi",
        env={

            "FAST_MODEL": 'anthropic/claude-haiku-5-5',
            "JUDGE_MODEL": 'anthropic/claude-sonnet-5-5',
            "PORT": "8080",
            "ROUTER_API_KEY": Secret.from_chalk_env('CHALK_ROUTER_API_KEY'),
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
        sg.deploy(build_timeout=1200.0, ready_timeout=900.0)
    finally:
        print(f"deploy returned after {time.monotonic() - t0:.0f}s")
    print("web_url:", sg.web_url)


if __name__ == "__main__":
    main()
