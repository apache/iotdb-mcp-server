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

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping


DEFAULT_TARGET_ID = "default"
_TARGET_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")

_POLICY_ENV_KEYS = (
    "IOTDB_ENABLE_METADATA_QUERY",
    "IOTDB_METADATA_ALLOWED_USERS",
    "IOTDB_ENABLE_DATABASE_DDL",
    "IOTDB_DATABASE_DDL_ALLOWED_USERS",
    "IOTDB_REQUIRE_DROP_CONFIRM",
    "IOTDB_ENABLE_TABLE_DDL",
    "IOTDB_TABLE_DDL_ALLOWED_USERS",
    "IOTDB_REQUIRE_TABLE_DROP_CONFIRM",
    "IOTDB_ENABLE_TIMESERIES_DDL",
    "IOTDB_TIMESERIES_DDL_ALLOWED_USERS",
    "IOTDB_REQUIRE_TIMESERIES_DROP_CONFIRM",
    "IOTDB_ENABLE_TTL_SQL",
    "IOTDB_TTL_ALLOWED_USERS",
    "IOTDB_REQUIRE_TTL_UNSET_CONFIRM",
    "IOTDB_ENABLE_WRITE_DML",
    "IOTDB_WRITE_ALLOWED_USERS",
    "IOTDB_REQUIRE_DELETE_CONFIRM",
    "IOTDB_ENABLE_SQL_DRIVER",
    "IOTDB_SQL_DRIVER_ALLOWED_USERS",
    "IOTDB_SQL_DRIVER_MODE",
    "IOTDB_SQL_DRIVER_REQUIRE_DESTRUCTIVE_CONFIRM",
    "IOTDB_SQL_DRIVER_EXTRA_READONLY_PREFIXES",
    "IOTDB_SQL_DRIVER_EXTRA_DDL_PREFIXES",
    "IOTDB_SQL_DRIVER_EXTRA_FULL_PREFIXES",
    "IOTDB_ENABLE_MODEL_MANAGEMENT",
    "IOTDB_MODEL_ALLOWED_USERS",
    "IOTDB_REQUIRE_MODEL_DESTRUCTIVE_CONFIRM",
    "TIMESEEK_MCP_PERMISSION_ENFORCEMENT",
    "IOTDB_STRICT_PERMISSION_ENFORCEMENT",
)


def _env_or_default(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name)
    if value is None or str(value).strip() == "":
        return default
    return str(value)


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    return _coerce_bool(env.get(name), default)


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    return default


def _coerce_int(value: Any, default: int | None) -> int | None:
    if value is None or str(value).strip() == "":
        return default
    return int(value)


