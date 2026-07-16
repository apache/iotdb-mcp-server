from __future__ import annotations

import sys
import unittest
import json
import logging
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iotdb_mcp_server.config import Config  # noqa: E402
from iotdb_mcp_server.result_store import FileResultStore  # noqa: E402
from iotdb_mcp_server.result_store import StoredCsvResult  # noqa: E402
from iotdb_mcp_server.services.sql_driver import (  # noqa: E402
    _render_sql_template,
    _resolve_batch_sqls,
    _stored_result_payload,
    register_sql_driver_tools,
)


class FakeMCP:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class FakeRecord:
    def __init__(self, fields: list[str]) -> None:
        self._fields = fields

    def get_fields(self) -> list[str]:
        return self._fields


class FakeDataSet:
    def __init__(self, columns: list[str], rows: list[list[str]]) -> None:
        self._columns = columns
        self._rows = rows
        self._index = 0

    def get_column_names(self) -> list[str]:
        return self._columns

    def has_next(self) -> bool:
        return self._index < len(self._rows)

    def next(self) -> FakeRecord:
        row = self._rows[self._index]
        self._index += 1
        return FakeRecord(row)


class FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def execute_query_statement(self, sql: str) -> FakeDataSet:
        return FakeDataSet(
            ["timeseries"],
            [[f"{sql}:row0"], [f"{sql}:row1"], [f"{sql}:row2"]],
        )

    def close(self) -> None:
        self.closed = True


class SlowSession(FakeSession):
    def execute_query_statement(self, sql: str) -> FakeDataSet:
        time.sleep(0.05)
        return super().execute_query_statement(sql)


class FakePool:
    def __init__(self, session_cls=FakeSession) -> None:
        self.session_cls = session_cls

    def get_session(self) -> FakeSession:
        return self.session_cls()


