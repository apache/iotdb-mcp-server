from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iotdb_mcp_server.services.udf import _prepare_udf_res  # noqa: E402


class _TreeRecord:
    def get_timestamp(self) -> int:
        return 123456789

    def get_fields(self) -> list[str]:
        return ["0.5"]


class _TreeDataSet:
    def __init__(self) -> None:
        self._returned = False

    def get_column_names(self) -> list[str]:
        return ["Time", "sin(root.sg.d1.s1)"]

    def has_next(self) -> bool:
        return not self._returned

    def next(self) -> _TreeRecord:
        self._returned = True
        return _TreeRecord()


class UdfResultTest(unittest.TestCase):
    def test_tree_udf_result_includes_timestamp_column_value(self) -> None:
        plan = {
            "sql": "SELECT sin(s1) FROM root.sg.d1",
            "dialect": "tree",
            "function_name": "sin",
        }
        session = Mock()

        with tempfile.TemporaryDirectory() as directory:
            response = _prepare_udf_res(
                _TreeDataSet(),
                session,
                "execute_udf_query",
                directory,
                plan,
                owner_session_id="ses_udf",
            )

        payload = json.loads(response[-1].text)["payload"]
        self.assertEqual(payload["columns"], ["Time", "sin(root.sg.d1.s1)"])
        self.assertEqual(payload["rows"], ["123456789,0.5"])
        session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
