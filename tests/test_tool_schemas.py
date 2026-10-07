from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path

from fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iotdb_mcp_server.config import Config  # noqa: E402
from iotdb_mcp_server.services.query import register_query_tools  # noqa: E402


class ToolSchemaTest(unittest.TestCase):
    def test_none_defaults_accept_null(self) -> None:
        """A parameter that defaults to None must allow null in its schema.

        Otherwise the schema advertises "default": null on a non-null type,
        and a client that sends that value explicitly is rejected before the
        tool runs.
        """
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
            mcp = FastMCP("test")
            register_query_tools(mcp, config, logging.getLogger(__name__))
            tools = asyncio.run(mcp.list_tools())

        bad = []
        for tool in tools:
            for name, prop in tool.parameters.get("properties", {}).items():
                if "default" in prop and prop["default"] is None:
                    types = [prop.get("type")] + [
                        branch.get("type") for branch in prop.get("anyOf", [])
                    ]
                    if "null" not in types:
                        bad.append(f"{tool.name}.{name}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
