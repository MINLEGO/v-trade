from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Protocol, cast

from vtrade.domain.ports import JsonObject

DEFAULT_PAGE_LIMIT = 100
MAXIMUM_PAGE_LIMIT = 200
MAXIMUM_RESULT_TOKENS = 24_000


class PortfolioPaginationError(ValueError):
    pass


class _Cursor(Protocol):
    def execute(self, query: str, params: Sequence[object] = ()) -> object: ...

    def fetchone(self) -> Sequence[object] | None: ...

    def fetchall(self) -> Sequence[Sequence[object]]: ...


class _Connection(Protocol):
    def cursor(self) -> AbstractContextManager[_Cursor]: ...


_Connect = Callable[[str], AbstractContextManager[_Connection]]
_CursorFactory = Callable[[], str]


class PostgresPortfolioHandler:
    """Bound, snapshot-backed implementation of the get_portfolio tool."""

    def __init__(
        self,
        database_url: str,
        *,
        agent_id: uuid.UUID,
        agent_cycle_id: uuid.UUID,
        connect: _Connect | None = None,
        cursor_factory: _CursorFactory | None = None,
        maximum_result_tokens: int = MAXIMUM_RESULT_TOKENS,
    ) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        if maximum_result_tokens <= 0 or maximum_result_tokens > MAXIMUM_RESULT_TOKENS:
            raise ValueError("portfolio result limit must be between 1 and 24000")
        self._database_url = database_url
        self._agent_id = agent_id
        self._agent_cycle_id = agent_cycle_id
        self._connect = connect or _default_connect
        self._cursor_factory = cursor_factory or (lambda: secrets.token_urlsafe(32))
        self._maximum_result_tokens = maximum_result_tokens

    def __call__(self, arguments: JsonObject) -> JsonObject:
        return self.handle(arguments)

    def handle(self, arguments: JsonObject) -> JsonObject:
        cursor_token, limit = _validate_arguments(arguments)
        with self._connect(self._database_url) as connection, connection.cursor() as cursor:
            if cursor_token is None:
                snapshot_id, after_position_id = self._load_or_create_snapshot(cursor)
            else:
                snapshot_id, after_position_id = self._resolve_cursor(cursor, cursor_token)

            rows = self._read_rows(cursor, snapshot_id, after_position_id, limit + 1)
            items, has_more = _bounded_items(
                rows,
                requested_limit=limit,
                maximum_result_tokens=self._maximum_result_tokens,
            )
            next_cursor: str | None = None
            if has_more:
                if not items:
                    raise PortfolioPaginationError(
                        "one portfolio item exceeds the 24000-token result ceiling"
                    )
                next_cursor = self._cursor_factory()
                _validate_cursor_token(next_cursor)
                last_position_id = uuid.UUID(str(items[-1][0]))
                self._store_cursor(
                    cursor,
                    token=next_cursor,
                    snapshot_id=snapshot_id,
                    after_position_id=last_position_id,
                )
            output: JsonObject = {
                "items": [item[1] for item in items],
                "next_cursor": next_cursor,
                "has_more": has_more,
            }
            if _token_upper_bound(output) > self._maximum_result_tokens:
                raise RuntimeError("portfolio page bound invariant violated")
            return output

    def _load_or_create_snapshot(self, cursor: _Cursor) -> tuple[uuid.UUID, None]:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"portfolio-snapshot:{self._agent_cycle_id}",),
        )
        cursor.execute(
            "SELECT ac.agent_id, ac.data_cutoff, a.portfolio_version "
            "FROM agent_cycles ac JOIN agents a ON a.id = ac.agent_id "
            "WHERE ac.id = %s FOR SHARE",
            (self._agent_cycle_id,),
        )
        cycle = cursor.fetchone()
        if cycle is None or uuid.UUID(str(cycle[0])) != self._agent_id:
            raise PortfolioPaginationError("agent cycle is not authorized for this agent")
        cursor.execute(
            "SELECT id FROM portfolio_query_snapshots "
            "WHERE agent_cycle_id = %s AND agent_id = %s",
            (self._agent_cycle_id, self._agent_id),
        )
        existing = cursor.fetchone()
        if existing is not None:
            return uuid.UUID(str(existing[0])), None

        snapshot_id = uuid.uuid4()
        cursor.execute(
            "INSERT INTO portfolio_query_snapshots "
            "(id, agent_cycle_id, agent_id, data_cutoff, portfolio_version) "
            "VALUES (%s, %s, %s, %s, %s)",
            (snapshot_id, self._agent_cycle_id, self._agent_id, cycle[1], cycle[2]),
        )
        cursor.execute(
            "INSERT INTO portfolio_snapshot_positions (snapshot_id, position_id, item) "
            "SELECT %s, p.id, jsonb_build_object(" 
            "'position_id', p.id::text, 'market_id', m.id::text, "
            "'market_question', m.question, 'outcome_id', o.id::text, "
            "'outcome', o.name, 'shares', p.shares::text, "
            "'average_cost', p.average_cost::text, "
            "'cost_basis_micros', p.cost_basis_micros, "
            "'realized_pnl_micros', p.realized_pnl_micros, "
            "'updated_at', to_char(p.updated_at AT TIME ZONE 'UTC', "
            "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')) "
            "FROM positions p JOIN outcomes o ON o.id = p.outcome_id "
            "JOIN markets m ON m.id = o.market_id "
            "WHERE p.agent_id = %s AND p.shares > 0",
            (snapshot_id, self._agent_id),
        )
        return snapshot_id, None

    def _resolve_cursor(self, cursor: _Cursor, token: str) -> tuple[uuid.UUID, uuid.UUID]:
        cursor.execute(
            "SELECT snapshot_id, after_position_id FROM portfolio_page_cursors "
            "WHERE cursor_hash = %s AND agent_id = %s AND agent_cycle_id = %s",
            (_cursor_hash(token), self._agent_id, self._agent_cycle_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise PortfolioPaginationError("invalid or foreign portfolio cursor")
        return uuid.UUID(str(row[0])), uuid.UUID(str(row[1]))

    def _read_rows(
        self,
        cursor: _Cursor,
        snapshot_id: uuid.UUID,
        after_position_id: uuid.UUID | None,
        limit: int,
    ) -> list[tuple[uuid.UUID, JsonObject]]:
        cursor.execute(
            "SELECT position_id, item FROM portfolio_snapshot_positions "
            "WHERE snapshot_id = %s AND (%s::uuid IS NULL OR position_id > %s::uuid) "
            "ORDER BY position_id ASC LIMIT %s",
            (snapshot_id, after_position_id, after_position_id, limit),
        )
        rows: list[tuple[uuid.UUID, JsonObject]] = []
        for row in cursor.fetchall():
            item = row[1]
            if not isinstance(item, Mapping):
                raise RuntimeError("portfolio snapshot item is not a JSON object")
            rows.append((uuid.UUID(str(row[0])), cast(JsonObject, dict(item))))
        return rows

    def _store_cursor(
        self,
        cursor: _Cursor,
        *,
        token: str,
        snapshot_id: uuid.UUID,
        after_position_id: uuid.UUID,
    ) -> None:
        cursor.execute(
            "INSERT INTO portfolio_page_cursors "
            "(cursor_hash, snapshot_id, agent_id, agent_cycle_id, after_position_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                _cursor_hash(token),
                snapshot_id,
                self._agent_id,
                self._agent_cycle_id,
                after_position_id,
            ),
        )


def _validate_arguments(arguments: JsonObject) -> tuple[str | None, int]:
    unknown = set(arguments) - {"cursor", "limit"}
    if unknown:
        raise PortfolioPaginationError(f"unknown get_portfolio arguments: {sorted(unknown)}")
    cursor = arguments.get("cursor")
    if cursor is not None:
        if not isinstance(cursor, str):
            raise PortfolioPaginationError("cursor must be a string")
        _validate_cursor_token(cursor)
    limit = arguments.get("limit", DEFAULT_PAGE_LIMIT)
    valid_limit = isinstance(limit, int) and not isinstance(limit, bool)
    if not valid_limit or not 1 <= limit <= MAXIMUM_PAGE_LIMIT:
        raise PortfolioPaginationError("limit must be an integer between 1 and 200")
    return cursor, limit


def _validate_cursor_token(token: str) -> None:
    if not token or len(token.encode("utf-8")) > 64:
        raise PortfolioPaginationError("cursor must be a non-empty opaque string")


def _bounded_items(
    rows: Sequence[tuple[uuid.UUID, JsonObject]],
    *,
    requested_limit: int,
    maximum_result_tokens: int,
) -> tuple[list[tuple[uuid.UUID, JsonObject]], bool]:
    selected: list[tuple[uuid.UUID, JsonObject]] = []
    for row in rows[:requested_limit]:
        candidate = [*selected, row]
        candidate_has_more = len(candidate) < len(rows)
        probe: JsonObject = {
            "items": [item[1] for item in candidate],
            "next_cursor": "x" * 64 if candidate_has_more else None,
            "has_more": candidate_has_more,
        }
        if _token_upper_bound(probe) > maximum_result_tokens:
            break
        selected = candidate
    has_more = len(selected) < len(rows)
    return selected, has_more


def _token_upper_bound(value: object) -> int:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return max(1, len(raw.encode("utf-8")))


def _cursor_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _default_connect(database_url: str) -> AbstractContextManager[_Connection]:
    import psycopg

    return cast(AbstractContextManager[_Connection], psycopg.connect(database_url))
