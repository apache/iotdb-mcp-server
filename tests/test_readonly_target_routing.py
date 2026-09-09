from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iotdb_mcp_server.config import Config  # noqa: E402
from iotdb_mcp_server.session_manager import IoTDBSessionManager  # noqa: E402
from iotdb_mcp_server.services.database import register_database_tools  # noqa: E402
from iotdb_mcp_server.services.metadata import register_metadata_tools  # noqa: E402
from iotdb_mcp_server.services.query import register_query_tools  # noqa: E402
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


class _WrappingMcp:
    """Model FastMCP versions whose decorator replaces the function object."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self):
        def register(function):
            self.tools[function.__name__] = function
            return object()

        return register


class ReadonlyTargetRoutingTest(unittest.TestCase):
    def _exercise_explicit_target(
        self,
        *,
        dialect: str,
        register_tools: Callable[..., None],
        tool_name: str,
        tool_args: tuple[Any, ...],
        format_result_patch: str,
    ) -> None:
        default_target = target_from_mapping(
            {
                "target_id": f"default-{dialect}",
                "host": "192.168.99.20",
                "port": 6667,
                "user": "root",
                "password": "default-secret",
                "database": "default_db",
                "sql_dialect": dialect,
                "verified_at": "2026-07-13T00:00:00+00:00",
            }
        )
        explicit_target = target_from_mapping(
            {
                "target_id": f"explicit-{dialect}",
                "host": "192.168.99.15",
                "port": 6667,
                "user": "operator",
                "password": "explicit-secret",
                "database": "explicit_db",
                "sql_dialect": dialect,
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
        session.execute_query_statement.return_value = Mock()

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
            config.export_path = directory
            config.targets_file = str(targets_file)
            config.targets_file_mtime_ns = targets_file.stat().st_mtime_ns
            mcp = _FakeMcp()
            register_tools(mcp, config, logging.getLogger(__name__))

            pool_factory = (
                "iotdb_mcp_server.session_manager.create_tree_session_pool"
                if dialect == "tree"
                else "iotdb_mcp_server.session_manager.create_table_session_pool"
            )
            selectors = (
                {"target_id": explicit_target.target_id},
                {"target": {"target_id": explicit_target.target_id}},
            )
            with (
                patch(pool_factory, return_value=pool) as create_pool,
                patch(format_result_patch, return_value=[]),
            ):
                for selector in selectors:
                    with self.subTest(
                        dialect=dialect,
                        tool=tool_name,
                        selector=selector,
                    ):
                        create_pool.reset_mock()
                        asyncio.run(mcp.tools[tool_name](*tool_args, **selector))
                        routed_target = create_pool.call_args.args[0]
                        self.assertEqual(
                            routed_target.target_id, explicit_target.target_id
                        )
                        self.assertEqual(routed_target.host, "192.168.99.15")
                        self.assertEqual(routed_target.user, "operator")
                        manager.close()

        manager.update_registry.assert_not_called()

    def test_tree_select_query_keeps_explicit_target(self) -> None:
        self._exercise_explicit_target(
            dialect="tree",
            register_tools=register_query_tools,
            tool_name="select_query",
            tool_args=("SELECT s1 FROM root.device",),
            format_result_patch="iotdb_mcp_server.services.query._prepare_tree_res",
        )

    def test_table_read_query_keeps_explicit_target(self) -> None:
        self._exercise_explicit_target(
            dialect="table",
            register_tools=register_query_tools,
            tool_name="read_query",
            tool_args=("SELECT s1 FROM device",),
            format_result_patch="iotdb_mcp_server.services.query._prepare_table_res",
        )

    def test_list_databases_keeps_explicit_target(self) -> None:
        for dialect in ("tree", "table"):
            self._exercise_explicit_target(
                dialect=dialect,
                register_tools=register_database_tools,
                tool_name="list_databases",
                tool_args=(),
                format_result_patch="iotdb_mcp_server.services.database._format_result",
            )

    def test_metadata_query_keeps_explicit_target(self) -> None:
        for dialect in ("tree", "table"):
            self._exercise_explicit_target(
                dialect=dialect,
                register_tools=register_metadata_tools,
                tool_name="metadata_query",
                tool_args=("SHOW DATABASES",),
                format_result_patch="iotdb_mcp_server.services.metadata._format_result",
            )

    def test_metadata_convenience_tool_uses_undecorated_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Config(
                host="127.0.0.1",
                port=6667,
                user="root",
                password="",
                database="test",
                sql_dialect="table",
                timezone="+00:00",
                export_path=directory,
            )
            mcp = _WrappingMcp()
            pool = Mock()
            session = Mock()
            pool.get_session.return_value = session
            session.execute_query_statement.return_value = Mock()
            register_metadata_tools(mcp, config, logging.getLogger(__name__))

            with patch(
                "iotdb_mcp_server.services.metadata.table_session_pool",
                return_value=(config, pool),
            ), patch(
                "iotdb_mcp_server.services.metadata._format_result",
                return_value=[],
            ):
                response = asyncio.run(mcp.tools["list_tables"]())

        self.assertEqual(response, [])
        session.execute_query_statement.assert_called_once_with("SHOW TABLES")


if __name__ == "__main__":
    unittest.main()
