"""HTTP routes and packaged assets for the private audit dashboard."""

from __future__ import annotations

import uuid
from importlib.resources import files
from typing import Annotated, Protocol

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from vtrade.dashboard.repository import DashboardFilters, DashboardPage, DashboardWindow


class DashboardDataSource(Protocol):
    def overview(self, filters: DashboardFilters | None = None) -> dict[str, object]: ...

    def agents(
        self,
        filters: DashboardFilters | None = None,
        *,
        page: DashboardPage | None = None,
    ) -> list[dict[str, object]]: ...

    def cycles(
        self,
        filters: DashboardFilters | None = None,
        *,
        page: DashboardPage | None = None,
    ) -> list[dict[str, object]]: ...

    def cycle_detail(self, cycle_id: uuid.UUID) -> dict[str, object] | None: ...


def create_dashboard_router(repository: DashboardDataSource) -> APIRouter:
    """Build the dashboard router around an injected, read-only data source."""

    router = APIRouter()

    @router.get("/", include_in_schema=False, response_class=HTMLResponse)
    @router.get("/admin", include_in_schema=False, response_class=HTMLResponse)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(_resource_text("templates/dashboard.html"))

    @router.get("/admin/assets/dashboard.css", include_in_schema=False)
    def dashboard_css() -> Response:
        return Response(_resource_text("static/dashboard.css"), media_type="text/css")

    @router.get("/admin/assets/dashboard.js", include_in_schema=False)
    def dashboard_javascript() -> Response:
        return Response(
            _resource_text("static/dashboard.js"),
            media_type="application/javascript",
        )

    def selected_filters(
        window: DashboardWindow,
        run_id: uuid.UUID | None,
        agent_id: uuid.UUID | None,
    ) -> DashboardFilters:
        return DashboardFilters(window=window, run_id=run_id, agent_id=agent_id)

    @router.get("/admin/dashboard-data/overview")
    def overview(
        window: DashboardWindow = DashboardWindow.LAST_30_DAYS,
        run_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
    ) -> dict[str, object]:
        return repository.overview(selected_filters(window, run_id, agent_id))

    @router.get("/admin/dashboard-data/agents")
    def agents(
        window: DashboardWindow = DashboardWindow.LAST_30_DAYS,
        run_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return repository.agents(
            selected_filters(window, run_id, agent_id),
            page=DashboardPage(limit=limit, offset=offset),
        )

    @router.get("/admin/dashboard-data/cycles")
    def cycles(
        window: DashboardWindow = DashboardWindow.LAST_30_DAYS,
        run_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, object]]:
        return repository.cycles(
            selected_filters(window, run_id, agent_id),
            page=DashboardPage(limit=limit, offset=offset),
        )

    @router.get("/admin/dashboard-data/cycles/{cycle_id}")
    def cycle_detail(cycle_id: uuid.UUID) -> dict[str, object]:
        detail = repository.cycle_detail(cycle_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="cycle not found")
        return detail

    return router


def _resource_text(relative_path: str) -> str:
    return files("vtrade.dashboard").joinpath(relative_path).read_text(encoding="utf-8")
