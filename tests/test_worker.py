from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vtrade.config import ConfigurationError
from vtrade.worker import ProductionCompositionUnavailable, run_worker


def _write_config(directory: str, *, pending: bool) -> Path:
    path = Path(directory) / "experiment.json"
    path.write_text(
        json.dumps(
            {
                "experiment_version": "worker-test-v1",
                "classifications": {},
                "limits": {},
                "owner_decisions": {
                    "pagination": {
                        "status": "owner_pending" if pending else "resolved",
                        "required": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


class WorkerFailClosedTests(unittest.TestCase):
    def test_owner_decisions_fail_before_composition_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_config(directory, pending=True)
            with self.assertRaisesRegex(ConfigurationError, "pagination"):
                run_worker(path)

    def test_runnable_config_refuses_missing_production_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = _write_config(directory, pending=False)
            with self.assertRaisesRegex(
                ProductionCompositionUnavailable,
                "Polymarket fee-policy source and monthly Exa request caps",
            ):
                run_worker(path)


if __name__ == "__main__":
    unittest.main()
