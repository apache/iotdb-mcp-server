from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iotdb_mcp_server.config import Config  # noqa: E402
from iotdb_mcp_server.session_manager import IoTDBSessionManager  # noqa: E402
from iotdb_mcp_server.services.target_selection import apply_target_registry  # noqa: E402
from iotdb_mcp_server.services.target_selection import _ainode_probe_cache_key  # noqa: E402
from iotdb_mcp_server.services.target_selection import _target_payload  # noqa: E402
from iotdb_mcp_server.target_registry import (  # noqa: E402
    IoTDBTargetRegistry,
    mark_target_last_known_good,
    merge_target,
    target_from_mapping,
)


def _cloud_target(password: str):
    return target_from_mapping(
        {
            "target_id": "cloud-192-168-99-20",
            "display_name": "Cloud IoTDB 192.168.99.20",
            "kind": "cloud",
            "host": "192.168.99.20",
            "port": 6667,
            "node_urls": ["192.168.99.20:6667"],
            "user": "root",
            "password": password,
            "database": "test",
            "sql_dialect": "tree",
            "timezone": "+00:00",
        }
    )


class SessionManagerAuthCacheTest(unittest.TestCase):
    def test_auth_fingerprint_tracks_password_without_changing_public_fingerprint(self) -> None:
        password_target = _cloud_target("TimechoDB@2021")
        empty_password_target = _cloud_target("")

        self.assertEqual(password_target.fingerprint(), empty_password_target.fingerprint())
        self.assertNotEqual(
            password_target.auth_fingerprint(),
            empty_password_target.auth_fingerprint(),
        )

    def test_empty_password_override_is_preserved(self) -> None:
        password_target = _cloud_target("TimechoDB@2021")
        empty_password_target = merge_target(password_target, {"password": ""})

        self.assertEqual(empty_password_target.password, "")
        self.assertFalse(empty_password_target.password_set)
        self.assertNotEqual(
            password_target.auth_fingerprint(),
            empty_password_target.auth_fingerprint(),
        )

    def test_last_known_good_credential_is_used_when_password_is_missing(self) -> None:
        target = target_from_mapping(
            {
                "target_id": "cloud-192-168-99-20",
                "host": "192.168.99.20",
                "port": 6667,
                "user": "root",
                "last_known_good_credential": {
                    "user": "root",
                    "password": "",
                    "last_success_at": "2026-07-10T00:00:00+00:00",
                },
            }
        )

        self.assertEqual(target.password, "")
        self.assertEqual(target.last_known_good_credential["password"], "")
        self.assertFalse(target.password_set)

    def test_last_known_good_non_empty_password_is_used_when_password_is_missing(self) -> None:
        target = target_from_mapping(
            {
                "target_id": "cloud-192-168-99-20",
                "host": "192.168.99.20",
                "port": 6667,
                "user": "root",
                "last_known_good_credential": {
                    "user": "root",
                    "password": "known-good",
                    "last_success_at": "2026-07-10T00:00:00+00:00",
                },
            }
        )

        self.assertEqual(target.password, "known-good")
        self.assertTrue(target.password_set)

    def test_explicit_password_overrides_last_known_good_credential(self) -> None:
        target = target_from_mapping(
            {
                "target_id": "cloud-192-168-99-20",
                "host": "192.168.99.20",
                "port": 6667,
                "user": "root",
                "password": "",
                "last_known_good_credential": {
                    "user": "root",
                    "password": "known-good",
                    "last_success_at": "2026-07-10T00:00:00+00:00",
                },
            }
        )

        self.assertEqual(target.password, "")
        self.assertFalse(target.password_set)

    def test_last_known_good_credential_is_redacted_in_public_target_payload(self) -> None:
        target = mark_target_last_known_good(
            _cloud_target("known-good"),
            last_success_at="2026-07-10T00:00:00+00:00",
        )

        public = target.as_dict(include_secret=False)
        secret = target.as_dict(include_secret=True)

        self.assertEqual(public["password"], "***")
        self.assertEqual(public["last_known_good_credential"]["password"], "***")
        self.assertEqual(secret["last_known_good_credential"]["password"], "known-good")

    def test_mark_last_known_good_updates_effective_credential(self) -> None:
        target = mark_target_last_known_good(
            _cloud_target(""),
            password="known-good",
            last_success_at="2026-07-10T00:00:00+00:00",
        )

        self.assertEqual(target.password, "known-good")
        self.assertEqual(target.last_known_good_credential["password"], "known-good")
        self.assertTrue(target.password_set)

    def test_config_round_trip_preserves_last_known_good_credential(self) -> None:
        target = mark_target_last_known_good(
            _cloud_target(""),
            last_success_at="2026-07-10T00:00:00+00:00",
        )
        config = Config.from_target(target)

        round_trip = config.to_target()

        self.assertEqual(round_trip.password, "")
        self.assertEqual(round_trip.last_known_good_credential["password"], "")
        self.assertEqual(
            round_trip.last_known_good_credential["last_success_at"],
            "2026-07-10T00:00:00+00:00",
        )

    def test_tree_pool_cache_is_not_reused_across_password_change(self) -> None:
        password_target = _cloud_target("TimechoDB@2021")
        empty_password_target = _cloud_target("")
        manager = IoTDBSessionManager(
            IoTDBTargetRegistry(
                {password_target.target_id: password_target},
                password_target.target_id,
            )
        )
        created_pools = [object(), object()]

        with patch(
            "iotdb_mcp_server.session_manager.create_tree_session_pool",
            side_effect=created_pools,
        ) as create_pool:
            first_pool = manager.tree_pool(password_target.target_id)
            manager.update_registry(
                IoTDBTargetRegistry(
                    {empty_password_target.target_id: empty_password_target},
                    empty_password_target.target_id,
                ),
                close_pools=False,
            )
            second_pool = manager.tree_pool(empty_password_target.target_id)

        self.assertIs(first_pool, created_pools[0])
        self.assertIs(second_pool, created_pools[1])
        self.assertEqual(create_pool.call_count, 2)

    def test_table_pool_cache_is_not_reused_across_password_change(self) -> None:
        password_target = _cloud_target("TimechoDB@2021")
        empty_password_target = _cloud_target("")
        manager = IoTDBSessionManager(
            IoTDBTargetRegistry(
                {password_target.target_id: password_target},
                password_target.target_id,
            )
        )
        created_pools = [object(), object()]

        with patch(
            "iotdb_mcp_server.session_manager.create_table_session_pool",
            side_effect=created_pools,
        ) as create_pool:
            first_pool = manager.table_pool(password_target.target_id)
            manager.update_registry(
                IoTDBTargetRegistry(
                    {empty_password_target.target_id: empty_password_target},
                    empty_password_target.target_id,
                ),
                close_pools=False,
            )
            second_pool = manager.table_pool(empty_password_target.target_id)

        self.assertIs(first_pool, created_pools[0])
        self.assertIs(second_pool, created_pools[1])
        self.assertEqual(create_pool.call_count, 2)

    def test_ainode_probe_cache_key_tracks_password_change(self) -> None:
        password_config = Config.from_target(_cloud_target("TimechoDB@2021"))
        empty_password_config = Config.from_target(_cloud_target(""))

        self.assertEqual(
            password_config.target_fingerprint,
            empty_password_config.target_fingerprint,
        )
        self.assertNotEqual(
            _ainode_probe_cache_key(password_config),
            _ainode_probe_cache_key(empty_password_config),
        )

    def test_target_payload_does_not_probe_ainode_implicitly(self) -> None:
        config = Config.from_target(_cloud_target("known-good"))

        with patch(
            "iotdb_mcp_server.services.target_selection._probe_ainode_availability"
        ) as probe:
            payload = _target_payload(config)

        probe.assert_not_called()
        self.assertEqual(payload["ainode_available"], "unknown")
        self.assertEqual(payload["ainode_availability_source"]["method"], "not_probed")

    def test_empty_registry_application_clears_active_connection_fields(self) -> None:
        config = Config.from_target(_cloud_target("known-good"))

        apply_target_registry(config, IoTDBTargetRegistry({}))

        self.assertEqual(config.target_id, "")
        self.assertEqual(config.host, "")
        self.assertEqual(config.port, 0)
        self.assertEqual(config.node_urls, ())
        self.assertEqual(config.user, "")
        self.assertEqual(config.password, "")
        self.assertEqual(config.last_known_good_credential, {})


if __name__ == "__main__":
    unittest.main()
