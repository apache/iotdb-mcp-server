from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iotdb_mcp_server.config import Config  # noqa: E402
from iotdb_mcp_server.session_manager import IoTDBSessionManager  # noqa: E402
from iotdb_mcp_server.services.database import register_database_tools  # noqa: E402
from iotdb_mcp_server.target_registry import (  # noqa: E402
    IoTDBTargetRegistry,
    target_from_mapping,
)


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return function

        return register


class DatabaseTargetRoutingTest(unittest.TestCase):
    def test_create_database_keeps_explicit_target_through_pool_selection(self) -> None:
        default_target = target_from_mapping(
            {
                "target_id": "default-cloud",
                "host": "192.168.99.20",
                "port": 6667,
                "user": "root",
                "password": "default-secret",
                "sql_dialect": "tree",
                "verified_at": "2026-07-13T00:00:00+00:00",
            }
        )
        explicit_target = target_from_mapping(
            {
                "target_id": "explicit-cloud",
                "host": "192.168.99.15",
                "port": 6667,
                "user": "operator",
                "password": "explicit-secret",
                "sql_dialect": "tree",
                "verified_at": "2026-07-13T00:00:00+00:00",
            }
        )
        registry = IoTDBTargetRegistry(
            {
                default_target.target_id: default_target,
                explicit_target.target_id: explicit_target,
            },
            default_target_id=default_target.target_id,
        )
        manager = IoTDBSessionManager(registry)
        manager.update_registry = Mock(wraps=manager.update_registry)
        pool = Mock()
        session = Mock()
        pool.get_session.return_value = session

        with tempfile.TemporaryDirectory() as directory:
            targets_file = Path(directory) / "iotdb-targets.json"
            targets_file.write_text(
                json.dumps(registry.as_dict(include_secret=True)),
                encoding="utf-8",
            )
            config = Config.from_target(
                default_target,
                target_registry=registry,
                session_manager=manager,
            )
            config.targets_file = str(targets_file)
            config.targets_file_mtime_ns = targets_file.stat().st_mtime_ns
            mcp = _FakeMcp()
            register_database_tools(mcp, config, logging.getLogger(__name__))

            selectors = (
                {"target_id": explicit_target.target_id},
                {"target": {"target_id": explicit_target.target_id}},
            )
            with patch(
                "iotdb_mcp_server.session_manager.create_tree_session_pool",
                return_value=pool,
            ) as create_pool:
                for selector in selectors:
                    with self.subTest(selector=selector):
                        create_pool.reset_mock()
                        session.execute_non_query_statement.reset_mock()
                        asyncio.run(
                            mcp.tools["create_database"](
                                "root.explicit_test",
                                **selector,
                            )
                        )
                        routed_target = create_pool.call_args.args[0]
                        self.assertEqual(
                            routed_target.target_id, explicit_target.target_id
                        )
                        self.assertEqual(routed_target.host, "192.168.99.15")
                        self.assertEqual(routed_target.user, "operator")
                        session.execute_non_query_statement.assert_called_once_with(
                            "CREATE DATABASE root.explicit_test"
                        )
                        manager.close()

        manager.update_registry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
