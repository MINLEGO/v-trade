from __future__ import annotations

import json
import uuid
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from vtrade.portfolio import PortfolioPaginationError, PostgresContractPortfolioHandler

AGENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _position_row(index: int) -> tuple[object, ...]:
    return (
        uuid.UUID(int=index),
        f"KX-{index}",
        "YES",
        100,
        1_000,
        10,
        0,
        datetime(2026, 1, 1, tzinfo=UTC),
    )


def _handler(
    rows: list[tuple[object, ...]],
    calls: list[object] | None = None,
    agent_id: uuid.UUID = AGENT_ID,
):
    cursor = SimpleNamespace(
        execute=lambda *_args: None,
        fetchall=lambda: list(rows),
    )
    connection = SimpleNamespace(cursor=lambda: nullcontext(cursor))

    def connect(_database_url: str):
        if calls is not None:
            calls.append(None)
        return nullcontext(connection)

    return PostgresContractPortfolioHandler(
        "postgresql://test",
        agent_id=agent_id,
        connect=connect,
    )


def test_get_portfolio_pages_are_bounded_and_resume_without_gaps() -> None:
    handler = _handler([_position_row(index) for index in range(1, 6)])

    first = handler({"limit": 2})
    second = handler({"cursor": first["next_cursor"], "limit": 2})
    third = handler({"cursor": second["next_cursor"], "limit": 2})

    assert [item["position_id"] for item in first["items"]] == [
        str(uuid.UUID(int=1)),
        str(uuid.UUID(int=2)),
    ]
    assert [item["position_id"] for item in second["items"]] == [
        str(uuid.UUID(int=3)),
        str(uuid.UUID(int=4)),
    ]
    assert [item["position_id"] for item in third["items"]] == [str(uuid.UUID(int=5))]
    assert first["has_more"] is True
    assert second["has_more"] is True
    assert third["next_cursor"] is None
    assert third["has_more"] is False


def test_get_portfolio_trims_large_pages_to_the_result_ceiling() -> None:
    rows = [_position_row(index) for index in range(1, 6)]
    rows = [(*row[:1], "x" * 8_000, *row[2:]) for row in rows]
    handler = _handler(rows)

    page = handler({"limit": 5})
    encoded = json.dumps(page, separators=(",", ":"), ensure_ascii=False, sort_keys=True)

    assert len(encoded.encode("utf-8")) <= 24_000
    assert 0 < len(page["items"]) < 5
    assert page["has_more"] is True


def test_get_portfolio_rejects_invalid_arguments_before_connecting() -> None:
    calls: list[object] = []
    handler = _handler([_position_row(1)], calls)

    with pytest.raises(PortfolioPaginationError):
        handler({"limit": 0})

    assert calls == []


def test_get_portfolio_rejects_a_cursor_bound_to_another_agent() -> None:
    rows = [_position_row(index) for index in range(1, 3)]
    owner = _handler(rows)
    foreign = _handler(rows, agent_id=uuid.UUID(int=2))
    first = owner({"limit": 1})

    with pytest.raises(PortfolioPaginationError, match="invalid or foreign"):
        foreign({"cursor": first["next_cursor"]})
