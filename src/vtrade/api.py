from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from vtrade.admin import AdminRepositoryError, InvalidOperatorAction, Page
from vtrade.admin import PostgresAdminRepository as AdminRepository
from vtrade.artifacts import SupabaseArtifactStore
from vtrade.config import ConfigurationError, load_experiment_config, required_environment
from vtrade.dashboard.repository import DashboardRepositoryError, PostgresDashboardRepository
from vtrade.dashboard.web import DashboardDataSource, create_dashboard_router


class _StorageProbe(Protocol):
    def validate(self) -> None: ...


class _AdminRepository(Protocol):
    def probe(self) -> dict[str, object]: ...

    def overview(self) -> dict[str, object]: ...

    def view(
        self,
        name: str,
        *,
        page: Page | None = None,
        agent_id: uuid.UUID | None = None,
    ) -> list[dict[str, object]]: ...

    def set_global_pause(
        self,
        *,
        paused: bool,
        actor_id: str,
        idempotency_key: str,
        occurred_at: datetime | None = None,
    ) -> dict[str, object]: ...

    def set_agent_pause(
        self,
        agent_id: uuid.UUID,
        *,
        paused: bool,
        actor_id: str,
        idempotency_key: str,
        occurred_at: datetime | None = None,
    ) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class AdminSettings:
    database_url: str
    admin_secret: str
    experiment_config: Path

    def __post_init__(self) -> None:
        if not self.database_url:
            raise ConfigurationError("VTRADE_DATABASE_URL is REQUIRED")
        if len(self.admin_secret.encode()) < 32:
            raise ConfigurationError("VTRADE_ADMIN_AUTH_SECRET must contain at least 32 bytes")

    @classmethod
    def from_environment(cls) -> AdminSettings:
        values = required_environment(("VTRADE_DATABASE_URL", "VTRADE_ADMIN_AUTH_SECRET"))
        return cls(
            database_url=values["VTRADE_DATABASE_URL"],
            admin_secret=values["VTRADE_ADMIN_AUTH_SECRET"],
            experiment_config=Path(
                os.getenv(
                    "VTRADE_EXPERIMENT_CONFIG",
                    "config/experiments/predictionarena-polymarket-v1.json",
                )
            ),
        )


def _control_headers(
    x_operator_id: Annotated[str, Header(min_length=1, max_length=128)],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ],
) -> tuple[str, str]:
    return x_operator_id, idempotency_key


ControlHeaders = Annotated[tuple[str, str], Depends(_control_headers)]