def _split_csv(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return tuple(item.strip() for item in str(value).split(",") if item.strip())


def _default_export_path(env: Mapping[str, str]) -> str:
    explicit = env.get("IOTDB_EXPORT_PATH")
    if explicit:
        return explicit

    workspace = env.get("TIMESEEK_WORKSPACE_ROOT") or env.get(
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


def normalize_target_id(value: Any, fallback: str = DEFAULT_TARGET_ID) -> str:
    target_id = str(value or fallback).strip() or fallback
    if not _TARGET_ID_RE.fullmatch(target_id):
        raise ValueError(
            "IoTDB target_id must start with a letter and contain only letters, "
            "numbers, underscore, dash, or dot."
        )
    return target_id


def _node_urls_from_host_port(host: str, port: int) -> tuple[str, ...]:
    return (f"{host}:{port}",)


def _host_port_from_node_urls(node_urls: tuple[str, ...]) -> tuple[str, int] | None:
    if not node_urls:
        return None
    first = node_urls[0].strip()
    if not first:
        return None
    if first.startswith("[") and "]:" in first:
        host, raw_port = first.rsplit(":", 1)
        return host.strip("[]"), int(raw_port)
    if ":" not in first:
        return first, 6667
    host, raw_port = first.rsplit(":", 1)
    return host, int(raw_port)


def _stable_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _mapping_get(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _credential_password(credential: Mapping[str, Any] | None) -> str | None:
    if not credential:
        return None
    if "password" not in credential:
        return None
    value = credential.get("password")
    return "" if value is None else str(value)


def _normalize_last_known_good_credential(
    value: Any,
    *,
    fallback_user: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}

    credential: dict[str, Any] = {
        key: item
        for key, item in value.items()
        if key
        in {
            "user",
            "username",
            "password",
            "password_source",
            "source",
            "last_success_at",
            "stale",
            "last_failure_code",
            "last_failure_at",
        }
    }
    if "username" in credential and "user" not in credential:
        credential["user"] = credential.pop("username")
    if "source" in credential and "password_source" not in credential:
        credential["password_source"] = credential.pop("source")
    if "user" not in credential or str(credential["user"]).strip() == "":
        credential["user"] = fallback_user
    if "password" in credential:
        credential["password"] = (
            "" if credential["password"] is None else str(credential["password"])
        )
        credential["password_set"] = credential["password"] != ""
    if "password_source" not in credential:
        credential["password_source"] = "last_success"
    if "stale" in credential:
        credential["stale"] = _coerce_bool(credential["stale"], False)
    return credential


def last_known_good_credential(
    *,
    user: str,
    password: str,
    password_source: str = "last_success",
    last_success_at: str | None = None,
) -> dict[str, Any]:
    return {
        "user": user,
        "password": password,
        "password_set": password != "",
        "password_source": password_source,
        "last_success_at": last_success_at or utc_now_iso(),
        "stale": False,
        "last_failure_code": None,
    }


def _normalized_match_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip().lower()


@dataclass(frozen=True)
class IoTDBTarget:
    """Named IoTDB execution target shared by all TimeSeek runtimes."""

    target_id: str
    display_name: str
    kind: str
    host: str
    port: int
    node_urls: tuple[str, ...]
    user: str
    password: str
    database: str
    sql_dialect: str
    timezone: str
    export_path: str
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
    policy: dict[str, Any] = field(default_factory=dict)
    last_known_good_credential: dict[str, Any] = field(default_factory=dict)
    verified_at: str = ""

    def __post_init__(self) -> None:
        normalize_target_id(self.target_id)
        if not self.host:
            raise ValueError("IoTDB target host cannot be empty.")
        if self.port < 1 or self.port > 65535:
            raise ValueError("IoTDB target port must be between 1 and 65535.")
        if self.sql_dialect not in {"tree", "table"}:
            raise ValueError("IoTDB target sql_dialect must be tree or table.")
        if not self.node_urls:
            raise ValueError("IoTDB target must define at least one node URL.")

    @property
    def password_set(self) -> bool:
        return self.password != ""

    @property
    def last_known_good_password_set(self) -> bool:
        return _credential_password(self.last_known_good_credential) not in (None, "")

    def fingerprint(self) -> str:
        payload = {
            "target_id": self.target_id,
            "kind": self.kind,
            "node_urls": self.node_urls,
            "user": self.user,
            "database": self.database,
            "sql_dialect": self.sql_dialect,
            "timezone": self.timezone,
            "use_ssl": self.use_ssl,
            "ca_certs": self.ca_certs,
            "connection_timeout_in_ms": self.connection_timeout_in_ms,
            "enable_redirection": self.enable_redirection,
            "enable_compression": self.enable_compression,
            "fetch_size": self.fetch_size,
            "max_retry": self.max_retry,
            "max_pool_size": self.max_pool_size,
            "tree_wait_timeout_in_ms": self.tree_wait_timeout_in_ms,
            "table_wait_timeout_in_ms": self.table_wait_timeout_in_ms,
        }
        return "sha256:" + hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()

    def auth_fingerprint(self) -> str:
        payload = {
            "target_fingerprint": self.fingerprint(),
            "password": self.password,
        }
        return "sha256:" + hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()

    def _last_known_good_credential_dict(self, include_secret: bool) -> dict[str, Any]:
        credential = dict(self.last_known_good_credential)
        if not credential:
            return {}
        raw_password = _credential_password(credential)
        if raw_password is not None:
            credential["password"] = (
                raw_password if include_secret else ("***" if raw_password else "")
            )
            credential["password_set"] = raw_password != ""
        return credential

    def as_dict(self, include_secret: bool = False) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "display_name": self.display_name,
            "kind": self.kind,
            "host": self.host,
            "port": self.port,
            "node_urls": list(self.node_urls),
            "user": self.user,
            "password": self.password if include_secret else ("***" if self.password else ""),
            "password_set": self.password_set,
            "database": self.database,
            "sql_dialect": self.sql_dialect,
            "timezone": self.timezone,
            "export_path": self.export_path,
            "iotdb_home": self.iotdb_home,
            "use_ssl": self.use_ssl,
            "ca_certs": self.ca_certs,
            "connection_timeout_in_ms": self.connection_timeout_in_ms,
            "enable_redirection": self.enable_redirection,
            "enable_compression": self.enable_compression,
            "fetch_size": self.fetch_size,
            "max_retry": self.max_retry,
            "max_pool_size": self.max_pool_size,
            "tree_wait_timeout_in_ms": self.tree_wait_timeout_in_ms,
            "table_wait_timeout_in_ms": self.table_wait_timeout_in_ms,
            "policy": dict(self.policy),
            "last_known_good_credential": self._last_known_good_credential_dict(
                include_secret
            ),
            "verified_at": self.verified_at,
            "fingerprint": self.fingerprint(),
        }


def target_from_mapping(
    mapping: Mapping[str, Any],
    fallback_target_id: str = DEFAULT_TARGET_ID,
    fallback_export_path: str = "/tmp",
) -> IoTDBTarget:
    target_id = normalize_target_id(
        _mapping_get(mapping, "target_id", "id", "name"), fallback_target_id
    )
    node_urls = _split_csv(_mapping_get(mapping, "node_urls", "endpoints", "nodes"))
    host = str(_mapping_get(mapping, "host", "IOTDB_HOST") or "").strip()
    raw_port = _mapping_get(mapping, "port", "IOTDB_PORT")
    port = _coerce_int(raw_port, None)

    if not node_urls and host and port is not None:
        node_urls = _node_urls_from_host_port(host, port)
    if node_urls and (not host or port is None):
        parsed = _host_port_from_node_urls(node_urls)
        if parsed:
            host, port = parsed

    host = host or "127.0.0.1"
    port = 6667 if port is None else port
    if not node_urls:
        node_urls = _node_urls_from_host_port(host, port)

    sql_dialect = str(
        _mapping_get(mapping, "sql_dialect", "dialect", "IOTDB_SQL_DIALECT") or "table"
    ).strip().lower()
    user = str(_mapping_get(mapping, "user", "username", "IOTDB_USER") or "root")
    raw_last_known_good = _mapping_get(
        mapping,
        "last_known_good_credential",
        "last_success_credential",
        "last_successful_credential",
    )
    normalized_last_known_good = _normalize_last_known_good_credential(
        raw_last_known_good,
        fallback_user=user,
    )
    raw_password = _mapping_get(mapping, "password", "IOTDB_PASSWORD")
    if raw_password is None:
        raw_password = _credential_password(normalized_last_known_good)
    policy = dict(_mapping_get(mapping, "policy") or {})

    return IoTDBTarget(
        target_id=target_id,
        display_name=str(
            _mapping_get(mapping, "display_name", "label") or target_id
        ).strip(),
        kind=str(_mapping_get(mapping, "kind", "target_kind") or "local").strip()
        or "local",
        host=host,
        port=port,
        node_urls=node_urls,
        user=user,
        password="" if raw_password is None else str(raw_password),
        database=str(_mapping_get(mapping, "database", "IOTDB_DATABASE") or "test"),
        sql_dialect=sql_dialect,
        timezone=str(
            _mapping_get(mapping, "timezone", "time_zone", "IOTDB_TIMEZONE") or "+00:00"
        ),
        export_path=str(
            _mapping_get(mapping, "export_path", "IOTDB_EXPORT_PATH")
            or fallback_export_path
        ),
        iotdb_home=str(
            _mapping_get(mapping, "iotdb_home", "TIMESEEK_IOTDB_HOME") or ""
        ),
        use_ssl=_coerce_bool(_mapping_get(mapping, "use_ssl", "IOTDB_USE_SSL"), False),
        ca_certs=str(_mapping_get(mapping, "ca_certs", "IOTDB_CA_CERTS") or ""),
        connection_timeout_in_ms=_coerce_int(
            _mapping_get(
                mapping,
                "connection_timeout_in_ms",
                "connection_timeout_ms",
                "IOTDB_CONNECTION_TIMEOUT_MS",
            ),
            None,
        ),
        enable_redirection=_coerce_bool(
            _mapping_get(mapping, "enable_redirection", "IOTDB_ENABLE_REDIRECTION"),
            True,
        ),
        enable_compression=_coerce_bool(
            _mapping_get(mapping, "enable_compression", "IOTDB_ENABLE_COMPRESSION"),
            False,
        ),
        fetch_size=_coerce_int(_mapping_get(mapping, "fetch_size", "IOTDB_FETCH_SIZE"), 1024)
        or 1024,
        max_retry=_coerce_int(_mapping_get(mapping, "max_retry", "IOTDB_MAX_RETRY"), 3)
        or 3,
        max_pool_size=_coerce_int(
            _mapping_get(mapping, "max_pool_size", "IOTDB_MAX_POOL_SIZE"), 100
        )
        or 100,
        tree_wait_timeout_in_ms=_coerce_int(
            _mapping_get(
                mapping,
                "tree_wait_timeout_in_ms",
                "wait_timeout_in_ms",
                "IOTDB_TREE_WAIT_TIMEOUT_MS",
                "IOTDB_WAIT_TIMEOUT_MS",
            ),
            5000,
        )
        or 5000,
        table_wait_timeout_in_ms=_coerce_int(
            _mapping_get(
                mapping,
                "table_wait_timeout_in_ms",
                "IOTDB_TABLE_WAIT_TIMEOUT_MS",
                "IOTDB_WAIT_TIMEOUT_MS",
            ),
            10000,
        )
        or 10000,
        policy=policy,
        last_known_good_credential=normalized_last_known_good,
        verified_at=str(_mapping_get(mapping, "verified_at") or "").strip(),
    )


def _legacy_target_from_env(env: Mapping[str, str]) -> IoTDBTarget:
    target_id = env.get("TIMESEEK_IOTDB_TARGET_ID") or env.get(
        "TIMESEEK_IOTDB_DEFAULT_TARGET_ID"
    )
    policy = {key: env[key] for key in _POLICY_ENV_KEYS if key in env}
    mapping: dict[str, Any] = {
        "target_id": target_id or DEFAULT_TARGET_ID,
        "display_name": env.get("TIMESEEK_IOTDB_TARGET_NAME") or "Default IoTDB",
        "kind": env.get("TIMESEEK_IOTDB_TARGET_KIND") or "local",
        "iotdb_home": env.get("TIMESEEK_IOTDB_HOME", ""),
        "host": env.get("IOTDB_HOST", "127.0.0.1"),
        "port": env.get("IOTDB_PORT", 6667),
        "node_urls": env.get("IOTDB_NODE_URLS", ""),
        "user": env.get("IOTDB_USER", "root"),
        "password": env.get("IOTDB_PASSWORD", ""),
        "database": env.get("IOTDB_DATABASE", "test"),
        "timezone": _env_or_default(env, "IOTDB_TIMEZONE", "+00:00"),
        "sql_dialect": env.get("IOTDB_SQL_DIALECT", "table"),
        "export_path": _default_export_path(env),
        "use_ssl": _env_bool(env, "IOTDB_USE_SSL", False),
        "ca_certs": env.get("IOTDB_CA_CERTS", ""),
        "connection_timeout_in_ms": env.get("IOTDB_CONNECTION_TIMEOUT_MS"),
        "enable_redirection": _env_bool(env, "IOTDB_ENABLE_REDIRECTION", True),
        "enable_compression": _env_bool(env, "IOTDB_ENABLE_COMPRESSION", False),
        "fetch_size": env.get("IOTDB_FETCH_SIZE", "1024"),
        "max_retry": env.get("IOTDB_MAX_RETRY", "3"),
        "max_pool_size": env.get("IOTDB_MAX_POOL_SIZE", "100"),
        "tree_wait_timeout_in_ms": env.get("IOTDB_TREE_WAIT_TIMEOUT_MS")
        or env.get("IOTDB_WAIT_TIMEOUT_MS")
        or "5000",
        "table_wait_timeout_in_ms": env.get("IOTDB_TABLE_WAIT_TIMEOUT_MS")
        or env.get("IOTDB_WAIT_TIMEOUT_MS")
        or "10000",
        "policy": policy,
    }
    return target_from_mapping(mapping, fallback_export_path=_default_export_path(env))


def _parse_targets_payload(payload: Any, export_path: str) -> tuple[dict[str, IoTDBTarget], str | None]:
    targets: dict[str, IoTDBTarget] = {}
    default_target_id: str | None = None

    def add(spec: Mapping[str, Any], fallback_id: str = DEFAULT_TARGET_ID) -> None:
        target = target_from_mapping(spec, fallback_id, fallback_export_path=export_path)
        targets[target.target_id] = target

    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, Mapping):
                raise ValueError("Each IoTDB target entry must be an object.")
            add(item)
        return targets, default_target_id

    if not isinstance(payload, Mapping):
        raise ValueError("IoTDB target registry JSON must be an object or array.")

    default_raw = payload.get("default_target_id") or payload.get("default") or payload.get(
        "active_target_id"
    )
    if default_raw:
        default_target_id = normalize_target_id(default_raw)

    raw_targets = payload.get("targets")
    if raw_targets is not None:
        nested, _ = _parse_targets_payload(raw_targets, export_path)
        targets.update(nested)
        return targets, default_target_id

    target_shape_keys = {"host", "node_urls", "endpoints", "user", "database", "sql_dialect"}
    if any(key in payload for key in target_shape_keys):
        add(payload)
        return targets, default_target_id

    for key, value in payload.items():
        if key in {"default", "default_target_id", "active_target_id"}:
            continue
        if not isinstance(value, Mapping):
            raise ValueError("IoTDB target registry map values must be objects.")
        add(value, fallback_id=str(key))

    return targets, default_target_id


class IoTDBTargetRegistry:
    def __init__(
        self,
        targets: Mapping[str, IoTDBTarget],
        default_target_id: str | None = None,
    ) -> None:
        self._targets = dict(targets)
        if not self._targets:
            self.default_target_id = None
            return
        self.default_target_id = normalize_target_id(
            default_target_id or sorted(self._targets)[0]
        )
        if self.default_target_id not in self._targets:
            raise KeyError(f"Default IoTDB target '{self.default_target_id}' is not registered.")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        targets_file: str | None = None,
        targets_json: str | None = None,
    ) -> "IoTDBTargetRegistry":
        resolved_env = os.environ if env is None else env
        export_path = _default_export_path(resolved_env)
        targets: dict[str, IoTDBTarget] = {}
        requested_default = resolved_env.get("TIMESEEK_IOTDB_DEFAULT_TARGET_ID")
        default_target_id = normalize_target_id(requested_default) if requested_default else None

        file_value = targets_file or resolved_env.get("TIMESEEK_IOTDB_TARGETS_FILE")
        if file_value:
            file_path = Path(file_value).expanduser()
            if file_path.exists():
                if not file_path.is_file():
                    raise ValueError(f"IoTDB targets file is not a file: {file_path}")
                payload = json.loads(file_path.read_text(encoding="utf-8"))
                parsed_targets, parsed_default = _parse_targets_payload(payload, export_path)
                targets.update(
                    {
                        target_id: target
                        for target_id, target in parsed_targets.items()
                        if target.verified_at
                        or target.last_known_good_credential.get("last_success_at")
                    }
                )
                if parsed_default:
                    default_target_id = parsed_default

        json_value = targets_json or resolved_env.get("TIMESEEK_IOTDB_TARGETS_JSON")
        if json_value:
            parsed_targets, parsed_default = _parse_targets_payload(
                json.loads(json_value), export_path
            )
            targets.update(
                {
                    target_id: target
                    for target_id, target in parsed_targets.items()
                    if target.verified_at
                    or target.last_known_good_credential.get("last_success_at")
                }
            )
            if parsed_default:
                default_target_id = parsed_default

        if default_target_id not in targets:
            default_target_id = sorted(targets)[0] if targets else None

        return cls(targets, default_target_id)

    def resolve(self, target_id: str | None = None) -> IoTDBTarget:
        if not self._targets:
            raise KeyError(
                "No verified IoTDB target is registered. Prepare a target and "
                "connect it successfully before running database tools."
            )
        resolved_id = normalize_target_id(target_id or self.default_target_id)
        try:
            return self._targets[resolved_id]
        except KeyError as exc:
            available = ", ".join(sorted(self._targets))
            raise KeyError(
                f"IoTDB target '{resolved_id}' is not registered. Available targets: {available}"
            ) from exc

    def resolve_selector(
        self,
        selector: str | Mapping[str, Any] | None = None,
        target_id: str | None = None,
    ) -> IoTDBTarget:
        if target_id:
            return self.resolve(target_id)
        if selector is None:
            return self.resolve()
        if isinstance(selector, str):
            return self.resolve(selector)
        if not isinstance(selector, Mapping):
            raise ValueError("IoTDB target selector must be a target id or object.")

        explicit_id = _mapping_get(selector, "target_id", "id", "name")
        if explicit_id:
            return self.resolve(str(explicit_id))

        allowed_fields = {
            "display_name",
            "label",
            "kind",
            "target_kind",
            "host",
            "port",
            "database",
            "sql_dialect",
            "dialect",
            "user",
        }
        requested = {
            field: selector[field]
            for field in allowed_fields
            if field in selector and str(selector[field]).strip() != ""
        }
        endpoint = _mapping_get(selector, "node_url", "endpoint")
        node_urls = _split_csv(_mapping_get(selector, "node_urls", "endpoints", "nodes"))

        if not requested and not endpoint and not node_urls:
            return self.resolve()

        matches: list[IoTDBTarget] = []
        for candidate in self._targets.values():
            candidate_data = candidate.as_dict(include_secret=False)
            candidate_data["label"] = candidate.display_name
            candidate_data["target_kind"] = candidate.kind
            candidate_data["dialect"] = candidate.sql_dialect
            matched = True
            for field, expected in requested.items():
                candidate_value = candidate_data.get(field)
                if _normalized_match_value(candidate_value) != _normalized_match_value(expected):
                    matched = False
                    break
            if matched and endpoint:
                matched = str(endpoint).strip() in candidate.node_urls
            if matched and node_urls:
                matched = all(item in candidate.node_urls for item in node_urls)
            if matched:
                matches.append(candidate)

        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(
                "No IoTDB target matches selector fields. Use list_iotdb_targets "
                "to inspect registered targets."
            )
        matched_ids = ", ".join(sorted(target.target_id for target in matches))
        raise KeyError(
            "IoTDB target selector is ambiguous. Matched targets: "
            f"{matched_ids}. Add target_id to disambiguate."
        )

    def list_targets(self, include_secret: bool = False) -> list[dict[str, Any]]:
        return [
            self._targets[target_id].as_dict(include_secret=include_secret)
            for target_id in sorted(self._targets)
        ]

    def as_dict(self, include_secret: bool = False) -> dict[str, Any]:
        return {
            "default_target_id": self.default_target_id,
            "targets": self.list_targets(include_secret=include_secret),
        }

    def with_target(
        self, target: IoTDBTarget, default_target_id: str | None = None
    ) -> "IoTDBTargetRegistry":
        targets = dict(self._targets)
        targets[target.target_id] = target
        return IoTDBTargetRegistry(
            targets,
            default_target_id=default_target_id or self.default_target_id or target.target_id,
        )

    def without_target(
        self, target_id: str, default_target_id: str | None = None
    ) -> "IoTDBTargetRegistry":
        resolved_id = normalize_target_id(target_id)
        if resolved_id not in self._targets:
            available = ", ".join(sorted(self._targets))
            raise KeyError(
                f"IoTDB target '{resolved_id}' is not registered. Available targets: {available}"
            )
        targets = dict(self._targets)
        targets.pop(resolved_id)
        if not targets:
            return IoTDBTargetRegistry({})
        next_default = normalize_target_id(default_target_id or self.default_target_id)
        if next_default == resolved_id:
            next_default = sorted(targets)[0]
        return IoTDBTargetRegistry(targets, default_target_id=next_default)

    def with_default(self, target_id: str) -> "IoTDBTargetRegistry":
        resolved_id = normalize_target_id(target_id)
        if resolved_id not in self._targets:
            available = ", ".join(sorted(self._targets))
            raise KeyError(
                f"IoTDB target '{resolved_id}' is not registered. Available targets: {available}"
            )
        return IoTDBTargetRegistry(self._targets, default_target_id=resolved_id)


