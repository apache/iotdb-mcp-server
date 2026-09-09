# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#

import argparse
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any

from iotdb_mcp_server.target_registry import IoTDBTarget
from iotdb_mcp_server.target_registry import IoTDBTargetRegistry
from iotdb_mcp_server.target_registry import target_from_mapping


def env_or_default(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value


def _argument_or_env(value: Any, name: str) -> Any:
    """Return an explicit CLI value, otherwise the matching environment value."""
    return value if value is not None else os.getenv(name)


def default_export_path() -> str:
    explicit = os.getenv("IOTDB_EXPORT_PATH")
    if explicit:
        return explicit

    workspace = os.getenv("TIMESEEK_WORKSPACE_ROOT") or os.getenv(
        "TIMESEEK_EXPERIMENT_WORKSPACE_ROOT"
    )
    if workspace:
        return str(
            Path(workspace).expanduser()
            / "auto_rule"
            / ".tmp"
            / "iotdb_exports"
        )

    return "/tmp"


@dataclass
class Config:
    """
    Configuration for the IoTDB mcp server.
    """

    host: str
    """
    IoTDB host
    """

    port: int
    """
    IoTDB port
    """

    user: str
    """
    IoTDB username
    """

    password: str = field(repr=False)
    """
    IoTDB password
    """

    database: str
    """
    IoTDB database/session scope.
    Table dialect: current database name.
    Tree dialect: optional root scope hint; tree SQL still uses explicit root paths in queries.
    """

    sql_dialect: str
    """
    SQL dialect: tree or table
    """

    timezone: str
    """
    IoTDB session timezone.
    """
    
    export_path: str
    """
    Path for exporting query results
    """

    target_id: str = "default"
    display_name: str = "Default IoTDB"
    target_kind: str = "local"
    node_urls: tuple[str, ...] = ()
    iotdb_home: str = ""
    use_ssl: bool = False
    ca_certs: str = ""
    connection_timeout_in_ms: int | None = None
    enable_redirection: bool = True
    enable_compression: bool = False
    fetch_size: int = 1024
    max_retry: int = 3
    max_pool_size: int = 100
    tree_wait_timeout_in_ms: int = 5000
    table_wait_timeout_in_ms: int = 10000
    target_fingerprint: str = ""
    policy: dict[str, str] = field(default_factory=dict)
    last_known_good_credential: dict[str, object] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    verified_at: str = ""
    target_registry: IoTDBTargetRegistry | None = field(default=None, repr=False, compare=False)
    session_manager: Any | None = field(default=None, repr=False, compare=False)
    targets_file: str = ""
    targets_json: str = field(default="", repr=False, compare=False)
    targets_file_mtime_ns: int | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_target(
        cls,
        target: IoTDBTarget,
        target_registry: IoTDBTargetRegistry | None = None,
        session_manager: Any | None = None,
    ) -> "Config":
        return cls(
            host=target.host,
            port=target.port,
            user=target.user,
            password=target.password,
            database=target.database,
            sql_dialect=target.sql_dialect,
            timezone=target.timezone,
            export_path=target.export_path,
            target_id=target.target_id,
            display_name=target.display_name,
            target_kind=target.kind,
            node_urls=target.node_urls,
            iotdb_home=target.iotdb_home,
            use_ssl=target.use_ssl,
            ca_certs=target.ca_certs,
            connection_timeout_in_ms=target.connection_timeout_in_ms,
            enable_redirection=target.enable_redirection,
            enable_compression=target.enable_compression,
            fetch_size=target.fetch_size,
            max_retry=target.max_retry,
            max_pool_size=target.max_pool_size,
            tree_wait_timeout_in_ms=target.tree_wait_timeout_in_ms,
            table_wait_timeout_in_ms=target.table_wait_timeout_in_ms,
            target_fingerprint=target.fingerprint(),
            policy={str(key): str(value) for key, value in target.policy.items()},
            last_known_good_credential=dict(target.last_known_good_credential),
            verified_at=target.verified_at,
            target_registry=target_registry,
            session_manager=session_manager,
        )

    def to_target(self) -> IoTDBTarget:
        return IoTDBTarget(
            target_id=self.target_id,
            display_name=self.display_name,
            kind=self.target_kind,
            host=self.host,
            port=self.port,
            node_urls=tuple(self.node_urls) or (f"{self.host}:{self.port}",),
            user=self.user,
            password=self.password,
            database=self.database,
            sql_dialect=self.sql_dialect,
            timezone=self.timezone,
            export_path=self.export_path,
            iotdb_home=self.iotdb_home,
            use_ssl=self.use_ssl,
            ca_certs=self.ca_certs,
            connection_timeout_in_ms=self.connection_timeout_in_ms,
            enable_redirection=self.enable_redirection,
            enable_compression=self.enable_compression,
            fetch_size=self.fetch_size,
            max_retry=self.max_retry,
            max_pool_size=self.max_pool_size,
            tree_wait_timeout_in_ms=self.tree_wait_timeout_in_ms,
            table_wait_timeout_in_ms=self.table_wait_timeout_in_ms,
            policy=dict(self.policy),
            last_known_good_credential=dict(self.last_known_good_credential),
            verified_at=self.verified_at,
        )

    def safe_dict(self) -> dict[str, object]:
        target = self.to_target()
        safe = target.as_dict(include_secret=False)
        safe["password"] = "***" if self.password else ""
        return safe

    @staticmethod
    def from_env_arguments() -> "Config":
        """
        Parse command line arguments.
        """
        parser = argparse.ArgumentParser(description="IoTDB MCP Server")

        parser.add_argument("--target-id", type=str, default=None, help="Named IoTDB target id")
        parser.add_argument(
            "--targets-file",
            type=str,
            default=None,
            help="JSON IoTDB target registry file",
        )
        parser.add_argument("--host", type=str, help="IoTDB host", default=None)

        parser.add_argument(
            "--port",
            type=int,
            help="IoTDB MySQL protocol port",
            default=None,
        )

        parser.add_argument(
            "--user",
            type=str,
            help="IoTDB username",
            default=None,
        )

        parser.add_argument(
            "--password",
            type=str,
            help="IoTDB password",
            default=None,
        )
        
        parser.add_argument(
            "--database",
            type=str,
            help="IoTDB database/session scope. Table dialect uses database name; tree dialect still requires explicit root paths in SQL.",
            default=None,
        )

        parser.add_argument(
            "--sql-dialect",
            type=str,
            help="SQL dialect: tree or table",
            default=None,
        )

        parser.add_argument(
            "--timezone",
            type=str,
            help="IoTDB session timezone",
            default=None,
        )
        
        parser.add_argument(
            "--export-path",
            type=str,
            help="Path for exporting query results",
            default=None,
        )
        parser.add_argument("--node-urls", type=str, default=None, help="Comma-separated IoTDB node URLs")
        parser.add_argument("--use-ssl", choices=("true", "false"), default=None)
        parser.add_argument("--ca-certs", type=str, default=None)
        parser.add_argument("--connection-timeout-ms", type=int, default=None)
        parser.add_argument("--enable-redirection", choices=("true", "false"), default=None)
        parser.add_argument("--enable-compression", choices=("true", "false"), default=None)
        parser.add_argument("--fetch-size", type=int, default=None)
        parser.add_argument("--max-retry", type=int, default=None)
        parser.add_argument("--max-pool-size", type=int, default=None)
        parser.add_argument("--wait-timeout-ms", type=int, default=None)

        args = parser.parse_args()
        targets_file = args.targets_file or os.getenv("TIMESEEK_IOTDB_TARGETS_FILE", "")
        targets_json = os.getenv("TIMESEEK_IOTDB_TARGETS_JSON", "")
        registry = IoTDBTargetRegistry.from_env(
            targets_file=targets_file or None,
            targets_json=targets_json or None,
        )
        if registry.list_targets(include_secret=False):
            target = registry.resolve(
                args.target_id or os.getenv("TIMESEEK_IOTDB_TARGET_ID")
            )
        else:
            wait_timeout = (
                args.wait_timeout_ms
                if args.wait_timeout_ms is not None
                else os.getenv("IOTDB_WAIT_TIMEOUT_MS")
            )
            target = target_from_mapping(
                {
                    "target_id": "unverified-template",
                    "display_name": "Unverified IoTDB template",
                    "host": _argument_or_env(args.host, "IOTDB_HOST"),
                    "port": _argument_or_env(args.port, "IOTDB_PORT"),
                    "user": _argument_or_env(args.user, "IOTDB_USER"),
                    "password": _argument_or_env(args.password, "IOTDB_PASSWORD"),
                    "database": _argument_or_env(args.database, "IOTDB_DATABASE"),
                    "sql_dialect": args.sql_dialect
                    or os.getenv("IOTDB_SQL_DIALECT")
                    or "table",
                    "timezone": _argument_or_env(args.timezone, "IOTDB_TIMEZONE"),
                    "export_path": (
                        args.export_path
                        if args.export_path is not None
                        else default_export_path()
                    ),
                    "node_urls": _argument_or_env(args.node_urls, "IOTDB_NODE_URLS"),
                    "iotdb_home": os.getenv("TIMESEEK_IOTDB_HOME"),
                    "use_ssl": _argument_or_env(args.use_ssl, "IOTDB_USE_SSL"),
                    "ca_certs": _argument_or_env(args.ca_certs, "IOTDB_CA_CERTS"),
                    "connection_timeout_in_ms": _argument_or_env(
                        args.connection_timeout_ms,
                        "IOTDB_CONNECTION_TIMEOUT_MS",
                    ),
                    "enable_redirection": _argument_or_env(
                        args.enable_redirection,
                        "IOTDB_ENABLE_REDIRECTION",
                    ),
                    "enable_compression": _argument_or_env(
                        args.enable_compression,
                        "IOTDB_ENABLE_COMPRESSION",
                    ),
                    "fetch_size": _argument_or_env(args.fetch_size, "IOTDB_FETCH_SIZE"),
                    "max_retry": _argument_or_env(args.max_retry, "IOTDB_MAX_RETRY"),
                    "max_pool_size": _argument_or_env(
                        args.max_pool_size,
                        "IOTDB_MAX_POOL_SIZE",
                    ),
                    "tree_wait_timeout_in_ms": (
                        args.wait_timeout_ms
                        if args.wait_timeout_ms is not None
                        else os.getenv("IOTDB_TREE_WAIT_TIMEOUT_MS")
                        or wait_timeout
                    ),
                    "table_wait_timeout_in_ms": (
                        args.wait_timeout_ms
                        if args.wait_timeout_ms is not None
                        else os.getenv("IOTDB_TABLE_WAIT_TIMEOUT_MS")
                        or wait_timeout
                    ),
                }
            )
            registry = registry.with_target(
                target,
                default_target_id=target.target_id,
            )
        from iotdb_mcp_server.session_manager import IoTDBSessionManager

        config = Config.from_target(
            target,
            target_registry=registry,
            session_manager=IoTDBSessionManager(registry),
        )
        config.targets_file = targets_file
        config.targets_json = targets_json
        if targets_file:
            try:
                config.targets_file_mtime_ns = Path(targets_file).expanduser().stat().st_mtime_ns
            except OSError:
                config.targets_file_mtime_ns = None
        return config
