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

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
from threading import RLock
from typing import Any


_DEFAULT_SERVER_NAME = "iotdb"
_SHELL_DEFAULT_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}$")
_SENSITIVE_KEY_RE = re.compile(r"(PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY)", re.IGNORECASE)
_SESSION_POLICY_LOCK = RLock()
_SESSION_POLICY: dict[str, str] = {}

_POLICY_KEYS = frozenset(
    {
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
    }
)

_FULL_PERMISSION_DEFAULTS = {
    "IOTDB_ENABLE_METADATA_QUERY": "true",
    "IOTDB_METADATA_ALLOWED_USERS": "*",
    "IOTDB_ENABLE_DATABASE_DDL": "true",
    "IOTDB_DATABASE_DDL_ALLOWED_USERS": "*",
    "IOTDB_REQUIRE_DROP_CONFIRM": "true",
    "IOTDB_ENABLE_TABLE_DDL": "true",
    "IOTDB_TABLE_DDL_ALLOWED_USERS": "*",
    "IOTDB_REQUIRE_TABLE_DROP_CONFIRM": "true",
    "IOTDB_ENABLE_TIMESERIES_DDL": "true",
    "IOTDB_TIMESERIES_DDL_ALLOWED_USERS": "*",
    "IOTDB_REQUIRE_TIMESERIES_DROP_CONFIRM": "true",
    "IOTDB_ENABLE_TTL_SQL": "true",
    "IOTDB_TTL_ALLOWED_USERS": "*",
    "IOTDB_REQUIRE_TTL_UNSET_CONFIRM": "true",
    "IOTDB_ENABLE_WRITE_DML": "true",
    "IOTDB_WRITE_ALLOWED_USERS": "*",
    "IOTDB_REQUIRE_DELETE_CONFIRM": "true",
    "IOTDB_ENABLE_SQL_DRIVER": "true",
    "IOTDB_SQL_DRIVER_ALLOWED_USERS": "*",
    "IOTDB_SQL_DRIVER_MODE": "full",
    "IOTDB_SQL_DRIVER_REQUIRE_DESTRUCTIVE_CONFIRM": "true",
    "IOTDB_ENABLE_MODEL_MANAGEMENT": "true",
    "IOTDB_MODEL_ALLOWED_USERS": "*",
    "IOTDB_REQUIRE_MODEL_DESTRUCTIVE_CONFIRM": "true",
    "TIMESEEK_MCP_PERMISSION_ENFORCEMENT": "advisory",
    "IOTDB_STRICT_PERMISSION_ENFORCEMENT": "false",
}

_PRESET_POLICIES = {
    "full": dict(_FULL_PERMISSION_DEFAULTS),
    "ddl": {
        **_FULL_PERMISSION_DEFAULTS,
        "IOTDB_SQL_DRIVER_MODE": "ddl",
        "IOTDB_ENABLE_WRITE_DML": "false",
    },
    "readonly": {
        **_FULL_PERMISSION_DEFAULTS,
        "IOTDB_ENABLE_DATABASE_DDL": "false",
        "IOTDB_ENABLE_TABLE_DDL": "false",
        "IOTDB_ENABLE_TIMESERIES_DDL": "false",
        "IOTDB_ENABLE_TTL_SQL": "false",
        "IOTDB_ENABLE_WRITE_DML": "false",
        "IOTDB_SQL_DRIVER_MODE": "readonly",
    },
}


def _candidate_mcp_config_paths() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.getenv("TIMESEEK_MCP_CONFIG_PATH")
    if explicit:
        candidates.append(Path(explicit).expanduser())

    timeseek_root = os.getenv("TIMESEEK_ROOT")
    if timeseek_root:
        root = Path(timeseek_root).expanduser()
        candidates.extend(
            [
                root / ".mcp.json",
                root / "timeseek-codex-multi-plugin" / ".mcp.json",
                root / "timeseek-codex-single-plugin" / ".mcp.json",
            ]
        )

    repo_root = os.getenv("REPO_ROOT") or os.getenv("TIMESEEK_REPO_ROOT")
    if repo_root:
        root = Path(repo_root).expanduser()
        candidates.extend(
            [
                root / ".mcp.json",
                root / "timeseek_codex" / "timeseek-codex-multi-plugin" / ".mcp.json",
                root / "timeseek_codex" / "timeseek-codex-single-plugin" / ".mcp.json",
                root / "timeseek_cc" / ".mcp.json",
            ]
        )

    cwd = Path.cwd()
    candidates.extend(
        [
            cwd / ".mcp.json",
            cwd / "timeseek_codex" / "timeseek-codex-multi-plugin" / ".mcp.json",
            cwd / "timeseek_codex" / "timeseek-codex-single-plugin" / ".mcp.json",
            cwd / "timeseek_cc" / ".mcp.json",
        ]
    )

    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            deduped.append(candidate)
    return deduped


