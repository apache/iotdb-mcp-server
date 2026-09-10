from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iotdb_mcp_server.runtime_policy import (  # noqa: E402
    dynamic_policy_snapshot,
    reset_session_policy,
    set_session_policy,
    strict_permission_enforcement,
)


class RuntimePolicyTest(unittest.TestCase):
    def tearDown(self) -> None:
        reset_session_policy()

    def test_targets_json_is_fully_withheld_from_policy_snapshot(self) -> None:
        mcp_env = {
            "TIMESEEK_IOTDB_TARGETS_JSON": (
                '{"targets":{"prod":{"password":"secret",'
                '"last_known_good_credential":{"password":"known-good"}}}}'
            ),
            "IOTDB_ENABLE_WRITE_DML": "false",
        }

        with patch(
            "iotdb_mcp_server.runtime_policy._load_mcp_env",
            return_value=(mcp_env, "/tmp/.mcp.json"),
        ):
            snapshot = dynamic_policy_snapshot()

        self.assertEqual(snapshot["mcp_env"]["TIMESEEK_IOTDB_TARGETS_JSON"], "***")
        self.assertEqual(snapshot["mcp_env"]["IOTDB_ENABLE_WRITE_DML"], "false")
        self.assertNotIn("secret", str(snapshot))
        self.assertNotIn("known-good", str(snapshot))

    def test_readonly_preset_preserves_process_strict_enforcement(self) -> None:
        with (
            patch(
                "iotdb_mcp_server.runtime_policy._load_mcp_env",
                return_value=({}, ""),
            ),
            patch.dict(
                os.environ,
                {"IOTDB_STRICT_PERMISSION_ENFORCEMENT": "true"},
                clear=True,
            ),
        ):
            snapshot = set_session_policy(preset="readonly")

            self.assertTrue(strict_permission_enforcement())
            self.assertNotIn(
                "IOTDB_STRICT_PERMISSION_ENFORCEMENT",
                snapshot["session_policy"],
            )

    def test_replacing_with_preset_preserves_session_enforcement(self) -> None:
        with (
            patch(
                "iotdb_mcp_server.runtime_policy._load_mcp_env",
                return_value=({}, ""),
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            set_session_policy({"TIMESEEK_MCP_PERMISSION_ENFORCEMENT": "strict"})
            snapshot = set_session_policy(preset="readonly", replace=True)

            self.assertTrue(strict_permission_enforcement())
            self.assertEqual(
                snapshot["session_policy"]["TIMESEEK_MCP_PERMISSION_ENFORCEMENT"],
                "strict",
            )


if __name__ == "__main__":
    unittest.main()
