from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from iotdb_mcp_server.target_cli import (  # noqa: E402
    build_target_cli_command,
    main,
    redact_target_cli_command,
)
from iotdb_mcp_server.target_registry import target_from_mapping  # noqa: E402


class TargetCliTest(unittest.TestCase):
    def test_main_loads_the_persisted_verified_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cli = root / "start-cli.sh"
            cli.write_text("#!/bin/sh\n", encoding="utf-8")
            registry = root / "targets.json"
            registry.write_text(
                json.dumps(
                    {
                        "default_target_id": "cloud",
                        "targets": [
                            {
                                "target_id": "cloud",
                                "host": "192.168.99.20",
                                "port": 6667,
                                "user": "operator",
                                "password": "known-good",
                                "verified_at": "2026-07-13T00:00:00+00:00",
                                "sql_dialect": "tree",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            completed = Mock(returncode=0)

            with patch(
                "iotdb_mcp_server.target_cli.subprocess.run",
                return_value=completed,
            ) as run:
                result = main(
                    [
                        "--targets-file",
                        str(registry),
                        "--cli",
                        str(cli),
                        "--",
                        "-e",
                        "SHOW VERSION",
                    ]
                )

        self.assertEqual(result, 0)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-u") + 1], "operator")
        self.assertEqual(command[command.index("-pw") + 1], "known-good")
        self.assertEqual(command[-2:], ["-e", "SHOW VERSION"])

    def test_uses_verified_target_credential(self) -> None:
        target = target_from_mapping(
            {
                "target_id": "cloud",
                "host": "192.168.99.20",
                "port": 6667,
                "user": "operator",
                "last_known_good_credential": {
                    "user": "operator",
                    "password": "known-good",
                    "last_success_at": "2026-07-13T00:00:00+00:00",
                },
                "verified_at": "2026-07-13T00:00:00+00:00",
                "sql_dialect": "tree",
            }
        )

        command = build_target_cli_command(
            target,
            "/opt/iotdb/sbin/start-cli.sh",
            ["-e", "SHOW VERSION"],
        )

        self.assertEqual(
            command,
            [
                "/opt/iotdb/sbin/start-cli.sh",
                "-h",
                "192.168.99.20",
                "-p",
                "6667",
                "-u",
                "operator",
                "-pw",
                "known-good",
                "-sql_dialect",
                "tree",
                "-e",
                "SHOW VERSION",
            ],
        )
        self.assertNotIn("known-good", redact_target_cli_command(command))

    def test_empty_password_omits_password_argument(self) -> None:
        target = target_from_mapping(
            {
                "target_id": "cloud",
                "host": "192.168.99.20",
                "user": "root",
                "password": "",
                "verified_at": "2026-07-13T00:00:00+00:00",
                "sql_dialect": "tree",
            }
        )

        command = build_target_cli_command(target, "/opt/iotdb/sbin/start-cli.sh")

        self.assertNotIn("-pw", command)

    def test_rejects_target_credential_overrides(self) -> None:
        target = target_from_mapping(
            {
                "target_id": "cloud",
                "password": "known-good",
                "verified_at": "2026-07-13T00:00:00+00:00",
            }
        )

        with self.assertRaisesRegex(ValueError, "loaded from the verified target"):
            build_target_cli_command(
                target,
                "/opt/iotdb/sbin/start-cli.sh",
                ["--", "-pw", "different"],
            )

    def test_table_start_cli_omits_unsupported_database_argument(self) -> None:
        target = target_from_mapping(
            {
                "target_id": "table",
                "password": "known-good",
                "verified_at": "2026-07-13T00:00:00+00:00",
                "sql_dialect": "table",
                "database": "factory",
            }
        )

        command = build_target_cli_command(target, "/opt/iotdb/sbin/start-cli.sh")

        self.assertNotIn("-db", command)
        self.assertEqual(command[-2:], ["-sql_dialect", "table"])

    def test_table_import_data_cli_supplies_database(self) -> None:
        target = target_from_mapping(
            {
                "target_id": "table",
                "password": "known-good",
                "verified_at": "2026-07-13T00:00:00+00:00",
                "sql_dialect": "table",
                "database": "factory",
            }
        )

        command = build_target_cli_command(
            target,
            "/opt/iotdb/tools/import-data.sh",
            ["-ft", "csv", "-s", "/tmp/input.csv"],
        )

        self.assertEqual(
            command[command.index("-sql_dialect") : command.index("-ft")],
            ["-sql_dialect", "table", "-db", "factory"],
        )


if __name__ == "__main__":
    unittest.main()
