from __future__ import annotations

import os
from pathlib import Path

from vtrade.config import load_experiment_config
from vtrade.runtime import RuntimeConfigurationError


class ProductionCompositionUnavailable(RuntimeConfigurationError):
    """Raised when required production contracts are not represented in config."""


def run_worker(config_path: str | Path) -> None:
    """Validate the frozen experiment before touching any external resource.

    Keep this guard after ``assert_runnable``: owner decisions must fail first and no
    database, storage, model, research, or venue client may be constructed before it.
    """
    config = load_experiment_config(config_path)
    config.assert_runnable()
    raise ProductionCompositionUnavailable(
        "production worker composition requires the owner-resolved Polymarket fee-policy "
        "source and monthly Exa request caps; no zero-fee or unbounded substitute is allowed"
    )


def main() -> None:
    config_path = os.getenv(
        "VTRADE_EXPERIMENT_CONFIG", "config/experiments/predictionarena-polymarket-v1.json"
    )
    run_worker(config_path)


if __name__ == "__main__":
    main()
