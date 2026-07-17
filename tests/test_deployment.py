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

    def test_services_wait_for_successful_migrations_and_are_hardened(self) -> None:
        compose = Path("compose.coolify.yaml").read_text(encoding="utf-8")
        self.assertIn('command: ["python", "-m", "vtrade.migrate"]', compose)
        self.assertEqual(compose.count("condition: service_completed_successfully"), 2)
        self.assertGreaterEqual(compose.count("read_only: true"), 3)
        self.assertGreaterEqual(compose.count("cap_drop: [ALL]"), 3)
        self.assertIn("VTRADE_ADMIN_AUTH_SECRET", compose)


if __name__ == "__main__":
    unittest.main()
