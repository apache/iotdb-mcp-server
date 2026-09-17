from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iotdb_mcp_server.runtime_policy import (  # noqa: E402
    dynamic_policy_snapshot,
    reset_session_policy,
    set_session_policy,
    strict_permission_enforcement,
    dynamic_getenv,
    initialize_runtime_policy,
)
from iotdb_mcp_server import runtime_policy as runtime  # noqa: E402
from iotdb_mcp_server.policy_approval import main as administer  # noqa: E402
from iotdb_mcp_server.config import Config  # noqa: E402
from iotdb_mcp_server.services.sql_driver import (
    _assert_sql_driver_permission,
)  # noqa: E402
from iotdb_mcp_server.services.write import _assert_write_permission  # noqa: E402
from iotdb_mcp_server.services.runtime_policy import (
    register_runtime_policy_tools,
)  # noqa: E402


class RuntimePolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch.object(runtime, "_load_mcp_env", return_value=({}, "")))
        for name, value in {
            "_DEPLOYMENT_POLICY": None,
            "_DEPLOYMENT_ENV": {},
            "_DEPLOYMENT_CONFIG_PATH": "",
            "_SESSION_POLICY": {},
            "_POLICY_REVISION": 0,
            "_APPROVAL_STORE": None,
            "_APPROVAL_MODE": "require",
        }.items():
            self.enterContext(patch.object(runtime, name, value))

    def approval_directory(self) -> Path:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        os.environ["IOTDB_SESSION_POLICY_APPROVAL_DIR"] = str(directory)
        return directory

    def decide(self, directory: Path, request: dict, action: str = "approve") -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                administer(
                    [action, request["request_id"], "--directory", str(directory)]
                ),
                0,
            )

    def config(self) -> Config:
        return Config(
            host="127.0.0.1",
            port=6667,
            user="root",
            password="test",
            database="test",
            sql_dialect="tree",
            timezone="UTC",
            export_path="/tmp",
        )

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

    def test_both_enforcement_switches_and_classification_are_admin_only(self) -> None:
        os.environ["IOTDB_STRICT_PERMISSION_ENFORCEMENT"] = "true"
        for key, value in {
            "IOTDB_STRICT_PERMISSION_ENFORCEMENT": False,
            "TIMESEEK_MCP_PERMISSION_ENFORCEMENT": "advisory",
            "IOTDB_SQL_DRIVER_EXTRA_READONLY_PREFIXES": "INSERT INTO",
            "IOTDB_SQL_DRIVER_EXTRA_DDL_PREFIXES": "DELETE FROM",
            "IOTDB_SQL_DRIVER_EXTRA_FULL_PREFIXES": "GRANT",
        }.items():
            with self.subTest(key=key):
                with self.assertRaises(PermissionError):
                    set_session_policy({key: value}, replace=True)
                with self.assertRaises(PermissionError):
                    reset_session_policy([key])
        self.assertTrue(strict_permission_enforcement())
        self.assertEqual(dynamic_policy_snapshot()["session_policy"], {})

    def test_environment_ceiling_is_frozen_and_takes_precedence_over_config(
        self,
    ) -> None:
        os.environ.update(
            {
                "IOTDB_STRICT_PERMISSION_ENFORCEMENT": "true",
                "IOTDB_SQL_DRIVER_MODE": "readonly",
                "IOTDB_ENABLE_WRITE_DML": "false",
            }
        )
        runtime._load_mcp_env.return_value = (
            {
                "IOTDB_SQL_DRIVER_MODE": "full",
                "IOTDB_ENABLE_WRITE_DML": "true",
            },
            "/tmp/.mcp.json",
        )
        initialize_runtime_policy()
        os.environ["IOTDB_SQL_DRIVER_MODE"] = "full"
        os.environ["IOTDB_STRICT_PERMISSION_ENFORCEMENT"] = "false"
        runtime._load_mcp_env.return_value = ({"IOTDB_SQL_DRIVER_MODE": "full"}, "")
        with self.assertRaisesRegex(PermissionError, "ceiling"):
            set_session_policy({"IOTDB_SQL_DRIVER_MODE": "full"})
        snapshot = set_session_policy(preset="full")
        self.assertEqual(
            snapshot["effective_policy"]["IOTDB_SQL_DRIVER_MODE"], "readonly"
        )
        self.assertEqual(dynamic_getenv("IOTDB_ENABLE_WRITE_DML"), "false")
        self.assertTrue(strict_permission_enforcement())
        with self.assertRaises(PermissionError):
            _assert_sql_driver_permission(
                self.config(), "ddl", "readonly", True, "DROP DATABASE"
            )
        with self.assertRaises(PermissionError):
            _assert_write_permission(self.config(), "INSERT")

    def test_preset_intersects_allowlists_and_confirmation_ceiling(self) -> None:
        os.environ["IOTDB_WRITE_ALLOWED_USERS"] = "alice"
        os.environ["IOTDB_REQUIRE_DELETE_CONFIRM"] = "true"
        result = set_session_policy(preset="readonly")
        self.assertTrue(result["applied"])
        self.assertEqual(dynamic_getenv("IOTDB_WRITE_ALLOWED_USERS"), "alice")
        self.assertEqual(dynamic_getenv("IOTDB_ENABLE_MODEL_MANAGEMENT"), "false")
        for policy in (
            {"IOTDB_WRITE_ALLOWED_USERS": "*"},
            {"IOTDB_REQUIRE_DELETE_CONFIRM": False},
        ):
            with self.assertRaises(PermissionError):
                set_session_policy(policy)

    def test_narrowing_applies_and_widening_fails_closed_without_channel(self) -> None:
        result = set_session_policy({"IOTDB_SQL_DRIVER_MODE": "readonly"})
        self.assertTrue(result["applied"])
        for operation in (
            lambda: set_session_policy({"IOTDB_SQL_DRIVER_MODE": "full"}),
            lambda: set_session_policy(preset="full", replace=True),
            lambda: set_session_policy(replace=True),
            lambda: reset_session_policy(),
            lambda: reset_session_policy(["IOTDB_SQL_DRIVER_MODE"]),
        ):
            with self.subTest(operation=operation):
                result = operation()
                self.assertFalse(result["applied"])
                self.assertEqual(result["status"], "approval_unavailable")
                self.assertEqual(dynamic_getenv("IOTDB_SQL_DRIVER_MODE"), "readonly")

    def test_approval_is_exact_one_time_and_precedes_mutation(self) -> None:
        directory = self.approval_directory()
        set_session_policy(preset="readonly")
        result = set_session_policy(preset="full")
        self.assertEqual(result["status"], "approval_required")
        self.assertFalse(result["applied"])
        self.assertEqual(dynamic_getenv("IOTDB_ENABLE_WRITE_DML"), "false")
        self.decide(directory, result)
        approved = set_session_policy(preset="full")
        self.assertTrue(approved["applied"])
        self.assertEqual(dynamic_getenv("IOTDB_ENABLE_WRITE_DML"), "true")
        self.assertEqual(list(directory.iterdir()), [])
        set_session_policy(preset="readonly")
        replay = set_session_policy(preset="full")
        self.assertFalse(replay["applied"])
        self.assertNotEqual(result["request_id"], replay["request_id"])

    def test_reset_and_replace_require_approval_before_restoring_permissions(
        self,
    ) -> None:
        directory = self.approval_directory()
        for operation in (
            reset_session_policy,
            lambda: set_session_policy(replace=True),
        ):
            with self.subTest(operation=operation):
                set_session_policy({"IOTDB_ENABLE_WRITE_DML": False})
                result = operation()
                self.assertFalse(result["applied"])
                self.decide(directory, result)
                self.assertTrue(operation()["applied"])
                self.assertEqual(dynamic_getenv("IOTDB_ENABLE_WRITE_DML"), "true")

    def test_allow_is_operator_only_and_never_bypasses_ceiling(self) -> None:
        os.environ.update(
            {
                "IOTDB_SESSION_POLICY_APPROVAL_MODE": "allow",
                "IOTDB_ENABLE_DATABASE_DDL": "false",
            }
        )
        set_session_policy(preset="readonly")
        self.assertTrue(set_session_policy(preset="full")["applied"])
        with self.assertRaises(PermissionError):
            set_session_policy({"IOTDB_ENABLE_DATABASE_DDL": True})
        for key in (
            "IOTDB_SESSION_POLICY_APPROVAL_MODE",
            "IOTDB_SESSION_POLICY_APPROVAL_DIR",
            "approved",
        ):
            with self.assertRaises(ValueError):
                set_session_policy({key: "allow"})

    def test_approval_mode_is_frozen(self) -> None:
        set_session_policy(preset="readonly")
        os.environ["IOTDB_SESSION_POLICY_APPROVAL_MODE"] = "allow"
        self.assertFalse(reset_session_policy()["applied"])

    def test_rejection_keeps_original_policy(self) -> None:
        directory = self.approval_directory()
        set_session_policy(preset="readonly")
        request = reset_session_policy()
        self.decide(directory, request, "deny")
        result = reset_session_policy()
        self.assertEqual(result["status"], "approval_rejected")
        self.assertFalse(result["applied"])
        self.assertEqual(dynamic_getenv("IOTDB_SQL_DRIVER_MODE"), "readonly")

    def test_expired_approval_cannot_apply(self) -> None:
        directory = self.approval_directory()
        set_session_policy(preset="readonly")
        request = reset_session_policy()
        self.decide(directory, request)
        with patch(
            "iotdb_mcp_server.policy_approval.time.time",
            return_value=request["expires_at"] + 1,
        ):
            result = reset_session_policy()
        self.assertFalse(result["applied"])
        self.assertNotEqual(result["request_id"], request["request_id"])

    def test_changed_revision_invalidates_pending_approval(self) -> None:
        directory = self.approval_directory()
        set_session_policy({"IOTDB_ENABLE_WRITE_DML": False})
        request = reset_session_policy()
        self.decide(directory, request)
        set_session_policy({"IOTDB_ENABLE_DATABASE_DDL": False})
        result = reset_session_policy()
        self.assertFalse(result["applied"])
        self.assertNotEqual(result["request_id"], request["request_id"])

    def test_different_proposal_cannot_reuse_approval(self) -> None:
        directory = self.approval_directory()
        set_session_policy(preset="readonly")
        request = set_session_policy({"IOTDB_ENABLE_WRITE_DML": True})
        self.decide(directory, request)
        result = set_session_policy(preset="full")
        self.assertFalse(result["applied"])
        self.assertNotEqual(result["request_id"], request["request_id"])

    def test_tampered_request_cannot_approve_original_proposal(self) -> None:
        directory = self.approval_directory()
        set_session_policy(preset="readonly")
        request = reset_session_policy()
        path = directory / f"{request['request_id']}.request.json"
        data = json.loads(path.read_text())
        data["changes"] = {}
        path.write_text(json.dumps(data))
        self.decide(directory, request)
        with self.assertRaises(PermissionError):
            reset_session_policy()
        self.assertEqual(dynamic_getenv("IOTDB_SQL_DRIVER_MODE"), "readonly")

    def test_atomic_rejection_and_invalid_values(self) -> None:
        os.environ["IOTDB_ENABLE_DATABASE_DDL"] = "false"
        with self.assertRaises(PermissionError):
            set_session_policy(
                {"IOTDB_ENABLE_WRITE_DML": False, "IOTDB_ENABLE_DATABASE_DDL": True}
            )
        self.assertEqual(dynamic_policy_snapshot()["session_policy"], {})
        for policy in (
            {"IOTDB_ENABLE_WRITE_DML": "maybe"},
            {"IOTDB_SQL_DRIVER_MODE": "unrestricted"},
        ):
            with self.assertRaises(ValueError):
                set_session_policy(policy)
        self.assertEqual(dynamic_policy_snapshot()["session_policy"], {})

    def test_empty_allowlist_denies_root_and_restoring_it_requires_approval(
        self,
    ) -> None:
        os.environ["IOTDB_STRICT_PERMISSION_ENFORCEMENT"] = "true"
        set_session_policy({"IOTDB_WRITE_ALLOWED_USERS": ""})
        self.assertEqual(dynamic_getenv("IOTDB_WRITE_ALLOWED_USERS", "root"), "")
        with self.assertRaises(PermissionError):
            _assert_write_permission(self.config(), "INSERT")
        self.assertFalse(reset_session_policy()["applied"])

    def test_confirmation_and_allowlist_widening_need_approval(self) -> None:
        os.environ["IOTDB_REQUIRE_DELETE_CONFIRM"] = "false"
        for narrow, wider in (
            (
                {"IOTDB_REQUIRE_DELETE_CONFIRM": True},
                {"IOTDB_REQUIRE_DELETE_CONFIRM": False},
            ),
            (
                {"IOTDB_WRITE_ALLOWED_USERS": "alice"},
                {"IOTDB_WRITE_ALLOWED_USERS": "alice,bob"},
            ),
            ({"IOTDB_SQL_DRIVER_MODE": "readonly"}, {"IOTDB_SQL_DRIVER_MODE": "ddl"}),
        ):
            self.assertTrue(set_session_policy(narrow)["applied"])
            self.assertFalse(set_session_policy(wider)["applied"])

    def test_invalid_deployment_fails_initialization(self) -> None:
        os.environ["IOTDB_ENABLE_WRITE_DML"] = "maybe"
        with self.assertRaises(ValueError):
            initialize_runtime_policy()
        self.assertIsNone(runtime._DEPLOYMENT_POLICY)

    def test_restart_invalidates_approval(self) -> None:
        directory = self.approval_directory()
        set_session_policy(preset="readonly")
        request = reset_session_policy()
        self.decide(directory, request)
        runtime._APPROVAL_STORE = runtime.PolicyApprovalStore(directory)
        result = reset_session_policy()
        self.assertFalse(result["applied"])
        self.assertNotEqual(result["request_id"], request["request_id"])

    def test_concurrent_retries_consume_approval_once(self) -> None:
        directory = self.approval_directory()
        set_session_policy(preset="readonly")
        request = reset_session_policy()
        self.decide(directory, request)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: reset_session_policy(), range(2)))
        self.assertTrue(all(result["applied"] for result in results))
        self.assertEqual(dynamic_policy_snapshot()["policy_revision"], 2)
        self.assertEqual(list(directory.iterdir()), [])

    def test_pending_approval_count_is_bounded(self) -> None:
        self.approval_directory()
        set_session_policy({"IOTDB_WRITE_ALLOWED_USERS": "alice"})
        with patch("iotdb_mcp_server.policy_approval._MAX_PENDING", 2):
            for user in ("bob", "carol"):
                self.assertFalse(
                    set_session_policy({"IOTDB_WRITE_ALLOWED_USERS": f"alice,{user}"})[
                        "applied"
                    ]
                )
            with self.assertRaisesRegex(PermissionError, "Too many"):
                set_session_policy({"IOTDB_WRITE_ALLOWED_USERS": "*"})

    def test_approval_directory_and_cli_request_id_are_validated(self) -> None:
        directory = self.approval_directory()
        if os.name == "posix":
            directory.chmod(0o777)
            try:
                with self.assertRaises(PermissionError):
                    initialize_runtime_policy()
            finally:
                directory.chmod(0o700)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            administer(["approve", "../invalid", "--directory", str(directory)])

    def test_real_mcp_tool_approval_flow(self) -> None:
        from fastmcp import Client, FastMCP

        directory = self.approval_directory()
        mcp = FastMCP("policy-test")
        register_runtime_policy_tools(mcp, self.config(), logging.getLogger(__name__))

        async def exercise():
            async with Client(mcp) as client:
                tools = await client.list_tools()
                self.assertEqual(
                    {tool.name for tool in tools},
                    {
                        "get_iotdb_session_policy",
                        "set_iotdb_session_policy",
                        "reset_iotdb_session_policy",
                    },
                )
                setter = next(
                    tool for tool in tools if tool.name == "set_iotdb_session_policy"
                )
                self.assertEqual(
                    set(setter.inputSchema["properties"]),
                    {"policy", "preset", "replace"},
                )
                await client.call_tool(
                    "set_iotdb_session_policy", {"preset": "readonly"}
                )
                response = await client.call_tool("reset_iotdb_session_policy", {})
                pending = json.loads(response.content[0].text)["payload"]
                self.assertFalse(pending["applied"])
                # Exercise the independent administrative process, not a tool or
                # an in-process callback that an agent could supply itself.
                decision = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "iotdb_mcp_server.policy_approval",
                        "approve",
                        pending["request_id"],
                        "--directory",
                        str(directory),
                    ],
                    env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(decision.returncode, 0, decision.stderr)
                response = await client.call_tool("reset_iotdb_session_policy", {})
                self.assertTrue(
                    json.loads(response.content[0].text)["payload"]["applied"]
                )

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
