from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from vtrade.frozen_artifacts import (
    FROZEN_EXPERIMENT_CONFIGS,
    FrozenArtifactError,
    canonical_artifact_sha256,
    verify_experiment_config,
)
from vtrade.worker import (
    ProductionCompositionUnavailable,
    _verify_frozen_artifact,
    run_worker,
)


class FrozenArtifactTests(unittest.TestCase):
    def test_smoke_check_prints_expected_and_actual_hashes(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            for config in FROZEN_EXPERIMENT_CONFIGS:
                verify_experiment_config(config)

        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 4)
        for line in lines:
            self.assertRegex(line, r"expected=[0-9a-f]{64} actual=[0-9a-f]{64}")

    def test_hash_contract_rejects_non_lf_bytes(self) -> None:
        with self.assertRaisesRegex(FrozenArtifactError, "LF line endings"):
            canonical_artifact_sha256(b"first\r\nsecond\n", label="test artifact")

    def test_worker_verification_uses_the_same_lf_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tool-schemas.json"
            content = b'{"schema":true}\r\n'
            path.write_bytes(content)
            raw = {
                "artifacts": {
                    "tool_schemas": {
                        "path": str(path),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                }
            }

            with self.assertRaisesRegex(ProductionCompositionUnavailable, "LF line endings"):
                _verify_frozen_artifact(raw, "tool_schemas")

    def test_worker_verifies_artifacts_before_environment_composition(self) -> None:
        raw = json.loads(
            Path("config/experiments/predictionarena-polymarket-v1-liquidity-aware.json")
            .read_text(encoding="utf-8")
        )
        raw["artifacts"]["tool_schemas"]["sha256"] = "0" * 64
        for decision in raw["owner_decisions"].values():
            decision["status"] = "resolved"

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "experiment.json"
            config.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ProductionCompositionUnavailable, "hash mismatch"):
                run_worker(config, environment={})


if __name__ == "__main__":
    unittest.main()