def create_app(
    *,
    settings: AdminSettings | None = None,
    repository: _AdminRepository | None = None,
    dashboard_repository: DashboardDataSource | None = None,
    storage: _StorageProbe | None = None,
) -> FastAPI:
    runtime_settings = settings or AdminSettings.from_environment()
    runtime_repository = repository or AdminRepository(runtime_settings.database_url)
    runtime_dashboard_repository = dashboard_repository or PostgresDashboardRepository(
        runtime_settings.database_url
    )
    runtime_storage = storage or SupabaseArtifactStore.from_environment()

    def authenticate(authorization: Annotated[str | None, Header()] = None) -> None:
        candidate: str | None = None
        if authorization is not None and authorization.startswith("Bearer "):
            candidate = authorization[len("Bearer ") :]
        elif authorization is not None and authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(
                    authorization[len("Basic ") :], validate=True
                ).decode("utf-8")
                _username, candidate = decoded.split(":", 1)
            except (binascii.Error, UnicodeDecodeError, ValueError):
                candidate = None
        if candidate is None or not secrets.compare_digest(
            candidate, runtime_settings.admin_secret
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="unauthorized",
                headers={"WWW-Authenticate": 'Basic realm="V-Trade private admin", Bearer'},
            )

    app = FastAPI(
        title="V-Trade private admin API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(authenticate)],
    )

    @app.middleware("http")
    async def private_response_headers(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; font-src 'self'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(AdminRepositoryError)
    async def repository_failure(_request: Request, _exc: AdminRepositoryError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "admin data source unavailable"},
        )

    @app.exception_handler(InvalidOperatorAction)
    async def invalid_action(_request: Request, exc: InvalidOperatorAction) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(DashboardRepositoryError)
    async def dashboard_repository_failure(
        _request: Request, _exc: DashboardRepositoryError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "dashboard data source unavailable"},
        )

    @app.exception_handler(KeyError)
    async def missing_target(_request: Request, _exc: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "target not found"})

    @app.get("/health/live")
    def live() -> dict[str, object]:
        return {"status": "ok", "checked_at": datetime.now(UTC)}

    @app.get("/health/ready")
    def ready() -> JSONResponse:
        checks: dict[str, object] = {}
        failures: list[str] = []
        try:
            checks["database"] = runtime_repository.probe()
        except Exception:
            checks["database"] = {"status": "failed"}
            failures.append("database")
        try:
            runtime_storage.validate()
            checks["supabase_storage"] = {"status": "ok"}
        except Exception:
            checks["supabase_storage"] = {"status": "failed"}
            failures.append("supabase_storage")
        try:
            config = load_experiment_config(runtime_settings.experiment_config)
            config.assert_runnable()
            checks["configuration"] = {
                "status": "ok",
                "experiment_version": config.version,
                "sha256": config.sha256,
            }
        except ConfigurationError as exc:
            checks["configuration"] = {"status": "failed", "reason": str(exc)}
            failures.append("configuration")
        body = {
            "status": "ready" if not failures else "not_ready",
            "checked_at": datetime.now(UTC).isoformat(),
            "checks": checks,
        }
        return JSONResponse(
            status_code=200 if not failures else status.HTTP_503_SERVICE_UNAVAILABLE,
            content=json.loads(json.dumps(body, default=str)),
        )

    app.include_router(create_dashboard_router(runtime_dashboard_repository))

    @app.get("/admin/overview")
    def overview() -> dict[str, object]:
        return runtime_repository.overview()

    @app.get("/admin/leaderboard")
    def leaderboard(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return runtime_repository.view("leaderboard", page=Page(limit, offset))

    def filtered_view(
        name: str, agent_id: uuid.UUID | None, limit: int, offset: int
    ) -> list[dict[str, object]]:
        return runtime_repository.view(
            name, agent_id=agent_id, page=Page(limit=limit, offset=offset)
        )

    @app.get("/admin/positions")
    def positions(
        agent_id: uuid.UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return filtered_view("positions", agent_id, limit, offset)

    @app.get("/admin/trades")
    def trades(
        agent_id: uuid.UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return filtered_view("trades", agent_id, limit, offset)

    @app.get("/admin/settlements")
    def settlements(
        agent_id: uuid.UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return filtered_view("settlements", agent_id, limit, offset)

    @app.get("/admin/rejections")
    def rejections(
        agent_id: uuid.UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return filtered_view("rejections", agent_id, limit, offset)

    @app.get("/admin/cycles")
    def cycles(
        agent_id: uuid.UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return filtered_view("cycles", agent_id, limit, offset)

    @app.get("/admin/usage")
    def usage(
        agent_id: uuid.UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return filtered_view("usage", agent_id, limit, offset)

    @app.get("/admin/freshness")
    def freshness() -> list[dict[str, object]]:
        return runtime_repository.view("freshness")

    @app.get("/admin/config-versions")
    def config_versions(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return runtime_repository.view("config_versions", page=Page(limit, offset))

    @app.get("/admin/alerts")
    def alerts(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return runtime_repository.view("alerts", page=Page(limit, offset))

    @app.get("/admin/operator-actions")
    def operator_actions(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return runtime_repository.view("operator_actions", page=Page(limit, offset))

    @app.post("/admin/control/pause")
    def pause_all(headers: ControlHeaders) -> dict[str, object]:
        actor, key = headers
        return runtime_repository.set_global_pause(
            paused=True, actor_id=actor, idempotency_key=key
        )

    @app.post("/admin/control/resume")
    def resume_all(headers: ControlHeaders) -> dict[str, object]:
        actor, key = headers
        return runtime_repository.set_global_pause(
            paused=False, actor_id=actor, idempotency_key=key
        )

    @app.post("/admin/agents/{agent_id}/pause")
    def pause_agent(agent_id: uuid.UUID, headers: ControlHeaders) -> dict[str, object]:
        actor, key = headers
        return runtime_repository.set_agent_pause(
            agent_id, paused=True, actor_id=actor, idempotency_key=key
        )

    @app.post("/admin/agents/{agent_id}/resume")
    def resume_agent(agent_id: uuid.UUID, headers: ControlHeaders) -> dict[str, object]:
        actor, key = headers
        return runtime_repository.set_agent_pause(
            agent_id, paused=False, actor_id=actor, idempotency_key=key
        )

    return app
