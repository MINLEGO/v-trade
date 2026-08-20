#!/usr/bin/env python3
"""Capture real Kalshi books and analyze conservative liquidity haircuts.

This is an issue-5 research prototype. It deliberately lives below the
production package: it captures a bounded public sample, keeps the raw
order-book responses, and produces review artifacts without changing broker
behavior or experiment configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
CAPTURE_VERSION = "kalshi-liquidity-haircut-capture-v1"
ANALYSIS_VERSION = "kalshi-liquidity-haircut-analysis-v1"
USER_AGENT = "V-Trade issue-5 liquidity analysis/1.0"
DEFAULT_CAPTURE = Path("docs/kalshi-liquidity-haircut-capture.json")
DEFAULT_ANALYSIS = Path("docs/kalshi-liquidity-haircut-analysis.json")
DEFAULT_REPORT = Path("docs/kalshi-liquidity-haircut-analysis.md")
TRANSIENT_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
PRICE_BANDS = (
    ("0.00-0.20", Decimal("0.00"), Decimal("0.20")),
    ("0.20-0.40", Decimal("0.20"), Decimal("0.40")),
    ("0.40-0.60", Decimal("0.40"), Decimal("0.60")),
    ("0.60-0.80", Decimal("0.60"), Decimal("0.80")),
    ("0.80-1.00", Decimal("0.80"), Decimal("1.0000001")),
)

JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class HaircutCandidate:
    """One versioned candidate rule evaluated against a selected raw ladder."""

    name: str
    observed_levels: int
    effective_levels: int
    ignored_best_levels: int
    maximum_ignored_fraction: Decimal
    description: str


CANDIDATES: tuple[HaircutCandidate, ...] = (
    HaircutCandidate(
        "no-haircut-5x5",
        observed_levels=5,
        effective_levels=5,
        ignored_best_levels=0,
        maximum_ignored_fraction=Decimal("0"),
        description="Five observed and five executable levels with no haircut.",
    ),
    HaircutCandidate(
        "best-level-25pct-6x6",
        observed_levels=6,
        effective_levels=6,
        ignored_best_levels=1,
        maximum_ignored_fraction=Decimal("0.25"),
        description=(
            "Six observed and executable levels; cap the best-level haircut at "
            "25% of captured depth."
        ),
    ),
    HaircutCandidate(
        "current-six-observed-five-effective",
        observed_levels=6,
        effective_levels=5,
        ignored_best_levels=1,
        maximum_ignored_fraction=Decimal("0.50"),
        description=(
            "Current V-Trade rule: six observed, five executable, one best "
            "level capped at 50%."
        ),
    ),
    HaircutCandidate(
        "best-level-50pct-6x6",
        observed_levels=6,
        effective_levels=6,
        ignored_best_levels=1,
        maximum_ignored_fraction=Decimal("0.50"),
        description=(
            "Six observed and executable levels; retain the current 50% "
            "best-level cap."
        ),
    ),
    HaircutCandidate(
        "top-two-50pct-7x7",
        observed_levels=7,
        effective_levels=7,
        ignored_best_levels=2,
        maximum_ignored_fraction=Decimal("0.50"),
        description=(
            "Seven observed and executable levels; spread one aggregate 50% "
            "cap across the two best levels."
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    contracts: Decimal


@dataclass(frozen=True, slots=True)
class CandidateMetrics:
    captured_levels: int
    executable_levels: int
    raw_depth: Decimal
    ignored_depth: Decimal
    effective_depth: Decimal
    retained_fraction: Decimal | None
    ignored_fraction: Decimal | None
    best_level_fully_removed: bool
    tail_depth_excluded: Decimal


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_decimal(value: object, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} is not a decimal: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{field} is not finite")
    return result


def optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _request_json(
    url: str,
    *,
    timeout_seconds: float,
    attempts: int = 3,
    deadline: float | None = None,
) -> JsonObject:
    """Fetch one public JSON response with the resolved bounded retry shape."""

    last_error: Exception | None = None
    for attempt in range(attempts):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("analysis capture deadline expired")
        request_timeout = timeout_seconds
        if deadline is not None:
            request_timeout = min(request_timeout, max(0.1, deadline - time.monotonic()))
        request = Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        )
        try:
            with urlopen(request, timeout=request_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return dict(_mapping(payload, field=f"JSON response from {url}"))
        except HTTPError as exc:
            last_error = exc
            if exc.code not in TRANSIENT_STATUS_CODES or attempt == attempts - 1:
                raise RuntimeError(
                    f"Kalshi request failed with HTTP {exc.code}: {url}"
                ) from exc
        except (OSError, TimeoutError, URLError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                raise RuntimeError(f"Kalshi request failed: {url}: {exc}") from exc
        time.sleep(min(2.0, 0.25 * (2**attempt)))
    raise RuntimeError(f"Kalshi request failed: {url}: {last_error}")


def _page_url(base_url: str, path: str, parameters: Mapping[str, str]) -> str:
    query = urlencode(parameters)
    return (
        f"{base_url.rstrip('/')}{path}?{query}"
        if query
        else f"{base_url.rstrip('/')}{path}"
    )


def _fetch_pages(
    base_url: str,
    *,
    path: str,
    collection_key: str,
    identity_key: str,
    parameters: Mapping[str, str],
    page_limit: int,
    timeout_seconds: float,
    deadline: float,
) -> tuple[list[JsonObject], JsonObject]:
    values: list[JsonObject] = []
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    cursor = ""
    pages = 0
    request_parameters = dict(parameters)
    request_parameters["limit"] = str(page_limit)

    while True:
        if cursor:
            if cursor in seen_cursors:
                raise ValueError(f"repeated Kalshi cursor: {cursor}")
            seen_cursors.add(cursor)
            request_parameters["cursor"] = cursor
        payload = _request_json(
            _page_url(base_url, path, request_parameters),
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
        page = payload.get(collection_key)
        if not isinstance(page, list):
            raise ValueError(f"Kalshi response field {collection_key!r} is not a list")
        if not page and payload.get("cursor"):
            raise ValueError(f"Kalshi returned an empty page with a cursor at {path}")
        pages += 1
        for value in page:
            item = _mapping(value, field=collection_key)
            identity = item.get(identity_key)
            if not isinstance(identity, str) or not identity:
                raise ValueError(f"Kalshi {collection_key} item has no {identity_key}")
            if identity in seen_ids:
                raise ValueError(f"duplicate {identity_key} across Kalshi pages: {identity}")
            seen_ids.add(identity)
            values.append(dict(item))
        next_cursor = payload.get("cursor") or ""
        if not isinstance(next_cursor, str):
            raise ValueError("Kalshi cursor is not a string")
        if not next_cursor:
            break
        cursor = next_cursor

    return values, {
        "pages": pages,
        "items": len(values),
        "sha256": canonical_sha256(values),
    }


def _market_quote(value: object) -> Decimal | None:
    parsed = optional_decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _market_midpoint(market: Mapping[str, Any]) -> Decimal | None:
    yes_bid = _market_quote(market.get("yes_bid_dollars"))
    no_bid = _market_quote(market.get("no_bid_dollars"))
    yes_ask = _market_quote(market.get("yes_ask_dollars"))
    if yes_bid is not None and no_bid is not None:
        return (yes_bid + Decimal(1) - no_bid) / Decimal(2)
    if yes_bid is not None:
        return yes_bid
    if yes_ask is not None:
        return yes_ask
    if no_bid is not None:
        return Decimal(1) - no_bid
    return None


def price_band(market: Mapping[str, Any]) -> str:
    midpoint = _market_midpoint(market)
    if midpoint is None:
        return "empty/unknown"
    for name, lower, upper in PRICE_BANDS:
        if lower <= midpoint < upper:
            return name
    return "empty/unknown"


def quote_state(market: Mapping[str, Any]) -> str:
    yes = _market_quote(market.get("yes_bid_dollars"))
    no = _market_quote(market.get("no_bid_dollars"))
    if yes is not None and no is not None:
        return "yes-and-no"
    if yes is not None:
        return "yes-only"
    if no is not None:
        return "no-only"
    return "empty"


def _market_sort_key(market: Mapping[str, Any]) -> tuple[Decimal, Decimal, str]:
    liquidity = optional_decimal(market.get("liquidity_dollars")) or Decimal(0)
    volume = optional_decimal(market.get("volume_fp")) or Decimal(0)
    return (-liquidity, -volume, str(market.get("ticker", "")))


def select_markets(
    markets: Sequence[Mapping[str, Any]],
    categories: Mapping[str, str],
    *,
    sample_size: int,
    max_categories: int,
) -> list[JsonObject]:
    """Select a deterministic category/price/quote-state round-robin sample."""

    eligible = [
        dict(market)
        for market in markets
        if market.get("market_type") == "binary"
        and isinstance(market.get("ticker"), str)
        and isinstance(market.get("event_ticker"), str)
    ]
    category_counts = Counter(
        categories.get(str(market["event_ticker"]), "Unknown")
        for market in eligible
    )
    allowed_categories = {
        category
        for category, _count in sorted(
            category_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[:max_categories]
    }
    groups: dict[tuple[str, str, str], list[JsonObject]] = defaultdict(list)
    for market in eligible:
        category = categories.get(str(market["event_ticker"]), "Unknown")
        if category not in allowed_categories:
            continue
        key = (category, price_band(market), quote_state(market))
        groups[key].append(market)
    for group in groups.values():
        group.sort(key=_market_sort_key)

    selected: list[JsonObject] = []
    ordered_groups = sorted(groups.items())
    indexes = {key: 0 for key, _group in ordered_groups}
    while len(selected) < sample_size:
        added = False
        for key, group in ordered_groups:
            index = indexes[key]
            if index >= len(group):
                continue
            selected.append(group[index])
            indexes[key] = index + 1
            added = True
            if len(selected) == sample_size:
                break
        if not added:
            break
    return sorted(selected, key=lambda market: str(market["ticker"]))


def _levels_from_value(value: object, *, field: str) -> tuple[BookLevel, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    aggregated: dict[Decimal, Decimal] = {}
    for index, raw_level in enumerate(value):
        if not isinstance(raw_level, (list, tuple)) or len(raw_level) != 2:
            raise ValueError(f"{field}[{index}] must be [price, contracts]")
        price = parse_decimal(raw_level[0], field=f"{field}[{index}].price")
        contracts = parse_decimal(raw_level[1], field=f"{field}[{index}].contracts")
        if not Decimal(0) <= price <= Decimal(1) or contracts <= 0:
            raise ValueError(
                f"{field}[{index}] has an invalid price or contract count"
            )
        aggregated[price] = aggregated.get(price, Decimal(0)) + contracts
    return tuple(
        BookLevel(price, contracts)
        for price, contracts in sorted(aggregated.items(), reverse=True)
    )


def parse_orderbook(payload: Mapping[str, Any]) -> dict[str, tuple[BookLevel, ...]]:
    """Parse the current orderbook_fp payload, with legacy keys as fallback."""

    if isinstance(payload.get("orderbook_fp"), Mapping):
        book = _mapping(payload["orderbook_fp"], field="orderbook_fp")
        yes_key, no_key = "yes_dollars", "no_dollars"
    elif isinstance(payload.get("orderbook"), Mapping):
        book = _mapping(payload["orderbook"], field="orderbook")
        yes_key, no_key = "yes", "no"
    else:
        raise ValueError("Kalshi order-book response has no orderbook_fp object")
    return {
        "yes": _levels_from_value(book.get(yes_key), field=f"orderbook.{yes_key}"),
        "no": _levels_from_value(book.get(no_key), field=f"orderbook.{no_key}"),
    }


def apply_candidate(
    levels: Sequence[BookLevel],
    candidate: HaircutCandidate,
) -> CandidateMetrics:
    selected = tuple(levels[: candidate.observed_levels])
    raw_depth = sum((level.contracts for level in selected), start=Decimal(0))
    remaining_haircut = raw_depth * candidate.maximum_ignored_fraction
    adjusted: list[tuple[BookLevel, Decimal]] = []
    for index, level in enumerate(selected):
        ignored = Decimal(0)
        if index < candidate.ignored_best_levels and remaining_haircut > 0:
            ignored = min(level.contracts, remaining_haircut)
            remaining_haircut -= ignored
        adjusted.append((level, ignored))
    ignored_depth = sum((ignored for _level, ignored in adjusted), start=Decimal(0))
    positive = [
        (level, ignored, level.contracts - ignored)
        for level, ignored in adjusted
        if level.contracts - ignored > 0
    ]
    executable = positive[: candidate.effective_levels]
    effective_depth = sum(
        (effective for _level, _ignored, effective in executable),
        start=Decimal(0),
    )
    tail_depth_excluded = sum(
        (
            effective
            for _level, _ignored, effective in positive[candidate.effective_levels :]
        ),
        start=Decimal(0),
    )
    retained_fraction = effective_depth / raw_depth if raw_depth else None
    ignored_fraction = ignored_depth / raw_depth if raw_depth else None
    return CandidateMetrics(
        captured_levels=len(selected),
        executable_levels=len(executable),
        raw_depth=raw_depth,
        ignored_depth=ignored_depth,
        effective_depth=effective_depth,
        retained_fraction=retained_fraction,
        ignored_fraction=ignored_fraction,
        best_level_fully_removed=bool(
            adjusted and adjusted[0][0].contracts == adjusted[0][1]
        ),
        tail_depth_excluded=tail_depth_excluded,
    )


def _price_range_tick(market: Mapping[str, Any]) -> Decimal | None:
    ranges = market.get("price_ranges")
    if not isinstance(ranges, list):
        return None
    ticks: list[Decimal] = []
    for item in ranges:
        if not isinstance(item, Mapping):
            continue
        step = optional_decimal(item.get("step"))
        if step is not None and step > 0:
            ticks.append(step)
    return min(ticks) if ticks else None


def _observed_tick(levels: Sequence[BookLevel]) -> Decimal | None:
    gaps = [
        levels[index - 1].price - levels[index].price
        for index in range(1, len(levels))
        if levels[index - 1].price > levels[index].price
    ]
    return min(gaps) if gaps else None


def _best_price(levels: Sequence[BookLevel]) -> Decimal | None:
    return levels[0].price if levels else None


def _quantile(values: Sequence[Decimal], quantile: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = Decimal(len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - Decimal(lower)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _number(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(Decimal("0.000001")), "f")


def _stats(values: Sequence[Decimal | None]) -> JsonObject:
    present = [value for value in values if value is not None]
    return {
        "n": len(present),
        "min": _number(min(present)) if present else None,
        "p10": _number(_quantile(present, Decimal("0.10"))),
        "median": _number(_quantile(present, Decimal("0.50"))),
        "p90": _number(_quantile(present, Decimal("0.90"))),
        "max": _number(max(present)) if present else None,
    }


def _rate(numerator: int, denominator: int) -> str:
    return (
        _number(Decimal(numerator) / Decimal(denominator))
        if denominator
        else "0.000000"
    )


def _side_records(capture: Mapping[str, Any]) -> tuple[list[JsonObject], list[JsonObject]]:
    side_records: list[JsonObject] = []
    market_records: list[JsonObject] = []
    observations = capture.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ValueError("capture has no observations")
    for raw_observation in observations:
        observation = _mapping(raw_observation, field="capture observation")
        market = _mapping(observation.get("market"), field="observation.market")
        event = _mapping(observation.get("event"), field="observation.event")
        ticker = str(market.get("ticker", ""))
        category = str(event.get("category") or "Unknown")
        book = parse_orderbook(
            _mapping(observation.get("orderbook"), field="observation.orderbook")
        )
        yes_levels = book["yes"]
        no_levels = book["no"]
        yes_best = _best_price(yes_levels)
        no_best = _best_price(no_levels)
        spread = (
            Decimal(1) - yes_best - no_best
            if yes_best is not None and no_best is not None
            else None
        )
        tick = _price_range_tick(market)
        market_records.append(
            {
                "ticker": ticker,
                "category": category,
                "price_band": price_band(market),
                "spread": _number(spread),
                "spread_ticks": _number(
                    spread / tick if spread is not None and tick else None
                ),
                "tick": _number(tick),
                "yes_levels": len(yes_levels),
                "no_levels": len(no_levels),
                "crossed": spread is not None and spread < 0,
            }
        )
        for side_name, levels in (("yes", yes_levels), ("no", no_levels)):
            full_depth = sum((level.contracts for level in levels), start=Decimal(0))
            best_fraction = (
                levels[0].contracts / full_depth
                if levels and full_depth
                else None
            )
            candidate_results: JsonObject = {}
            for candidate in CANDIDATES:
                metrics = apply_candidate(levels, candidate)
                candidate_results[candidate.name] = {
                    "captured_levels": metrics.captured_levels,
                    "executable_levels": metrics.executable_levels,
                    "raw_depth": _number(metrics.raw_depth),
                    "ignored_depth": _number(metrics.ignored_depth),
                    "effective_depth": _number(metrics.effective_depth),
                    "retained_fraction": _number(metrics.retained_fraction),
                    "ignored_fraction": _number(metrics.ignored_fraction),
                    "best_level_fully_removed": metrics.best_level_fully_removed,
                    "tail_depth_excluded": _number(metrics.tail_depth_excluded),
                    "floor_ok": (
                        metrics.retained_fraction is None
                        or metrics.retained_fraction >= Decimal("0.50")
                    ),
                }
            side_records.append(
                {
                    "ticker": ticker,
                    "category": category,
                    "price_band": price_band(market),
                    "side": side_name,
                    "empty": not levels,
                    "full_levels": len(levels),
                    "full_depth": _number(full_depth),
                    "full_best_fraction": _number(best_fraction),
                    "best_price": _number(_best_price(levels)),
                    "observed_tick": _number(_observed_tick(levels)),
                    "candidates": candidate_results,
                }
            )
    return side_records, market_records


def _group_summary(
    side_records: Sequence[Mapping[str, Any]],
    market_records: Sequence[Mapping[str, Any]],
    *,
    group_key: str,
    group_value: str,
) -> JsonObject:
    sides = [record for record in side_records if record[group_key] == group_value]
    markets = [record for record in market_records if record[group_key] == group_value]
    candidates: JsonObject = {}
    for candidate in CANDIDATES:
        values = [
            optional_decimal(
                _mapping(record["candidates"], field="candidate results")[
                    candidate.name
                ].get("retained_fraction")
            )
            for record in sides
        ]
        floor_values = [
            _mapping(record["candidates"], field="candidate results")[candidate.name].get(
                "floor_ok"
            )
            for record in sides
            if not record["empty"]
        ]
        candidates[candidate.name] = {
            "retained_fraction": _stats(values),
            "floor_ok_rate": _rate(
                sum(value is True for value in floor_values),
                len(floor_values),
            ),
            "floor_violations": sum(value is False for value in floor_values),
        }
    return {
        "value": group_value,
        "markets": len(markets),
        "side_observations": len(sides),
        "empty_side_rate": _rate(
            sum(bool(record["empty"]) for record in sides),
            len(sides),
        ),
        "full_levels": _stats([Decimal(str(record["full_levels"])) for record in sides]),
        "full_depth": _stats(
            [optional_decimal(record["full_depth"]) for record in sides]
        ),
        "full_best_fraction": _stats(
            [optional_decimal(record["full_best_fraction"]) for record in sides]
        ),
        "spread": _stats([optional_decimal(record["spread"]) for record in markets]),
        "spread_ticks": _stats(
            [optional_decimal(record["spread_ticks"]) for record in markets]
        ),
        "tick": _stats([optional_decimal(record["tick"]) for record in markets]),
        "candidates": candidates,
    }


def _group_summary_all(
    side_records: Sequence[Mapping[str, Any]],
    market_records: Sequence[Mapping[str, Any]],
) -> JsonObject:
    return {
        "value": "all",
        "markets": len(market_records),
        "side_observations": len(side_records),
        "empty_side_rate": _rate(
            sum(bool(record["empty"]) for record in side_records),
            len(side_records),
        ),
        "full_levels": _stats(
            [Decimal(str(record["full_levels"])) for record in side_records]
        ),
        "full_depth": _stats(
            [optional_decimal(record["full_depth"]) for record in side_records]
        ),
        "full_best_fraction": _stats(
            [optional_decimal(record["full_best_fraction"]) for record in side_records]
        ),
        "spread": _stats([optional_decimal(record["spread"]) for record in market_records]),
        "spread_ticks": _stats(
            [optional_decimal(record["spread_ticks"]) for record in market_records]
        ),
        "tick": _stats([optional_decimal(record["tick"]) for record in market_records]),
    }


def _recommend(candidate_overall: Mapping[str, Any]) -> JsonObject:
    current = candidate_overall["current-six-observed-five-effective"]
    current_violations = int(current["floor_violations"])
    if current_violations == 0:
        name = "current-six-observed-five-effective"
        rationale = (
            "The existing rule is the least disruptive candidate and retained at "
            "least 50% of captured depth for every non-empty sampled side."
        )
    else:
        preferred = (
            "best-level-50pct-6x6",
            "best-level-25pct-6x6",
            "top-two-50pct-7x7",
            "no-haircut-5x5",
        )
        name = next(
            (
                candidate
                for candidate in preferred
                if int(candidate_overall[candidate]["floor_violations"]) == 0
            ),
            None,
        )
        rationale = (
            f"The existing rule violated the 50% retained-depth floor on "
            f"{current_violations} non-empty sampled side(s); the proposed "
            "alternative is the first conservative candidate in the documented "
            "preference order with no observed violation."
        )
    if name is None:
        return {
            "candidate": None,
            "status": "no_candidate_passed_observed_floor",
            "rationale": rationale,
        }
    return {
        "candidate": name,
        "status": "prototype_recommendation_pending_owner_review",
        "rationale": rationale,
        "production_change": "none",
    }


def analyze_capture(capture: Mapping[str, Any]) -> JsonObject:
    if capture.get("capture_version") != CAPTURE_VERSION:
        raise ValueError(f"unsupported capture version: {capture.get('capture_version')!r}")
    side_records, market_records = _side_records(capture)
    categories = sorted({str(record["category"]) for record in side_records})
    bands = sorted({str(record["price_band"]) for record in side_records})
    candidate_overall: JsonObject = {}
    for candidate in CANDIDATES:
        values = [
            optional_decimal(
                _mapping(record["candidates"], field="candidate results")[
                    candidate.name
                ].get("retained_fraction")
            )
            for record in side_records
        ]
        nonempty = [record for record in side_records if not record["empty"]]
        candidate_overall[candidate.name] = {
            "observed_levels": candidate.observed_levels,
            "effective_levels": candidate.effective_levels,
            "ignored_best_levels": candidate.ignored_best_levels,
            "maximum_ignored_fraction": _number(candidate.maximum_ignored_fraction),
            "description": candidate.description,
            "retained_fraction": _stats(values),
            "ignored_fraction": _stats(
                [
                    optional_decimal(
                        _mapping(record["candidates"], field="candidate results")[
                            candidate.name
                        ].get("ignored_fraction")
                    )
                    for record in side_records
                ]
            ),
            "effective_depth": _stats(
                [
                    optional_decimal(
                        _mapping(record["candidates"], field="candidate results")[
                            candidate.name
                        ].get("effective_depth")
                    )
                    for record in side_records
                ]
            ),
            "tail_depth_excluded": _stats(
                [
                    optional_decimal(
                        _mapping(record["candidates"], field="candidate results")[
                            candidate.name
                        ].get("tail_depth_excluded")
                    )
                    for record in side_records
                ]
            ),
            "best_level_fully_removed_rate": _rate(
                sum(
                    bool(
                        _mapping(record["candidates"], field="candidate results")[
                            candidate.name
                        ].get("best_level_fully_removed")
                    )
                    for record in nonempty
                ),
                len(nonempty),
            ),
            "floor_ok_rate": _rate(
                sum(
                    bool(
                        _mapping(record["candidates"], field="candidate results")[
                            candidate.name
                        ].get("floor_ok")
                    )
                    for record in nonempty
                ),
                len(nonempty),
            ),
            "floor_violations": sum(
                not bool(
                    _mapping(record["candidates"], field="candidate results")[
                        candidate.name
                    ].get("floor_ok")
                )
                for record in nonempty
            ),
            "nonempty_side_observations": len(nonempty),
        }
    recommendation = _recommend(candidate_overall)
    return {
        "analysis_version": ANALYSIS_VERSION,
        "capture_sha256": canonical_sha256(capture),
        "candidate_order": [candidate.name for candidate in CANDIDATES],
        "sample": capture.get("selection", {}),
        "overall": {
            "markets": len(market_records),
            "side_observations": len(side_records),
            "categories": categories,
            "price_bands": bands,
            "empty_side_rate": _rate(
                sum(bool(record["empty"]) for record in side_records),
                len(side_records),
            ),
            "crossed_market_count": sum(
                bool(record["crossed"]) for record in market_records
            ),
            "full_best_fraction": _stats(
                [optional_decimal(record["full_best_fraction"]) for record in side_records]
            ),
            "full_depth": _stats(
                [optional_decimal(record["full_depth"]) for record in side_records]
            ),
            "spread": _stats(
                [optional_decimal(record["spread"]) for record in market_records]
            ),
            "spread_ticks": _stats(
                [optional_decimal(record["spread_ticks"]) for record in market_records]
            ),
            "tick": _stats(
                [optional_decimal(record["tick"]) for record in market_records]
            ),
            "candidates": candidate_overall,
        },
        "by_category": [
            _group_summary(
                side_records,
                market_records,
                group_key="category",
                group_value=value,
            )
            for value in categories
        ],
        "by_price_band": [
            _group_summary(
                side_records,
                market_records,
                group_key="price_band",
                group_value=value,
            )
            for value in bands
        ],
        "recommendation": recommendation,
        "side_observations": side_records,
        "market_observations": market_records,
    }


def _fmt(value: object) -> str:
    if value is None:
        return "n/a"
    return str(value)


def _stats_cell(stats: Mapping[str, Any], key: str = "median") -> str:
    return _fmt(stats.get(key))


def render_report(analysis: Mapping[str, Any], capture: Mapping[str, Any]) -> str:
    overall = _mapping(analysis["overall"], field="analysis.overall")
    recommendation = _mapping(
        analysis["recommendation"],
        field="analysis.recommendation",
    )
    candidates = _mapping(overall["candidates"], field="analysis.overall.candidates")
    source = _mapping(capture.get("source"), field="capture.source")
    selection = _mapping(capture.get("selection"), field="capture.selection")
    lines = [
        "# Kalshi liquidity haircut analysis",
        "",
        f"Analysis version: {analysis['analysis_version']}  ",
        f"Capture version: {capture.get('capture_version')}  ",
        f"Capture completed: {source.get('completed_at', 'unknown')}  ",
        f"Capture SHA-256 (canonical JSON): {analysis['capture_sha256']}",
        "",
        "## Scope and evidence",
        "",
        "This is the throwaway prototype requested by GitHub issue "
        "[#5](https://github.com/MINLEGO/v-trade/issues/5). It uses public Kalshi "
        "market metadata and individual orderbook_fp responses; it does not use "
        "credentials, the authenticated bulk-book endpoint, or production broker code.",
        "",
        f"- Markets sampled: **{overall['markets']}**",
        f"- Side observations (YES and NO bid ladders): **{overall['side_observations']}**",
        f"- Categories represented: **{', '.join(overall['categories'])}**",
        f"- Price bands represented: **{', '.join(overall['price_bands'])}**",
        f"- Empty-side rate: **{overall['empty_side_rate']}**",
        "- Sample target / category cap: "
        f"**{selection.get('sample_size')} / {selection.get('max_categories')}**",
        f"- Markets endpoint: {source.get('markets_url')}",
        f"- Events endpoint: {source.get('events_url')}",
        f"- Order-book endpoint: {source.get('orderbook_url_template')}",
        "",
        "The two API bid ladders are analyzed as separate contract sides. A BUY ask "
        "ladder is the complementary NO/YES bid ladder at 1 - price, so contract "
        "depth is not duplicated; the market spread is counted once from the two best bids.",
        "",
        "## Prototype recommendation",
        "",
        f"**{_fmt(recommendation.get('candidate'))}** "
        f"({recommendation.get('status', 'unknown')}). "
        f"{recommendation.get('rationale', '')}",
        "",
        "This recommendation is evidence from one captured sample, not a statistical "
        "guarantee and not a production configuration change. Human review must decide "
        "whether to version a production rule and schedule a new migration/configuration "
        "change.",
        "",
        "## Candidate comparison",
        "",
        "Captured raw depth is the sum of the candidate's first observed_levels; "
        "retained depth is the executable sum after the haircut and executable-level cap. "
        "The floor is evaluated only on non-empty sampled sides.",
        "",
        "| Candidate | Observed / executable | Ignored best levels | Max haircut | "
        "Median retained | P10 retained | Floor violations | Median tail excluded |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for candidate in CANDIDATES:
        item = _mapping(candidates[candidate.name], field="candidate summary")
        retained = _mapping(item["retained_fraction"], field="retained stats")
        tail = _mapping(item["tail_depth_excluded"], field="tail stats")
        lines.append(
            "| "
            f"{candidate.name} | {candidate.observed_levels} / {candidate.effective_levels} | "
            f"{candidate.ignored_best_levels} | {candidate.maximum_ignored_fraction} | "
            f"{_stats_cell(retained)} | {_stats_cell(retained, 'p10')} | "
            f"{item['floor_violations']} | {_stats_cell(tail)} |"
        )
    lines.extend(
        [
            "",
            "The current rule is included as a diagnostic baseline even when its "
            "five-level tail truncation violates the explicit 50% floor. Alternatives "
            "are not promoted to production by this artifact.",
            "",
            "## Depth, spreads, ticks, and empty sides",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
            "| Full-ladder best-level fraction, median / p90 | "
            f"{_stats_cell(overall['full_best_fraction'])} / "
            f"{_stats_cell(overall['full_best_fraction'], 'p90')} |",
            f"| Full-ladder depth, median contracts | {_stats_cell(overall['full_depth'])} |",
            f"| Complementary best-bid spread, median | {_stats_cell(overall['spread'])} |",
            f"| Complementary spread, median ticks | {_stats_cell(overall['spread_ticks'])} |",
            f"| Minimum configured tick, median dollars | {_stats_cell(overall['tick'])} |",
            f"| Crossed books | {overall['crossed_market_count']} |",
            "",
            "## By category",
            "",
            "| Category | Markets | Side obs. | Empty-side rate | Best fraction median | "
            "Spread median | Tick median | Current retained median | "
            "Current floor violations |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for group in analysis["by_category"]:
        item = _mapping(group, field="category group")
        current = _mapping(
            _mapping(item["candidates"], field="group candidates")[
                "current-six-observed-five-effective"
            ],
            field="current group candidate",
        )
        lines.append(
            f"| {item['value']} | {item['markets']} | {item['side_observations']} | "
            f"{item['empty_side_rate']} | {_stats_cell(item['full_best_fraction'])} | "
            f"{_stats_cell(item['spread'])} | {_stats_cell(item['tick'])} | "
            f"{_stats_cell(current['retained_fraction'])} | {current['floor_violations']} |"
        )
    lines.extend(
        [
            "",
            "## By price band",
            "",
            "| Best-quote midpoint band | Markets | Side obs. | Empty-side rate | "
            "Best fraction median | Spread median | Tick median | "
            "Current retained median | Current floor violations |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for group in analysis["by_price_band"]:
        item = _mapping(group, field="price-band group")
        current = _mapping(
            _mapping(item["candidates"], field="group candidates")[
                "current-six-observed-five-effective"
            ],
            field="current group candidate",
        )
        lines.append(
            f"| {item['value']} | {item['markets']} | {item['side_observations']} | "
            f"{item['empty_side_rate']} | {_stats_cell(item['full_best_fraction'])} | "
            f"{_stats_cell(item['spread'])} | {_stats_cell(item['tick'])} | "
            f"{_stats_cell(current['retained_fraction'])} | {current['floor_violations']} |"
        )
    lines.extend(
        [
            "",
            "## Reproduction",
            "",
            "The capture is preserved in "
            "docs/kalshi-liquidity-haircut-capture.json; rerun the offline analysis with:",
            "",
            "    uv run python scripts/analyze_kalshi_liquidity_haircut.py "
            "--capture docs/kalshi-liquidity-haircut-capture.json "
            "--analysis-json docs/kalshi-liquidity-haircut-analysis.json "
            "--report docs/kalshi-liquidity-haircut-analysis.md",
            "",
            "To refresh the public sample, add --fetch. A refresh replaces the capture "
            "artifact and must be reviewed as a new evidence snapshot.",
            "",
            "## Non-goals",
            "",
            "- No production haircut, migration, experiment, or broker behavior changes.",
            "- No claim of out-of-sample protection, fill probability, or "
            "venue-performance equivalence.",
            "- No authenticated access, order submission, WebSocket data, or real-money execution.",
            "",
        ]
    )
    return "\n".join(lines)


def _capture_live(
    *,
    base_url: str,
    sample_size: int,
    max_categories: int,
    workers: int,
    timeout_seconds: float,
    deadline_seconds: float,
) -> JsonObject:
    started_at = utc_now()
    deadline = time.monotonic() + deadline_seconds
    markets_url = _page_url(
        base_url,
        "/markets",
        {"status": "open", "mve_filter": "exclude", "limit": "1000"},
    )
    events_url = _page_url(
        base_url,
        "/events",
        {"status": "open", "with_nested_markets": "false", "limit": "200"},
    )
    markets, market_summary = _fetch_pages(
        base_url,
        path="/markets",
        collection_key="markets",
        identity_key="ticker",
        parameters={"status": "open", "mve_filter": "exclude"},
        page_limit=1000,
        timeout_seconds=timeout_seconds,
        deadline=deadline,
    )
    events, event_summary = _fetch_pages(
        base_url,
        path="/events",
        collection_key="events",
        identity_key="event_ticker",
        parameters={"status": "open", "with_nested_markets": "false"},
        page_limit=200,
        timeout_seconds=timeout_seconds,
        deadline=deadline,
    )
    event_map = {
        str(event["event_ticker"]): str(event.get("category") or "Unknown")
        for event in events
    }
    selected = select_markets(
        markets,
        event_map,
        sample_size=sample_size,
        max_categories=max_categories,
    )
    if not selected:
        raise ValueError("no eligible binary markets were selected")

    events_by_ticker = {
        str(event["event_ticker"]): event
        for event in events
    }

    def fetch_observation(market: Mapping[str, Any]) -> JsonObject:
        ticker = str(market["ticker"])
        url = (
            f"{base_url.rstrip('/')}/markets/{quote(ticker, safe='')}/orderbook"
            "?depth=0"
        )
        orderbook = _request_json(
            url,
            timeout_seconds=timeout_seconds,
            deadline=deadline,
        )
        event = events_by_ticker.get(str(market["event_ticker"]), {})
        return {
            "market": dict(market),
            "event": {
                "event_ticker": str(market["event_ticker"]),
                "series_ticker": event.get("series_ticker"),
                "category": event.get("category") or "Unknown",
                "title": event.get("title"),
            },
            "orderbook": orderbook,
            "orderbook_sha256": canonical_sha256(orderbook),
            "orderbook_url": url,
        }

    with ThreadPoolExecutor(max_workers=workers) as executor:
        observations = list(executor.map(fetch_observation, selected))
    completed_at = utc_now()
    return {
        "capture_version": CAPTURE_VERSION,
        "source": {
            "base_url": base_url.rstrip("/"),
            "markets_url": markets_url,
            "events_url": events_url,
            "orderbook_url_template": (
                f"{base_url.rstrip('/')}/markets/{{ticker}}/orderbook?depth=0"
            ),
            "started_at": started_at,
            "completed_at": completed_at,
            "user_agent": USER_AGENT,
            "transport": {
                "timeout_seconds": timeout_seconds,
                "attempts": 3,
                "retry_status_codes": sorted(TRANSIENT_STATUS_CODES),
                "workers": workers,
                "deadline_seconds": deadline_seconds,
            },
        },
        "catalogue": market_summary,
        "events": event_summary,
        "selection": {
            "sample_size": sample_size,
            "max_categories": max_categories,
            "selected_markets": len(selected),
            "selected_tickers": [str(market["ticker"]) for market in selected],
        },
        "observations": observations,
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true", help="refresh the public Kalshi capture")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--capture", type=Path, default=DEFAULT_CAPTURE)
    parser.add_argument("--analysis-json", type=Path, default=DEFAULT_ANALYSIS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--sample-size", type=int, default=48)
    parser.add_argument("--max-categories", type=int, default=8)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--deadline-seconds", type=float, default=600.0)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.sample_size <= 0 or args.max_categories <= 0 or args.workers <= 0:
        raise SystemExit("sample size, category cap, and workers must be positive")
    if args.timeout_seconds <= 0 or args.deadline_seconds <= 0:
        raise SystemExit("timeouts must be positive")
    if args.fetch:
        capture = _capture_live(
            base_url=args.base_url,
            sample_size=args.sample_size,
            max_categories=args.max_categories,
            workers=args.workers,
            timeout_seconds=args.timeout_seconds,
            deadline_seconds=args.deadline_seconds,
        )
        _write_json(args.capture, capture)
    else:
        capture = _mapping(
            json.loads(args.capture.read_text(encoding="utf-8")),
            field="capture file",
        )
    analysis = analyze_capture(capture)
    _write_json(args.analysis_json, analysis)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(analysis, capture), encoding="utf-8")
    recommendation = analysis["recommendation"].get("candidate")
    print(
        f"Analyzed {analysis['overall']['markets']} markets and "
        f"{analysis['overall']['side_observations']} side observations; "
        f"prototype recommendation: {recommendation or 'none'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