def _expand_config_value(value: Any) -> str:
    text = str(value)
    match = _SHELL_DEFAULT_RE.fullmatch(text)
    if not match:
        return text
    name, default = match.groups()
    current = os.getenv(name)
    return current if current not in (None, "") else default


def _load_mcp_env() -> tuple[dict[str, str], str]:
    server_name = os.getenv("TIMESEEK_MCP_SERVER_NAME", _DEFAULT_SERVER_NAME)
    for path in _candidate_mcp_config_paths():
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        server = data.get("mcpServers", {}).get(server_name, {})
        env = server.get("env", {})
        if isinstance(env, dict):
            return {str(key): _expand_config_value(value) for key, value in env.items()}, str(path)
    return {}, ""


def dynamic_getenv(name: str, default: str | None = None) -> str | None:
    with _SESSION_POLICY_LOCK:
        value = _SESSION_POLICY.get(name)
    if value not in (None, ""):
        return value

    env, _ = _load_mcp_env()
    value = env.get(name)
    if value not in (None, ""):
        return value

    value = os.getenv(name)
    if value not in (None, ""):
        return value

    return _FULL_PERMISSION_DEFAULTS.get(name, default)


def dynamic_env_bool(name: str, default: bool) -> bool:
    value = dynamic_getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    return default


def permission_enforcement_mode() -> str:
    if dynamic_env_bool("IOTDB_STRICT_PERMISSION_ENFORCEMENT", False):
        return "strict"
    mode = (dynamic_getenv("TIMESEEK_MCP_PERMISSION_ENFORCEMENT", "advisory") or "advisory").strip().lower()
    if mode not in {"advisory", "strict"}:
        return "advisory"
    return mode


def strict_permission_enforcement() -> bool:
    return permission_enforcement_mode() == "strict"


def _redact_env(env: dict[str, str]) -> dict[str, str]:
    return {
        key: "***" if _SENSITIVE_KEY_RE.search(key) else value
        for key, value in env.items()
    }


def _session_policy_copy() -> dict[str, str]:
    with _SESSION_POLICY_LOCK:
        return dict(_SESSION_POLICY)


def _coerce_policy_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _validate_policy_keys(policy: Mapping[str, Any]) -> None:
    unknown = sorted(str(key) for key in policy if str(key) not in _POLICY_KEYS)
    if unknown:
        raise ValueError(
            "Unsupported session policy key(s): "
            + ", ".join(unknown)
            + ". Use get_iotdb_session_policy to inspect supported keys."
        )


def session_policy_for_preset(preset: str) -> dict[str, str]:
    normalized = preset.strip().lower()
    try:
        return dict(_PRESET_POLICIES[normalized])
    except KeyError as exc:
        raise ValueError("Unsupported policy preset. Expected one of: full, ddl, readonly.") from exc


def supported_policy_keys() -> list[str]:
    return sorted(_POLICY_KEYS)


def set_session_policy(
    policy: Mapping[str, Any] | None = None,
    *,
    preset: str | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    updates: dict[str, str] = {}
    if preset:
        updates.update(session_policy_for_preset(preset))
    if policy:
        _validate_policy_keys(policy)
        updates.update({str(key): _coerce_policy_value(value) for key, value in policy.items()})

    with _SESSION_POLICY_LOCK:
        if replace:
            _SESSION_POLICY.clear()
        _SESSION_POLICY.update(updates)

    return dynamic_policy_snapshot()


def reset_session_policy(keys: list[str] | None = None) -> dict[str, Any]:
    if keys is not None:
        _validate_policy_keys({key: "" for key in keys})

    with _SESSION_POLICY_LOCK:
        if keys is None:
            _SESSION_POLICY.clear()
        else:
            for key in keys:
                _SESSION_POLICY.pop(key, None)

    return dynamic_policy_snapshot()


def dynamic_policy_snapshot() -> dict[str, Any]:
    env, path = _load_mcp_env()
    session_policy = _session_policy_copy()
    effective: dict[str, str] = {}
    for key in supported_policy_keys():
        value = dynamic_getenv(key, "")
        if value not in (None, ""):
            effective[key] = value
    return {
        "mcp_config_path": path,
        "session_policy": _redact_env(session_policy),
        "mcp_env": _redact_env(env),
        "defaults": dict(_FULL_PERMISSION_DEFAULTS),
        "effective_policy": _redact_env(effective),
        "supported_keys": supported_policy_keys(),
        "presets": sorted(_PRESET_POLICIES),
    }
