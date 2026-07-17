from __future__ import annotations

import json
import uuid
from contextlib import AbstractContextManager
from typing import Any

import pytest

from vtrade.portfolio import PortfolioPaginationError, PostgresPortfolioHandler

AGENT = uuid.UUID("10000000-0000-0000-0000-000000000001")
OTHER_AGENT = uuid.UUID("10000000-0000-0000-0000-000000000002")
CYCLE = uuid.UUID("20000000-0000-0000-0000-000000000001")
SNAPSHOT = uuid.UUID("30000000-0000-0000-0000-000000000001")


def _item(position_id: uuid.UUID, question: str = "Will it happen?") -> dict[str, object]:
    return {
        "position_id": str(position_id),
        "market_id": str(uuid.uuid5(position_id, "market")),
        "market_question": question,
        "outcome_id": str(uuid.uuid5(position_id, "outcome")),
        "outcome": "YES",
        "shares": "10.000000000000",
        "average_cost": "0.500000000000",
        "cost_basis_micros": 5_000_000,
        "realized_pnl_micros": 0,
        "updated_at": "2026-07-16T12:00:00.000000Z",
    }


class _State:
    def __init__(self) -> None:
        self.snapshot_exists = False
        self.snapshot_rows = [
            (uuid.UUID(int=index), _item(uuid.UUID(int=index))) for index in range(1, 6)
        ]
        self.cursors: dict[str, tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]] = {}


class _Cursor:
    def __init__(self, state: _State) -> None:
        self.state = state
        self.selected: list[tuple[object, ...]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...] = ()) -> object:
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT pg_advisory_xact_lock"):
            self.selected = [(None,)]
        elif normalized.startswith("SELECT ac.agent_id"):
            self.selected = [(AGENT, "2026-07-16T12:00:00Z", 7)]
        elif normalized.startswith("SELECT id FROM portfolio_query_snapshots"):
            self.selected = [(SNAPSHOT,)] if self.state.snapshot_exists else []
        elif normalized.startswith("INSERT INTO portfolio_query_snapshots"):
            self.state.snapshot_exists = True
            self.selected = []
        elif normalized.startswith("INSERT INTO portfolio_snapshot_positions"):
            self.selected = []
        elif normalized.startswith("SELECT position_id, item"):
            after = params[1]
            limit = int(str(params[3]))
            rows = self.state.snapshot_rows
            if after is not None:
                rows = [row for row in rows if row[0] > after]
            self.selected = list(rows[:limit])
        elif normalized.startswith("INSERT INTO portfolio_page_cursors"):
            self.state.cursors[str(params[0])] = (
                uuid.UUID(str(params[1])),
                uuid.UUID(str(params[2])),
                uuid.UUID(str(params[3])),
                uuid.UUID(str(params[4])),
            )
            self.selected = []
        elif normalized.startswith("SELECT snapshot_id, after_position_id"):
            stored = self.state.cursors.get(str(params[0]))
            if stored is None or stored[1:3] != (params[1], params[2]):
                self.selected = []
            else:
                self.selected = [(stored[0], stored[3])]
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return self.selected[0] if self.selected else None

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.selected)


class _Connection:
    def __init__(self, state: _State) -> None:
        self.state = state

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self.state)


def _connect(state: _State) -> Any:
    def connect(_database_url: str) -> AbstractContextManager[Any]:
        return _Connection(state)

    return connect


def test_pages_are_stable_without_overlap_or_gaps() -> None:
    state = _State()
    tokens = iter(("first-opaque-cursor", "second-opaque-cursor"))
    handler = PostgresPortfolioHandler(
        "postgresql://test",
        agent_id=AGENT,
        agent_cycle_id=CYCLE,
        connect=_connect(state),
        cursor_factory=lambda: next(tokens),
    )

    first = handler({"limit": 2})
    first_ids = [item["position_id"] for item in first["items"]]  # type: ignore[index]
    assert first_ids == [str(uuid.UUID(int=1)), str(uuid.UUID(int=2))]
    assert first["has_more"] is True

    # A live-table mutation cannot affect the already materialized snapshot rows.
    second = handler({"cursor": first["next_cursor"], "limit": 2})
    third = handler({"cursor": second["next_cursor"], "limit": 2})
    all_ids = first_ids + [
        item["position_id"] for item in second["items"] + third["items"]  # type: ignore[operator,index]
    ]
    assert all_ids == [str(uuid.UUID(int=index)) for index in range(1, 6)]
    assert len(set(all_ids)) == 5
    assert third == {"items": [state.snapshot_rows[-1][1]], "next_cursor": None, "has_more": False}


def test_foreign_cursor_is_rejected() -> None:
    state = _State()
    owner = PostgresPortfolioHandler(
        "postgresql://test",
        agent_id=AGENT,
        agent_cycle_id=CYCLE,
        connect=_connect(state),
        cursor_factory=lambda: "opaque-owner-cursor",
    )
    first = owner({"limit": 1})
    foreign = PostgresPortfolioHandler(
        "postgresql://test",
        agent_id=OTHER_AGENT,
        agent_cycle_id=CYCLE,
        connect=_connect(state),
    )
    with pytest.raises(PortfolioPaginationError, match="invalid or foreign"):
        foreign({"cursor": first["next_cursor"]})


@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": 201}, {"limit": True}, {"x": 1}])
def test_invalid_arguments_are_rejected(arguments: dict[str, object]) -> None:
    handler = PostgresPortfolioHandler(
        "postgresql://test", agent_id=AGENT, agent_cycle_id=CYCLE, connect=_connect(_State())
    )
    with pytest.raises(PortfolioPaginationError):
        handler(arguments)


def test_page_is_trimmed_to_conservative_24k_bound() -> None:
    state = _State()
    state.snapshot_rows = [
        (uuid.UUID(int=index), _item(uuid.UUID(int=index), "q" * 8_000))
        for index in range(1, 6)
    ]
    handler = PostgresPortfolioHandler(
        "postgresql://test",
        agent_id=AGENT,
        agent_cycle_id=CYCLE,
        connect=_connect(state),
        cursor_factory=lambda: "bounded-cursor",
    )
    page = handler({"limit": 5})
    encoded = json.dumps(page, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode()
    assert len(encoded) <= 24_000
    assert 0 < len(page["items"]) < 5  # type: ignore[arg-type]
    assert page["has_more"] is True
