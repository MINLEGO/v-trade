from __future__ import annotations

import unittest
from pathlib import Path


class DeploymentShapeTests(unittest.TestCase):
    def test_image_contains_every_file_referenced_by_runtime_config(self) -> None:
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY config ./config", dockerfile)
        self.assertIn("COPY migrations ./migrations", dockerfile)
        self.assertIn("COPY spec ./spec", dockerfile)
        ignored = Path(".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertNotIn("spec", ignored)
        self.assertIn(".env", ignored)

    def test_image_runs_the_frozen_artifact_smoke_check_at_build_time(self) -> None:
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("RUN python -m vtrade.frozen_artifacts", dockerfile)

    def test_image_installs_only_locked_runtime_dependencies(self) -> None:
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM ghcr.io/astral-sh/uv:0.11.2 AS uv", dockerfile)
        self.assertIn("FROM python:3.12.11-slim-bookworm AS runtime", dockerfile)
        self.assertIn("COPY pyproject.toml uv.lock README.md ./", dockerfile)
        self.assertIn("RUN uv lock --check", dockerfile)
        self.assertIn("RUN uv sync --frozen --no-dev --no-editable --compile-bytecode", dockerfile)
        self.assertIn('ENV PATH="/app/.venv/bin:$PATH"', dockerfile)
        self.assertNotIn("pip install", dockerfile)

    def test_image_smoke_checks_runtime_entrypoint_imports(self) -> None:
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        self.assertIn('RUN python -c "import vtrade, vtrade.api, vtrade.worker"', dockerfile)
        self.assertIn("USER 65532:65532", dockerfile)

    def test_services_wait_for_successful_migrations_and_are_hardened(self) -> None:
        compose = Path("compose.coolify.yaml").read_text(encoding="utf-8")
        self.assertIn('command: ["python", "-m", "vtrade.migrate"]', compose)
        self.assertEqual(compose.count("condition: service_completed_successfully"), 2)
        self.assertGreaterEqual(compose.count("read_only: true"), 3)
        self.assertGreaterEqual(compose.count("cap_drop: [ALL]"), 3)
        self.assertIn("VTRADE_ADMIN_AUTH_SECRET", compose)

    def test_worker_starts_only_after_private_api_readiness(self) -> None:
        compose = Path("compose.coolify.yaml").read_text(encoding="utf-8")
        api = compose.split("\n  api:\n", 1)[1].split("\n  worker:\n", 1)[0]
        worker = compose.split("\n  worker:\n", 1)[1]

        self.assertIn("/health/ready", api)
        self.assertNotIn("/health/live", api)
        self.assertIn("VTRADE_ADMIN_AUTH_SECRET", api)
        self.assertIn("migrate:\n        condition: service_completed_successfully", api)
        self.assertIn("migrate:\n        condition: service_completed_successfully", worker)
        self.assertIn("api:\n        condition: service_healthy", worker)


if __name__ == "__main__":
    unittest.main()
