from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="V-Trade private admin API", docs_url=None, redoc_url=None)

    @app.get("/health/live")
    async def live() -> dict[str, Any]:
        return {"status": "ok", "checked_at": datetime.now(UTC).isoformat()}

    @app.get("/health/ready")
    async def ready() -> dict[str, Any]:
        # Database and object-storage probes are wired only after resources are supplied.
        return {"status": "not_ready", "reason": "external_resources_not_configured"}

    return app

