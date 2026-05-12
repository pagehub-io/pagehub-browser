"""Modal deployment for pagehub-browser.

Single-container pinning (min=max=1) so the in-process session registry is always
consistent; @modal.concurrent makes "different sessions run in parallel" real (without it
the one container serializes inputs and the SPEC's parallelism success-criterion is false).
The custom image bakes Playwright + Chromium so cold starts don't `playwright install`.
"""

from __future__ import annotations

import subprocess

import modal


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"]).decode().strip()
    except Exception:  # noqa: BLE001 - deploy must not hard-fail over this
        return "unknown"


_GIT_COMMIT = _git_commit()

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "fastapi==0.136.1",
        "uvicorn[standard]==0.34.0",
        "pydantic==2.10.4",
        "pydantic-settings==2.7.0",
        "playwright==1.58.0",
    )
    .run_commands("playwright install --with-deps chromium")
    .env({"GIT_COMMIT": _GIT_COMMIT, "ENV": "production", "HEADLESS": "true"})
    .add_local_dir("api", remote_path="/root/api")
)

app = modal.App("pagehub-browser", image=image)

MODAL_FUNCTION_TIMEOUT = 120
CONCURRENT_INPUTS = 12


@app.function(
    min_containers=1,
    max_containers=1,
    scaledown_window=900,  # >= IDLE_TIMEOUT_MAX_SECONDS so a brief gap between eval steps keeps the container
    timeout=MODAL_FUNCTION_TIMEOUT,
)
@modal.concurrent(max_inputs=CONCURRENT_INPUTS)
@modal.asgi_app()
def fastapi_app():
    from api.main import create_app

    return create_app()
