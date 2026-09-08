from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence

from iotdb_mcp_server.target_registry import IoTDBTarget, IoTDBTargetRegistry

_TARGET_CONTROLLED_OPTIONS = {
    "-h",
    "--host",
    "-p",
    "--port",
    "-u",
    "--user",
    "--username",
    "-pw",
    "--password",
    "-sql_dialect",
    "--sql-dialect",
    "-db",
    "--database",
}
_DATABASE_OPTION_CLI_NAMES = {"import-data.sh", "import-data.bat"}


def _target_cli_path(
    target: IoTDBTarget,
    explicit_cli: str | None,
    env: Mapping[str, str],
) -> Path:
    configured = explicit_cli or env.get("TIMESEEK_IOTDB_CLI", "")
    if configured:
        return Path(configured).expanduser()

    iotdb_home = target.iotdb_home or env.get("TIMESEEK_IOTDB_HOME", "")
    if not iotdb_home:
        raise ValueError(
            "IoTDB CLI is not configured. Set target.iotdb_home, "
            "TIMESEEK_IOTDB_HOME, or pass --cli."
        )
    return Path(iotdb_home).expanduser() / "sbin" / "start-cli.sh"


def _validate_passthrough_args(cli_args: Sequence[str]) -> None:
    for argument in cli_args:
        option = argument.split("=", 1)[0]
        if option in _TARGET_CONTROLLED_OPTIONS:
            raise ValueError(
                f"Do not pass {option} to iotdb-target-cli. Endpoint, credentials, "
                "and dialect are loaded from the verified target; database options "
                "are injected only for CLI tools that support them."
            )


def cli_supports_database_option(cli_path: str | Path) -> bool:
    return Path(cli_path).name.lower() in _DATABASE_OPTION_CLI_NAMES


def build_target_cli_command(
    target: IoTDBTarget,
    cli_path: str | Path,
    cli_args: Sequence[str] = (),
) -> list[str]:
    passthrough = list(cli_args)
    if passthrough[:1] == ["--"]:
        passthrough = passthrough[1:]
    _validate_passthrough_args(passthrough)

    command = [
        str(cli_path),
        "-h",
        target.host,
        "-p",
        str(target.port),
        "-u",
        target.user,
    ]
    if target.password:
        command.extend(["-pw", target.password])
    command.extend(["-sql_dialect", target.sql_dialect])
    if (
        target.sql_dialect == "table"
        and target.database
        and cli_supports_database_option(cli_path)
    ):
        command.extend(["-db", target.database])
    command.extend(passthrough)
    return command


def redact_target_cli_command(command: Sequence[str]) -> list[str]:
    redacted = list(command)
    try:
        password_index = redacted.index("-pw") + 1
    except ValueError:
        return redacted
    if password_index < len(redacted):
        redacted[password_index] = "<redacted>"
    return redacted


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the IoTDB Java CLI with endpoint and credentials loaded from a "
            "verified TimeSeek target."
        )
    )
    parser.add_argument(
        "--targets-file",
        help="Target registry JSON. Defaults to TIMESEEK_IOTDB_TARGETS_FILE.",
    )
    parser.add_argument(
        "--target-id",
        help="Verified target id. Defaults to the registry default target.",
    )
    parser.add_argument(
        "--cli",
        help=(
            "Path to start-cli.sh. Defaults to TIMESEEK_IOTDB_CLI or "
            "<target.iotdb_home>/sbin/start-cli.sh."
        ),
    )
    parser.add_argument(
        "--print-command",
        action="store_true",
        help="Print a shell-escaped command with the password redacted, then exit.",
    )
    parser.add_argument(
        "cli_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed to start-cli.sh after an optional -- separator.",
    )
    return parser


def _run(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    env = os.environ
    targets_file = args.targets_file or env.get("TIMESEEK_IOTDB_TARGETS_FILE")
    registry = IoTDBTargetRegistry.from_env(
        env=env,
        targets_file=targets_file,
    )
    target = registry.resolve(args.target_id)
    cli_path = _target_cli_path(target, args.cli, env)
    if not cli_path.is_file():
        raise FileNotFoundError(f"IoTDB CLI does not exist: {cli_path}")
    command = build_target_cli_command(target, cli_path, args.cli_args)

    if args.print_command:
        print(shlex.join(redact_target_cli_command(command)))
        return 0

    completed = subprocess.run(command, check=False)
    return completed.returncode


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _run(argv)
    except (OSError, KeyError, ValueError) as exc:
        print(f"iotdb-target-cli: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
