from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iotdb_mcp_server.config import Config  # noqa: E402
from iotdb_mcp_server.target_registry import target_from_mapping  # noqa: E402


class ConfigFallbackTest(unittest.TestCase):
    def test_config_repr_omits_last_known_good_credential(self) -> None:
        config = Config.from_target(
            target_from_mapping(
                {
                    "target_id": "secret-target",
                    "password": "current-secret",
                    "last_known_good_credential": {
                        "user": "root",
                        "password": "cached-secret",
                        "last_success_at": "2026-09-09T00:00:00+00:00",
                    },
                }
            )
        )

        rendered = repr(config)

        self.assertNotIn("last_known_good_credential", rendered)
        self.assertNotIn("cached-secret", rendered)
        self.assertNotIn("current-secret", rendered)

        equivalent = Config.from_target(config.to_target())
        equivalent.last_known_good_credential = {"password": "different-secret"}
        self.assertEqual(config, equivalent)

    def test_legacy_single_target_defaults_to_table_dialect(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(sys, "argv", ["iotdb-mcp-server"]),
        ):
            config = Config.from_env_arguments()

        self.assertEqual(config.sql_dialect, "table")
        assert config.target_registry is not None
        self.assertEqual(
            config.target_registry.resolve(config.target_id).sql_dialect,
            "table",
        )

    def test_legacy_cli_connection_options_override_environment(self) -> None:
        legacy_env = {
            "IOTDB_USE_SSL": "false",
            "IOTDB_CA_CERTS": "/env/ca.pem",
            "IOTDB_CONNECTION_TIMEOUT_MS": "100",
            "IOTDB_ENABLE_REDIRECTION": "true",
            "IOTDB_ENABLE_COMPRESSION": "false",
            "IOTDB_FETCH_SIZE": "200",
            "IOTDB_MAX_RETRY": "2",
            "IOTDB_MAX_POOL_SIZE": "3",
            "IOTDB_WAIT_TIMEOUT_MS": "400",
        }
        argv = [
            "iotdb-mcp-server",
            "--use-ssl",
            "true",
            "--ca-certs",
            "/cli/ca.pem",
            "--connection-timeout-ms",
            "1100",
            "--enable-redirection",
            "false",
            "--enable-compression",
            "true",
            "--fetch-size",
            "1200",
            "--max-retry",
            "12",
            "--max-pool-size",
            "13",
            "--wait-timeout-ms",
            "1400",
        ]
        with (
            patch.dict(os.environ, legacy_env, clear=True),
            patch.object(sys, "argv", argv),
        ):
            config = Config.from_env_arguments()

        self.assertTrue(config.use_ssl)
        self.assertEqual(config.ca_certs, "/cli/ca.pem")
        self.assertEqual(config.connection_timeout_in_ms, 1100)
        self.assertFalse(config.enable_redirection)
        self.assertTrue(config.enable_compression)
        self.assertEqual(config.fetch_size, 1200)
        self.assertEqual(config.max_retry, 12)
        self.assertEqual(config.max_pool_size, 13)
        self.assertEqual(config.tree_wait_timeout_in_ms, 1400)
        self.assertEqual(config.table_wait_timeout_in_ms, 1400)

        assert config.target_registry is not None
        registered = config.target_registry.resolve(config.target_id)
        self.assertEqual(registered, config.to_target())

    def test_legacy_environment_connection_options_are_applied(self) -> None:
        legacy_env = {
            "IOTDB_USE_SSL": "true",
            "IOTDB_CA_CERTS": "/env/ca.pem",
            "IOTDB_CONNECTION_TIMEOUT_MS": "2100",
            "IOTDB_ENABLE_REDIRECTION": "false",
            "IOTDB_ENABLE_COMPRESSION": "true",
            "IOTDB_FETCH_SIZE": "2200",
            "IOTDB_MAX_RETRY": "22",
            "IOTDB_MAX_POOL_SIZE": "23",
            "IOTDB_TREE_WAIT_TIMEOUT_MS": "2400",
            "IOTDB_TABLE_WAIT_TIMEOUT_MS": "2500",
        }
        with (
            patch.dict(os.environ, legacy_env, clear=True),
            patch.object(sys, "argv", ["iotdb-mcp-server"]),
        ):
            config = Config.from_env_arguments()

        self.assertTrue(config.use_ssl)
        self.assertEqual(config.ca_certs, "/env/ca.pem")
        self.assertEqual(config.connection_timeout_in_ms, 2100)
        self.assertFalse(config.enable_redirection)
        self.assertTrue(config.enable_compression)
        self.assertEqual(config.fetch_size, 2200)
        self.assertEqual(config.max_retry, 22)
        self.assertEqual(config.max_pool_size, 23)
        self.assertEqual(config.tree_wait_timeout_in_ms, 2400)
        self.assertEqual(config.table_wait_timeout_in_ms, 2500)

    def test_legacy_node_urls_cli_sets_primary_endpoint(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                sys,
                "argv",
                ["iotdb-mcp-server", "--node-urls", "10.0.0.8:7788"],
            ),
        ):
            config = Config.from_env_arguments()

        self.assertEqual(config.host, "10.0.0.8")
        self.assertEqual(config.port, 7788)
        self.assertEqual(config.node_urls, ("10.0.0.8:7788",))

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
