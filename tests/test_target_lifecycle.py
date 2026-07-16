from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iotdb_mcp_server.session_manager import IoTDBSessionManager  # noqa: E402
from iotdb_mcp_server.session_manager import (  # noqa: E402
    is_iotdb_connection_error,
    make_tree_pool_config,
)
from iotdb_mcp_server.target_lifecycle import (  # noqa: E402
    IoTDBTargetCandidateStore,
    candidate_target,
    verify_target_once,
)
from iotdb_mcp_server.target_registry import (  # noqa: E402
    IoTDBTargetRegistry,
    target_from_mapping,
)


class TargetLifecycleTest(unittest.TestCase):
    def test_empty_registry_is_valid_and_not_resolvable(self) -> None:
        registry = IoTDBTargetRegistry({})

        self.assertIsNone(registry.default_target_id)
        self.assertEqual(registry.list_targets(), [])
        with self.assertRaisesRegex(KeyError, "No verified IoTDB target"):
            registry.resolve()

    def test_from_env_ignores_unverified_persisted_targets(self) -> None:
        payload = {
            "default_target_id": "unverified",
            "targets": {
                "unverified": {
                    "target_id": "unverified",
                    "host": "192.168.99.15",
                    "port": 6667,
                    "user": "root",
                    "password": "stale",
                },
                "verified": {
                    "target_id": "verified",
                    "host": "192.168.99.20",
                    "port": 6667,
                    "user": "root",
                    "password": "known-good",
                    "verified_at": "2026-07-10T00:00:00+00:00",
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            registry = IoTDBTargetRegistry.from_env(
                {"TIMESEEK_IOTDB_TARGETS_FILE": str(path)}
            )

        self.assertEqual(
            [target["target_id"] for target in registry.list_targets()],
            ["verified"],
        )
        self.assertEqual(registry.default_target_id, "verified")

    def test_candidate_rejects_credentials_and_is_one_time(self) -> None:
        store = IoTDBTargetCandidateStore()
        with self.assertRaisesRegex(ValueError, "cannot contain credentials"):
            store.prepare({"host": "192.168.99.15", "password": "secret"})

        candidate = store.prepare({"host": "192.168.99.15", "port": 6667})
        self.assertNotIn("password", candidate.as_dict()["template"])
        self.assertEqual(store.consume(candidate.candidate_id), candidate)
        with self.assertRaisesRegex(KeyError, "already consumed"):
            store.consume(candidate.candidate_id)

    def test_failed_candidate_requires_user_confirmed_retry(self) -> None:
        store = IoTDBTargetCandidateStore(retry_guard_seconds=60)
        candidate = store.prepare({"host": "192.168.99.15", "port": 6667})
        store.consume(candidate.candidate_id)
        store.record_failure(candidate)

        with self.assertRaisesRegex(PermissionError, "explicitly requests"):
            store.prepare({"host": "192.168.99.15", "port": 6667})
        retried = store.prepare(
            {"host": "192.168.99.15", "port": 6667},
            user_confirmed_retry=True,
        )
        self.assertTrue(retried.candidate_id.startswith("candidate-"))

    def test_successful_verification_marks_target_publishable(self) -> None:
        store = IoTDBTargetCandidateStore()
        candidate = store.prepare({"host": "192.168.99.15", "port": 6667})
        target = candidate_target(
            store.consume(candidate.candidate_id),
            user="root",
            password="known-good",
        )
        session = Mock()

        with patch("iotdb_mcp_server.target_lifecycle.Session", return_value=session):
            verified = verify_target_once(target)

        session.open.assert_called_once()
        session.execute_query_statement.assert_called_once_with("SHOW VERSION")
        session.close.assert_called_once()
        self.assertTrue(verified.verified_at)
        self.assertEqual(
            verified.last_known_good_credential["password"], "known-good"
        )

    def test_connection_error_evicts_last_target(self) -> None:
        target = target_from_mapping(
            {
                "target_id": "cloud",
                "host": "192.168.99.15",
                "port": 6667,
                "user": "root",
                "password": "known-good",
                "sql_dialect": "tree",
            }
        )
        manager = IoTDBSessionManager(
            IoTDBTargetRegistry({target.target_id: target}, target.target_id)
        )
        callback = Mock()
        manager.set_connection_error_callback(callback)
        raw_pool = Mock()
        raw_pool.get_session.side_effect = ConnectionError("connection refused")

        with patch(
            "iotdb_mcp_server.session_manager.create_tree_session_pool",
            return_value=raw_pool,
        ):
            pool = manager.tree_pool(target.target_id)
            with self.assertRaises(ConnectionError):
                pool.get_session()

        self.assertEqual(manager.registry.list_targets(), [])
        callback.assert_called_once()

    def test_pool_config_uses_single_primary_endpoint(self) -> None:
        target = target_from_mapping(
            {
                "target_id": "cloud",
                "host": "192.168.99.15",
                "port": 6667,
                "node_urls": ["192.168.99.15:6667", "192.168.99.16:6667"],
                "user": "root",
                "password": "known-good",
            }
        )

        pool_config = make_tree_pool_config(target)

        self.assertEqual(pool_config.host, "192.168.99.15")
        self.assertEqual(pool_config.port, 6667)
        self.assertFalse(pool_config.node_urls)

    def test_generic_sql_error_does_not_count_as_connection_error(self) -> None:
        self.assertFalse(is_iotdb_connection_error(RuntimeError("syntax error")))
        self.assertTrue(is_iotdb_connection_error(RuntimeError("Status code 801")))


if __name__ == "__main__":
    unittest.main()
