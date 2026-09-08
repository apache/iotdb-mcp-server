from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iotdb_mcp_server.config import Config  # noqa: E402


class ConfigFallbackTest(unittest.TestCase):
    def test_legacy_single_target_is_registered_with_session_manager(self) -> None:
        legacy_env = {
            "IOTDB_HOST": "192.168.99.20",
            "IOTDB_PORT": "6667",
            "IOTDB_USER": "root",
            "IOTDB_PASSWORD": "known-good",
            "IOTDB_DATABASE": "legacy_db",
            "IOTDB_SQL_DIALECT": "table",
        }
        with (
            patch.dict(os.environ, legacy_env, clear=True),
            patch.object(
                sys,
                "argv",
                ["iotdb-mcp-server"],
            ),
        ):
            config = Config.from_env_arguments()

        self.assertIsNotNone(config.target_registry)
        self.assertIsNotNone(config.session_manager)
        manager = config.session_manager
        registry = config.target_registry
        assert manager is not None
        assert registry is not None
        target = manager.registry.resolve(config.target_id)
        self.assertEqual(target.host, "192.168.99.20")
        self.assertEqual(target.database, "legacy_db")
        self.assertEqual(registry.resolve(config.target_id), target)


if __name__ == "__main__":
    unittest.main()
