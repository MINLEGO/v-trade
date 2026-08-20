from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType

SCRIPT = Path("scripts/analyze_kalshi_liquidity_haircut.py")


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("kalshi_liquidity_haircut", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("unable to load issue-5 analysis script")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _market(ticker: str, event_ticker: str, *, yes_bid: str = "0.40") -> dict:
    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "market_type": "binary",
        "yes_bid_dollars": yes_bid,
        "yes_ask_dollars": "0.60",
        "no_bid_dollars": "0.30",
        "liquidity_dollars": "100.0000",
        "volume_fp": "10.00",
        "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0100"}],
    }


def _capture(module: ModuleType) -> dict:
    first = _market("MKT-1", "EVT-1")
    second = _market("MKT-2", "EVT-2", yes_bid="0.70")
    return {
        "capture_version": module.CAPTURE_VERSION,
        "source": {"completed_at": "2026-08-20T00:00:00Z"},
        "selection": {"sample_size": 2, "max_categories": 2},
        "observations": [
            {
                "market": first,
                "event": {"category": "Sports"},
                "orderbook": {
                    "orderbook_fp": {
                        "yes_dollars": [
                            ["0.45", "60"],
                            ["0.44", "1"],
                            ["0.43", "1"],
                            ["0.42", "1"],
                            ["0.41", "1"],
                            ["0.40", "1"],
                        ],
                        "no_dollars": [],
                    }
                },
            },
            {
                "market": second,
                "event": {"category": "World"},
                "orderbook": {
                    "orderbook_fp": {
                        "yes_dollars": [
                            ["0.70", "2"],
                            ["0.69", "2"],
                            ["0.68", "2"],
                        ],
                        "no_dollars": [
                            ["0.20", "3"],
                            ["0.19", "3"],
                        ],
                    }
                },
            },
        ],
    }


def test_current_rule_reports_tail_truncation_floor_violation() -> None:
    module = _load_script()
    levels = tuple(
        module.BookLevel(Decimal(f"0.{40 + index}"), Decimal("1"))
        for index in range(6)
    )
    levels = (module.BookLevel(Decimal("0.40"), Decimal("60")), *levels[1:])

    result = module.apply_candidate(
        levels,
        module.CANDIDATES[2],
    )

    assert result.captured_levels == 6
    assert result.executable_levels == 5
    assert result.retained_fraction is not None
    assert result.retained_fraction < Decimal("0.50")
    assert result.tail_depth_excluded == Decimal("1")


def test_six_level_candidate_keeps_the_floor_for_the_same_ladder() -> None:
    module = _load_script()
    levels = (
        module.BookLevel(Decimal("0.40"), Decimal("60")),
        module.BookLevel(Decimal("0.41"), Decimal("1")),
        module.BookLevel(Decimal("0.42"), Decimal("1")),
        module.BookLevel(Decimal("0.43"), Decimal("1")),
        module.BookLevel(Decimal("0.44"), Decimal("1")),
        module.BookLevel(Decimal("0.45"), Decimal("1")),
    )

    result = module.apply_candidate(levels, module.CANDIDATES[3])

    assert result.retained_fraction == Decimal("0.50")
    assert result.tail_depth_excluded == Decimal("0")


def test_parser_accepts_current_and_legacy_shapes() -> None:
    module = _load_script()

    current = module.parse_orderbook(
        {
            "orderbook_fp": {
                "yes_dollars": [["0.42", "1.50"]],
                "no_dollars": [],
            }
        }
    )
    legacy = module.parse_orderbook(
        {
            "orderbook": {
                "yes": [["0.42", "1.50"]],
                "no": [],
            }
        }
    )

    assert current["yes"][0].price == Decimal("0.42")
    assert current["yes"][0].contracts == Decimal("1.50")
    assert legacy == current


def test_analysis_reports_empty_sides_and_recommends_floor_safe_alternative() -> None:
    module = _load_script()

    result = module.analyze_capture(_capture(module))

    assert result["overall"]["markets"] == 2
    assert result["overall"]["side_observations"] == 4
    assert result["overall"]["empty_side_rate"] == "0.250000"
    assert result["overall"]["spread"]["n"] == 1
    assert (
        result["recommendation"]["candidate"]
        == "best-level-50pct-6x6"
    )
    assert (
        result["overall"]["candidates"][
            "current-six-observed-five-effective"
        ]["floor_violations"]
        == 1
    )