class SqlDriverBatchTest(unittest.TestCase):
    def test_resolve_direct_sqls_normalizes_single_statements(self) -> None:
        statements = _resolve_batch_sqls(
            [" SHOW TIMESERIES root.** LIMIT 1; ", "SELECT s1 FROM root.sg.d1"],
            None,
            None,
        )

        self.assertEqual(
            [item["sql"] for item in statements],
            ["SHOW TIMESERIES root.** LIMIT 1", "SELECT s1 FROM root.sg.d1"],
        )
        self.assertEqual([item["index"] for item in statements], [0, 1])

    def test_resolve_direct_sqls_rejects_multi_statement(self) -> None:
        with self.assertRaisesRegex(ValueError, "single SQL statement"):
            _resolve_batch_sqls(["SELECT 1; SELECT 2"], None, None)

    def test_render_template_supports_literals_paths_and_identifiers(self) -> None:
        sql = _render_sql_template(
            "SELECT {{measurement:identifier}} FROM {{device:path}} "
            "WHERE time >= {{start}} AND time < {{end}}",
            {
                "measurement": "s1",
                "device": "root.BFGDGS.DATA.d1",
                "start": 0,
                "end": "2026-07-06T00:00:00",
            },
        )

        self.assertEqual(
            sql,
            "SELECT s1 FROM root.BFGDGS.DATA.d1 "
            "WHERE time >= 0 AND time < '2026-07-06T00:00:00'",
        )

    def test_render_template_rejects_unsafe_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsafe IoTDB path"):
            _render_sql_template(
                "SELECT s1 FROM {{device:path}}",
                {"device": "root.sg.d1; DELETE TIMESERIES root.**"},
            )

    def test_resolve_template_param_sets(self) -> None:
        statements = _resolve_batch_sqls(
            None,
            "SHOW TIMESERIES {{path:path}} LIMIT {{limit}}",
            [
                {"path": "root.BFGDGS.DATA.**", "limit": 1},
                {"path": "root.BFGDGS.DATA.device1.*", "limit": 2},
            ],
        )

        self.assertEqual(
            [item["sql"] for item in statements],
            [
                "SHOW TIMESERIES root.BFGDGS.DATA.** LIMIT 1",
                "SHOW TIMESERIES root.BFGDGS.DATA.device1.* LIMIT 2",
            ],
        )

    def test_stored_result_payload_is_pageable(self) -> None:
        stored = StoredCsvResult(
            result_id="res_20260706T000000Z_0123456789abcdef",
            owner_session_id="ses_batch",
            columns=["Time", "root.sg.d1.s1"],
            preview_rows=["0,1"],
            row_count=3,
            byte_count=12,
            page_size_rows=500,
            manifest_path="/tmp/result_store/manifest.json",
            shard_count=1,
        )

        payload = _stored_result_payload(
            index=0,
            sql="SELECT s1 FROM root.sg.d1",
            stored=stored,
            columns=stored.columns,
            duration_ms=12,
        )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result_id"], stored.result_id)
        self.assertEqual(payload["next_cursor"], "1")
        self.assertEqual(payload["result_store"]["page_tool"], "read_result_page")

    def test_sql_executor_batch_tool_writes_result_store_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                host="127.0.0.1",
                port=6667,
                user="root",
                password="",
                database="",
                sql_dialect="tree",
                timezone="Asia/Shanghai",
                export_path=tmp,
            )
            fake_mcp = FakeMCP()
            register_sql_driver_tools(
                fake_mcp,
                cfg,
                logging.getLogger("test_sql_executor_batch"),
            )

            with patch(
                "iotdb_mcp_server.services.sql_driver.tree_session_pool",
                return_value=(cfg, FakePool()),
            ), patch(
                "iotdb_mcp_server.services.target_selection._probe_ainode_availability",
                return_value={
                    "ainode_available": "unknown",
                    "ainode_availability_source": {"method": "test", "ok": False},
                },
            ):
                import asyncio

                response = asyncio.run(
                    fake_mcp.tools["sql_executor_batch"](
                        sqls=[
                            "SHOW TIMESERIES root.sg.** LIMIT 2",
                            "SHOW DEVICES root.sg.** LIMIT 2",
                        ],
                        max_concurrency=2,
                        worker_pool_size=2,
                        owner_session_id="ses_tool",
                        max_inline_rows=1,
                        page_size_rows=2,
                    )
                )

            obj = json.loads(response[-1].text)
            payload = obj["payload"]
            self.assertEqual(payload["succeeded"], 2)
            self.assertEqual(payload["failed"], 0)
            self.assertEqual(payload["worker_pool_size"], 2)
            self.assertEqual(payload["execution_controls"]["per_item_timeout_ms"], 60000)
            self.assertEqual(payload["execution_controls"]["max_result_rows_per_item"], 10000)
            first = payload["results"][0]
            self.assertEqual(first["inline_row_count"], 1)
            self.assertTrue(first["inline_truncated"])

            store = FileResultStore(Path(tmp) / "result_store")
            page = store.read_page(
                first["result_id"],
                offset=1,
                limit=2,
                owner_session_id="ses_tool",
            )
            self.assertEqual(page["returned_rows"], 2)
            self.assertFalse(page["has_more"])

    def test_sql_executor_batch_enforces_per_item_row_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                host="127.0.0.1",
                port=6667,
                user="root",
                password="",
                database="",
                sql_dialect="tree",
                timezone="Asia/Shanghai",
                export_path=tmp,
            )
            fake_mcp = FakeMCP()
            register_sql_driver_tools(
                fake_mcp,
                cfg,
                logging.getLogger("test_sql_executor_batch_quota"),
            )

            with patch(
                "iotdb_mcp_server.services.sql_driver.tree_session_pool",
                return_value=(cfg, FakePool()),
            ), patch(
                "iotdb_mcp_server.services.target_selection._probe_ainode_availability",
                return_value={
                    "ainode_available": "unknown",
                    "ainode_availability_source": {"method": "test", "ok": False},
                },
            ):
                import asyncio

                response = asyncio.run(
                    fake_mcp.tools["sql_executor_batch"](
                        sqls=["SHOW TIMESERIES root.sg.** LIMIT 3"],
                        max_result_rows_per_item=2,
                        owner_session_id="ses_quota",
                    )
                )

            payload = json.loads(response[-1].text)["payload"]
            self.assertEqual(payload["succeeded"], 0)
            self.assertEqual(payload["failed"], 1)
            self.assertEqual(payload["results"][0]["error_type"], "BatchQuotaExceeded")
            self.assertIn("row quota exceeded", payload["results"][0]["error"])

    def test_sql_executor_batch_enforces_per_item_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                host="127.0.0.1",
                port=6667,
                user="root",
                password="",
                database="",
                sql_dialect="tree",
                timezone="Asia/Shanghai",
                export_path=tmp,
            )
            fake_mcp = FakeMCP()
            register_sql_driver_tools(
                fake_mcp,
                cfg,
                logging.getLogger("test_sql_executor_batch_timeout"),
            )

            with patch(
                "iotdb_mcp_server.services.sql_driver.tree_session_pool",
                return_value=(cfg, FakePool(SlowSession)),
            ), patch(
                "iotdb_mcp_server.services.target_selection._probe_ainode_availability",
                return_value={
                    "ainode_available": "unknown",
                    "ainode_availability_source": {"method": "test", "ok": False},
                },
            ):
                import asyncio

                response = asyncio.run(
                    fake_mcp.tools["sql_executor_batch"](
                        sqls=["SHOW TIMESERIES root.sg.** LIMIT 3"],
                        per_item_timeout_ms=1,
                        owner_session_id="ses_timeout",
                    )
                )

            payload = json.loads(response[-1].text)["payload"]
            self.assertEqual(payload["succeeded"], 0)
            self.assertEqual(payload["failed"], 1)
            self.assertEqual(payload["results"][0]["error_type"], "BatchItemTimeout")
            self.assertIn("timed out", payload["results"][0]["error"])

    def test_sql_executor_batch_enforces_total_row_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(
                host="127.0.0.1",
                port=6667,
                user="root",
                password="",
                database="",
                sql_dialect="tree",
                timezone="Asia/Shanghai",
                export_path=tmp,
            )
            fake_mcp = FakeMCP()
            register_sql_driver_tools(
                fake_mcp,
                cfg,
                logging.getLogger("test_sql_executor_batch_total_quota"),
            )

            with patch(
                "iotdb_mcp_server.services.sql_driver.tree_session_pool",
                return_value=(cfg, FakePool()),
            ), patch(
                "iotdb_mcp_server.services.target_selection._probe_ainode_availability",
                return_value={
                    "ainode_available": "unknown",
                    "ainode_availability_source": {"method": "test", "ok": False},
                },
            ):
                import asyncio

                response = asyncio.run(
                    fake_mcp.tools["sql_executor_batch"](
                        sqls=[
                            "SHOW TIMESERIES root.sg.** LIMIT 3",
                            "SHOW DEVICES root.sg.** LIMIT 3",
                        ],
                        max_concurrency=1,
                        max_batch_result_rows=4,
                        owner_session_id="ses_total_quota",
                    )
                )

            payload = json.loads(response[-1].text)["payload"]
            self.assertEqual(payload["succeeded"], 1)
            self.assertEqual(payload["failed"], 1)
            self.assertEqual(payload["results"][1]["error_type"], "BatchQuotaExceeded")
            self.assertIn("batch row quota exceeded", payload["results"][1]["error"])


if __name__ == "__main__":
    unittest.main()
