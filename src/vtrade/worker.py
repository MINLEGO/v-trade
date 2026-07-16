from __future__ import annotations

import os

from vtrade.config import ConfigurationError, load_experiment_config


def main() -> None:
    config_path = os.getenv(
        "VTRADE_EXPERIMENT_CONFIG", "config/experiments/predictionarena-polymarket-v1.json"
    )
    config = load_experiment_config(config_path)
    config.assert_runnable()
    raise ConfigurationError(
        "worker scheduling is unavailable until persistence resources are supplied"
    )


if __name__ == "__main__":
    main()
