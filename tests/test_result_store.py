from __future__ import annotations

import json
import logging
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iotdb_mcp_server.config import Config  # noqa: E402
from iotdb_mcp_server.result_store import (
    FileResultStore,
    ResultStoreSettings,
    result_store_from_export_path,
)  # noqa: E402
from iotdb_mcp_server.services.json_response import (
    csv_result_payload_response,
)  # noqa: E402
from iotdb_mcp_server.services.results import register_result_store_tools  # noqa: E402


class FakeMCP:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class FileResultStoreTest(unittest.TestCase):
    def test_failed_manifest_write_removes_partial_result_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileResultStore(
                tmp,
                ResultStoreSettings(
                    preview_rows=2,
                    page_size_rows=2,
                    max_page_rows=10,
                    shard_rows=10,
                    shard_bytes=1024,
                ),
            )

            with patch.object(
                Path,
                "write_text",
                side_effect=OSError("disk full"),
            ), self.assertRaisesRegex(OSError, "disk full"):
                store.write_csv_result(
                    tool="select_query",
                    columns=["Time", "root.sg.d.s0"],
                    rows=["0,0"],
                    owner_session_id="ses_manifest_failure",
                )

            session_dir = Path(tmp) / "sessions" / "ses_manifest_failure"
            self.assertEqual(list(session_dir.glob("res_*")), [])

    def test_failed_row_iteration_removes_partial_result_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileResultStore(
                tmp,
                ResultStoreSettings(
                    preview_rows=2,
                    page_size_rows=2,
                    max_page_rows=10,
                    shard_rows=10,
                    shard_bytes=1024,
                ),
            )

            def failing_rows():
                yield "0,0"
                raise RuntimeError("row generation failed")

            with self.assertRaisesRegex(RuntimeError, "row generation failed"):
                store.write_csv_result(
                    tool="select_query",
                    columns=["Time", "root.sg.d.s0"],
                    rows=failing_rows(),
                    owner_session_id="ses_failure",
                )

            session_dir = Path(tmp) / "sessions" / "ses_failure"
            self.assertEqual(list(session_dir.glob("res_*")), [])
            self.assertEqual(
                store.list_results("ses_failure", limit=10)["result_count"],
                0,
            )

    def test_write_and_read_pages_across_shards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileResultStore(
                tmp,
                ResultStoreSettings(
                    preview_rows=2,
                    page_size_rows=2,
                    max_page_rows=10,
                    shard_rows=2,
                    shard_bytes=1024,
                ),
            )
            stored = store.write_csv_result(
                tool="select_query",
                columns=["Time", "root.sg.d.s0"],
                rows=[f"{index},{index * 10}" for index in range(5)],
                source={"query_sql": "SELECT s0 FROM root.sg.d"},
                owner_session_id="ses_test",
            )

            self.assertEqual(stored.row_count, 5)
            self.assertEqual(stored.owner_session_id, "ses_test")
            self.assertEqual(stored.preview_rows, ["0,0", "1,10"])
            self.assertEqual(stored.shard_count, 3)
            self.assertIn("/sessions/ses_test/", stored.manifest_path)

            first = store.read_page(
                stored.result_id,
                offset=0,
                limit=2,
                owner_session_id="ses_test",
            )
            self.assertEqual(first["rows"], ["0,0", "1,10"])
            self.assertEqual(first["owner_session_id"], "ses_test")
            self.assertEqual(first["next_cursor"], "2")
            self.assertTrue(first["has_more"])

            second = store.read_page(
                stored.result_id,
                offset=int(first["next_cursor"]),
                limit=3,
                owner_session_id="ses_test",
            )
            self.assertEqual(second["rows"], ["2,20", "3,30", "4,40"])
            self.assertIsNone(second["next_cursor"])
            self.assertFalse(second["has_more"])

    def test_csv_result_payload_response_returns_preview_and_result_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileResultStore(
                tmp,
                ResultStoreSettings(
                    preview_rows=2,
                    page_size_rows=2,
                    max_page_rows=10,
                    shard_rows=10,
                    shard_bytes=1024,
                ),
            )
            response = csv_result_payload_response(
                "select_query",
                ["Time", "root.sg.d.s0"],
                [f"{index},{index * 10}" for index in range(4)],
                result_store=store,
                source={"query_sql": "SELECT s0 FROM root.sg.d"},
                owner_session_id="ses_payload",
            )

            self.assertEqual(len(response), 1)
            obj = json.loads(response[0].text)
            payload = obj["payload"]
            self.assertEqual(payload["row_count"], 4)
            self.assertEqual(payload["inline_row_count"], 2)
            self.assertTrue(payload["inline_truncated"])
            self.assertEqual(payload["rows"], ["0,0", "1,10"])
            self.assertEqual(payload["next_cursor"], "2")
            self.assertEqual(payload["owner_session_id"], "ses_payload")
            self.assertEqual(payload["result_store"]["owner_session_id"], "ses_payload")
            self.assertEqual(payload["result_store"]["page_tool"], "read_result_page")

            page = store.read_page(
                payload["result_id"],
                offset=2,
                limit=10,
                owner_session_id="ses_payload",
            )
            self.assertEqual(page["rows"], ["2,20", "3,30"])

    def test_empty_result_is_pageable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileResultStore(
                tmp,
                ResultStoreSettings(
                    preview_rows=2,
                    page_size_rows=2,
                    max_page_rows=10,
                    shard_rows=10,
                    shard_bytes=1024,
                ),
            )
            stored = store.write_csv_result(
                tool="select_query",
                columns=["Time", "root.sg.d.s0"],
                rows=[],
            )

            self.assertEqual(stored.row_count, 0)
            page = store.read_page(stored.result_id)
            self.assertEqual(page["rows"], [])
            self.assertEqual(page["owner_session_id"], "standalone")
            self.assertIsNone(page["next_cursor"])
            self.assertFalse(page["has_more"])

    def test_read_pages_returns_multiple_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileResultStore(
                tmp,
                ResultStoreSettings(
                    preview_rows=1,
                    page_size_rows=2,
                    max_page_rows=10,
                    shard_rows=2,
                    shard_bytes=1024,
                ),
            )
            first = store.write_csv_result(
                tool="select_query",
                columns=["Time", "root.sg.d.s0"],
                rows=[f"{index},{index * 10}" for index in range(4)],
                owner_session_id="ses_pages",
            )
            second = store.write_csv_result(
                tool="select_query",
                columns=["Time", "root.sg.d.s1"],
                rows=[f"{index},{index * 100}" for index in range(3)],
                owner_session_id="ses_pages",
            )

            payload = store.read_pages(
                [
                    {"result_id": first.result_id, "limit": 2},
                    {"result_id": second.result_id, "cursor": "1", "limit": 2},
                ],
                owner_session_id="ses_pages",
                max_total_rows=10,
            )

            self.assertEqual(payload["succeeded"], 2)
            self.assertEqual(payload["failed"], 0)
            self.assertEqual(payload["returned_rows"], 4)
            self.assertEqual(payload["pages"][0]["rows"], ["0,0", "1,10"])
            self.assertEqual(payload["pages"][0]["next_cursor"], "2")
            self.assertEqual(payload["pages"][1]["rows"], ["1,100", "2,200"])
            self.assertFalse(payload["pages"][1]["has_more"])

    def test_read_pages_returns_item_errors_and_skips_after_abort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileResultStore(
                tmp,
                ResultStoreSettings(
                    preview_rows=1,
                    page_size_rows=2,
                    max_page_rows=10,
                    shard_rows=10,
                    shard_bytes=1024,
                ),
            )
            live = store.write_csv_result(
                tool="select_query",
                columns=["Time", "root.sg.d.s0"],
                rows=["0,0"],
                owner_session_id="ses_pages_error",
            )
            missing = "res_20260706T000000Z_0123456789abcdef"

            payload = store.read_pages(
                [
                    {"result_id": missing},
                    {"result_id": live.result_id},
                ],
                owner_session_id="ses_pages_error",
                continue_on_error=False,
            )

            self.assertEqual(payload["succeeded"], 0)
            self.assertEqual(payload["failed"], 1)
            self.assertEqual(payload["skipped"], 1)
            self.assertEqual(payload["pages"][0]["status"], "error")
            self.assertEqual(payload["pages"][1]["status"], "skipped")

    def test_read_result_pages_tool_returns_text_for_success_pages(self) -> None:
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
            store = result_store_from_export_path(tmp)
            stored = store.write_csv_result(
                tool="select_query",
                columns=["Time", "root.sg.d.s0"],
                rows=["0,0", "1,10", "2,20"],
                owner_session_id="ses_tool_pages",
            )
            fake_mcp = FakeMCP()
            register_result_store_tools(
                fake_mcp,
                cfg,
                logging.getLogger("test_read_result_pages_tool"),
            )

            import asyncio

            response = asyncio.run(
                fake_mcp.tools["read_result_pages"](
                    pages=[
                        {"result_id": stored.result_id, "limit": 2},
                        {"result_id": stored.result_id, "cursor": "2", "limit": 2},
                    ],
                    owner_session_id="ses_tool_pages",
                    default_limit=2,
                )
            )

            payload = json.loads(response[-1].text)["payload"]
            self.assertEqual(payload["succeeded"], 2)
            self.assertEqual(payload["returned_rows"], 3)
            self.assertEqual(payload["pages"][0]["inline_row_count"], 2)
            self.assertTrue(payload["pages"][0]["inline_truncated"])
            self.assertIn("Time,root.sg.d.s0\n0,0\n1,10", payload["pages"][0]["text"])

    def test_session_lru_quota_evicts_oldest_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileResultStore(
                tmp,
                ResultStoreSettings(
                    preview_rows=1,
                    page_size_rows=2,
                    max_page_rows=10,
                    shard_rows=10,
                    shard_bytes=1024,
                    ttl_seconds=0,
                    max_session_results=2,
                    max_session_bytes=1024 * 1024,
                    max_global_results=100,
                    max_global_bytes=1024 * 1024,
                ),
            )
            first = store.write_csv_result(
                tool="select_query",
                columns=["Time", "root.sg.d.s0"],
                rows=["0,0"],
                owner_session_id="ses_quota",
            )
            second = store.write_csv_result(
                tool="select_query",
                columns=["Time", "root.sg.d.s0"],
                rows=["1,10"],
                owner_session_id="ses_quota",
            )
            third = store.write_csv_result(
                tool="select_query",
                columns=["Time", "root.sg.d.s0"],
                rows=["2,20"],
                owner_session_id="ses_quota",
            )

            listing = store.list_results("ses_quota", limit=10)
            result_ids = {item["result_id"] for item in listing["results"]}
            self.assertEqual(listing["result_count"], 2)
            self.assertNotIn(first.result_id, result_ids)
            self.assertIn(second.result_id, result_ids)
            self.assertIn(third.result_id, result_ids)

    def test_ttl_cleanup_removes_expired_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileResultStore(
                tmp,
                ResultStoreSettings(
                    preview_rows=1,
                    page_size_rows=2,
                    max_page_rows=10,
                    shard_rows=10,
                    shard_bytes=1024,
                    ttl_seconds=0,
                    max_session_results=100,
                    max_session_bytes=1024 * 1024,
                    max_global_results=100,
                    max_global_bytes=1024 * 1024,
                ),
            )
            expired = store.write_csv_result(
                tool="select_query",
                columns=["Time", "root.sg.d.s0"],
                rows=["0,0"],
                owner_session_id="ses_ttl",
            )
            live = store.write_csv_result(
                tool="select_query",
                columns=["Time", "root.sg.d.s0"],
                rows=["1,10"],
                owner_session_id="ses_ttl",
            )

            manifest_path = Path(expired.manifest_path)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["last_accessed_at"] = (
                datetime.now(timezone.utc) - timedelta(seconds=30)
            ).isoformat()
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            summary = store.cleanup(owner_session_id="ses_ttl", ttl_seconds=1)
            result_ids = {
                item["result_id"]
                for item in store.list_results("ses_ttl", limit=10)["results"]
            }
            self.assertEqual(summary["deleted_results"], 1)
            self.assertNotIn(expired.result_id, result_ids)
            self.assertIn(live.result_id, result_ids)

    def test_single_oversized_result_is_retained_for_paging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileResultStore(
                tmp,
                ResultStoreSettings(
                    preview_rows=1,
                    page_size_rows=2,
                    max_page_rows=10,
                    shard_rows=10,
                    shard_bytes=1024,
                    ttl_seconds=0,
                    max_session_results=100,
                    max_session_bytes=1,
                    max_global_results=100,
                    max_global_bytes=1,
                ),
            )
            stored = store.write_csv_result(
                tool="select_query",
                columns=["Time", "root.sg.d.s0"],
                rows=["0," + "x" * 200],
                owner_session_id="ses_oversized",
            )

            page = store.read_page(
                stored.result_id,
                owner_session_id="ses_oversized",
            )
            self.assertEqual(page["returned_rows"], 1)
            self.assertGreater(
                store.list_results("ses_oversized")["storage_bytes"],
                1,
            )


if __name__ == "__main__":
    unittest.main()
