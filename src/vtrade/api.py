from __future__ import annotations

import base64
import binascii
import html
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from vtrade.admin import AdminRepositoryError, InvalidOperatorAction, Page
from vtrade.admin import PostgresAdminRepository as AdminRepository
from vtrade.artifacts import SupabaseArtifactStore
from vtrade.config import ConfigurationError, load_experiment_config, required_environment


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
    storage: _StorageProbe | None = None,
) -> FastAPI:
    runtime_settings = settings or AdminSettings.from_environment()
    runtime_repository = repository or AdminRepository(runtime_settings.database_url)
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
            "default-src 'none'; style-src 'unsafe-inline'"
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

    @app.get("/", include_in_schema=False)
    @app.get("/admin")
    def dashboard() -> HTMLResponse:
        overview = runtime_repository.overview()
        leaderboard = runtime_repository.view("leaderboard", page=Page(limit=100))
        freshness = runtime_repository.view("freshness")
        alerts = runtime_repository.view("alerts", page=Page(limit=100))
        positions = runtime_repository.view("positions", page=Page(limit=100))
        trades = runtime_repository.view("trades", page=Page(limit=100))
        settlements = runtime_repository.view("settlements", page=Page(limit=100))
        rejections = runtime_repository.view("rejections", page=Page(limit=100))
        cycles = runtime_repository.view("cycles", page=Page(limit=100))
        usage = runtime_repository.view("usage", page=Page(limit=100))
        config_versions = runtime_repository.view("config_versions", page=Page(limit=100))
        body = _render_dashboard(
            overview,
            leaderboard,
            positions,
            trades,
            settlements,
            rejections,
            cycles,
            usage,
            freshness,
            config_versions,
            alerts,
        )
        return HTMLResponse(body)

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


def _render_dashboard(
    overview: dict[str, object],
    leaderboard: list[dict[str, object]],
    positions: list[dict[str, object]],
    trades: list[dict[str, object]],
    settlements: list[dict[str, object]],
    rejections: list[dict[str, object]],
    cycles: list[dict[str, object]],
    usage: list[dict[str, object]],
    freshness: list[dict[str, object]],
    config_versions: list[dict[str, object]],
    alerts: list[dict[str, object]],
) -> str:
    sections = (
        ("Overview", [overview]),
        ("Leaderboard and PnL", leaderboard),
        ("Positions and executable bid valuation", positions),
        ("Trades", trades),
        ("Settlements", settlements),
        ("Rejections", rejections),
        ("Cycles and decision versions", cycles),
        ("Model and search usage and cost", usage),
        ("Data freshness", freshness),
        ("Configuration, prompt, model and code versions", config_versions),
        ("Alerts", alerts),
    )
    rendered_sections = []
    for title, rows in sections:
        payload = html.escape(json.dumps(rows, default=str, indent=2))
        rendered_sections.append(
            f"<section><h2>{html.escape(title)}</h2><pre>{payload}</pre></section>"
        )
    rendered = "".join(rendered_sections)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>V-Trade private admin</title><style>"
        "body{font-family:system-ui;background:#10141b;color:#eef3f8;margin:2rem;max-width:110rem}"
        "section{background:#18202b;padding:1rem;margin:1rem 0;border-radius:.5rem;overflow:auto}"
        "pre{font-size:.8rem;white-space:pre-wrap}h1,h2{color:#8fd3ff}</style></head>"
        f"<body><h1>V-Trade private admin</h1>{rendered}</body></html>"
    )