def merge_target(target: IoTDBTarget, overrides: Mapping[str, Any]) -> IoTDBTarget:
    payload = target.as_dict(include_secret=True)
    payload.pop("password_set", None)
    payload.pop("fingerprint", None)
    clean_overrides = {
        key: value for key, value in overrides.items() if value is not None
    }
    if "node_urls" in clean_overrides:
        if "host" not in clean_overrides:
            payload.pop("host", None)
        if "port" not in clean_overrides:
            payload.pop("port", None)
    elif ("host" in clean_overrides or "port" in clean_overrides) and (
        "node_urls" not in clean_overrides
    ):
        payload.pop("node_urls", None)
    payload.update(clean_overrides)
    return target_from_mapping(
        payload,
        fallback_target_id=target.target_id,
        fallback_export_path=target.export_path,
    )


def mark_target_last_known_good(
    target: IoTDBTarget,
    *,
    user: str | None = None,
    password: str | None = None,
    password_source: str = "last_success",
    last_success_at: str | None = None,
) -> IoTDBTarget:
    resolved_user = user or target.user
    resolved_password = target.password if password is None else password
    return merge_target(
        target,
        {
            "user": resolved_user,
            "password": resolved_password,
            "last_known_good_credential": last_known_good_credential(
                user=resolved_user,
                password=resolved_password,
                password_source=password_source,
                last_success_at=last_success_at,
            ),
            "verified_at": last_success_at or utc_now_iso(),
        },
    )
