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
import hashlib
import json
import os
from pathlib import Path
import re
from threading import RLock
from typing import Any

from iotdb_mcp_server.policy_approval import PolicyApprovalStore


_DEFAULT_SERVER_NAME = "iotdb"
_SHELL_DEFAULT_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}$")
_SENSITIVE_KEY_RE = re.compile(
    r"(PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY)", re.IGNORECASE
)
_WITHHELD_ENV_KEYS = frozenset({"TIMESEEK_IOTDB_TARGETS_JSON"})
_SESSION_POLICY_LOCK = RLock()
_SESSION_POLICY: dict[str, str] = {}
_DEPLOYMENT_POLICY: dict[str, str] | None = None
_DEPLOYMENT_ENV: dict[str, str] = {}
_DEPLOYMENT_CONFIG_PATH = ""
_APPROVAL_MODE = "require"
_APPROVAL_STORE: PolicyApprovalStore | None = None
_POLICY_REVISION = 0

_ENFORCEMENT_POLICY_KEYS = frozenset(
    {
        "TIMESEEK_MCP_PERMISSION_ENFORCEMENT",
        "IOTDB_STRICT_PERMISSION_ENFORCEMENT",
    }
)

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

# Prefix changes can reclassify writes as reads, so they are administrator-only.
_ADMIN_POLICY_KEYS = _ENFORCEMENT_POLICY_KEYS | frozenset(
    key for key in _POLICY_KEYS if "_EXTRA_" in key
)
_BOOL_POLICY_KEYS = frozenset(
    key
    for key in _POLICY_KEYS
    if key.startswith("IOTDB_ENABLE_")
    or "CONFIRM" in key
    or key == "IOTDB_STRICT_PERMISSION_ENFORCEMENT"
)
_MODE_RANK = {"readonly": 0, "ddl": 1, "full": 2}

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
}

_ENFORCEMENT_DEFAULTS = {
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
        "IOTDB_ENABLE_MODEL_MANAGEMENT": "false",
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
            return {
                str(key): _expand_config_value(value) for key, value in env.items()
            }, str(path)
    return {}, ""


def _normalize_policy_value(key: str, value: Any) -> str:
    text = _coerce_policy_value(value).strip()
    if key in _BOOL_POLICY_KEYS:
        if text.lower() in {"1", "true", "yes", "on"}:
            return "true"
        if text.lower() in {"0", "false", "no", "off"}:
            return "false"
        raise ValueError(f"{key} must be a boolean.")
    if key == "IOTDB_SQL_DRIVER_MODE":
        if text.lower() not in _MODE_RANK:
            raise ValueError(f"{key} must be readonly, ddl, or full.")
        return text.lower()
    if key == "TIMESEEK_MCP_PERMISSION_ENFORCEMENT":
        if text.lower() not in {"advisory", "strict"}:
            raise ValueError(f"{key} must be advisory or strict.")
        return text.lower()
    items = {item.strip() for item in text.split(",") if item.strip()}
    if key.endswith("_ALLOWED_USERS"):
        return "*" if "*" in items else ",".join(sorted(items))
    return ",".join(sorted(item.upper() for item in items))


def initialize_runtime_policy() -> None:
    """Freeze operator policy once, before tools are registered. No MCP reload API."""
    global _DEPLOYMENT_POLICY, _DEPLOYMENT_ENV, _DEPLOYMENT_CONFIG_PATH
    global _APPROVAL_MODE, _APPROVAL_STORE
    with _SESSION_POLICY_LOCK:
        if _DEPLOYMENT_POLICY is not None:
            return
        env, path = _load_mcp_env()
        # Explicit process environment wins over config discovery, including empty
        # allowlists. Malformed policy fails startup rather than enabling access.
        source = {**env, **os.environ}
        defaults = {**_FULL_PERMISSION_DEFAULTS, **_ENFORCEMENT_DEFAULTS}
        deployment = {
            key: _normalize_policy_value(key, source.get(key, defaults.get(key, "")))
            for key in _POLICY_KEYS
        }
        mode = (
            source.get("IOTDB_SESSION_POLICY_APPROVAL_MODE", "require").strip().lower()
        )
        if mode not in {"require", "allow"}:
            raise ValueError(
                "IOTDB_SESSION_POLICY_APPROVAL_MODE must be require or allow."
            )
        directory = source.get("IOTDB_SESSION_POLICY_APPROVAL_DIR", "")
        store = PolicyApprovalStore(Path(directory).expanduser()) if directory else None
        _DEPLOYMENT_ENV = env
        _DEPLOYMENT_CONFIG_PATH = path
        _APPROVAL_MODE = mode
        _APPROVAL_STORE = store
        _DEPLOYMENT_POLICY = deployment


def dynamic_getenv(name: str, default: str | None = None) -> str | None:
    if name in _POLICY_KEYS:
        initialize_runtime_policy()
        with _SESSION_POLICY_LOCK:
            assert _DEPLOYMENT_POLICY is not None
            return _SESSION_POLICY.get(name, _DEPLOYMENT_POLICY[name])

    # Non-policy operational settings retain their existing lookup behavior.
    env, _ = _load_mcp_env()
    value = env.get(name)
    if value not in (None, ""):
        return value

    value = os.getenv(name)
    if value not in (None, ""):
        return value

    return _FULL_PERMISSION_DEFAULTS.get(
        name,
        _ENFORCEMENT_DEFAULTS.get(name, default),
    )


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
    mode = (
        (
            dynamic_getenv("TIMESEEK_MCP_PERMISSION_ENFORCEMENT", "advisory")
            or "advisory"
        )
        .strip()
        .lower()
    )
    if mode not in {"advisory", "strict"}:
        return "advisory"
    return mode


def strict_permission_enforcement() -> bool:
    return permission_enforcement_mode() == "strict"


def _redact_env(env: dict[str, str]) -> dict[str, str]:
    return {
        key: (
            "***"
            if key in _WITHHELD_ENV_KEYS or _SENSITIVE_KEY_RE.search(key)
            else value
        )
        for key, value in env.items()
    }


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
    admin = sorted(str(key) for key in policy if str(key) in _ADMIN_POLICY_KEYS)
    if admin:
        raise PermissionError(
            "Administrator-only policy key(s): "
            + ", ".join(admin)
            + ". Change protected deployment configuration and restart the server."
        )


def session_policy_for_preset(preset: str) -> dict[str, str]:
    normalized = preset.strip().lower()
    try:
        return dict(_PRESET_POLICIES[normalized])
    except KeyError as exc:
        raise ValueError(
            "Unsupported policy preset. Expected one of: full, ddl, readonly."
        ) from exc


def supported_policy_keys() -> list[str]:
    return sorted(_POLICY_KEYS - _ADMIN_POLICY_KEYS)


def _no_broader(key: str, candidate: str, limit: str) -> bool:
    if candidate == limit:
        return True
    if key.startswith("IOTDB_ENABLE_"):
        return candidate == "false"
    if "CONFIRM" in key:
        return candidate == "true"
    if key == "IOTDB_SQL_DRIVER_MODE":
        return _MODE_RANK[candidate] <= _MODE_RANK[limit]
    if key.endswith("_ALLOWED_USERS"):
        candidate_users = {item for item in candidate.split(",") if item}
        limit_users = {item for item in limit.split(",") if item}
        return "*" in limit_users or candidate_users <= limit_users
    return False


def _apply_session_candidate(candidate: dict[str, str]) -> dict[str, Any]:
    """Validate, authorize, and commit under one lock, including reset/replace."""
    global _POLICY_REVISION
    assert _DEPLOYMENT_POLICY is not None
    before = {**_DEPLOYMENT_POLICY, **_SESSION_POLICY}
    after = {**_DEPLOYMENT_POLICY, **candidate}
    above_ceiling = [
        key
        for key in after
        if not _no_broader(key, after[key], _DEPLOYMENT_POLICY[key])
    ]
    if above_ceiling:
        raise PermissionError(
            "Deployment permission ceiling exceeded: "
            + ", ".join(sorted(above_ceiling))
            + ". Administrator must change protected configuration and restart."
        )
    changes = {
        key: {"before": before[key], "after": after[key]}
        for key in sorted(after)
        if before[key] != after[key]
    }
    widened = [key for key in changes if not _no_broader(key, after[key], before[key])]
    if widened and _APPROVAL_MODE == "require":
        if _APPROVAL_STORE is None:
            return {
                **dynamic_policy_snapshot(),
                "status": "approval_unavailable",
                "applied": False,
                "requested_changes": changes,
                "message": "No administrator approval directory configured; policy unchanged.",
            }
        fingerprint = hashlib.sha256(
            json.dumps(_DEPLOYMENT_POLICY, sort_keys=True).encode()
        ).hexdigest()
        approval = _APPROVAL_STORE.authorize(
            {
                "revision": _POLICY_REVISION,
                "deployment_fingerprint": fingerprint,
                "changes": changes,
                "widened_keys": widened,
            }
        )
        if approval["status"] != "approved":
            return {
                **dynamic_policy_snapshot(),
                **approval,
                "applied": False,
                "message": "Policy unchanged. Administrator approval is required before retrying.",
            }
    _SESSION_POLICY.clear()
    _SESSION_POLICY.update(candidate)
    if changes:
        _POLICY_REVISION += 1
    return {**dynamic_policy_snapshot(), "status": "applied", "applied": True}


def set_session_policy(
    policy: Mapping[str, Any] | None = None,
    *,
    preset: str | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    initialize_runtime_policy()
    with _SESSION_POLICY_LOCK:
        assert _DEPLOYMENT_POLICY is not None
        updates: dict[str, str] = {}
        if preset:
            # Presets are permission caps, intersected with operator restrictions.
            for key, value in session_policy_for_preset(preset).items():
                ceiling = _DEPLOYMENT_POLICY[key]
                updates[key] = value if _no_broader(key, value, ceiling) else ceiling
        if policy:
            _validate_policy_keys(policy)
            updates.update(
                {
                    str(key): _normalize_policy_value(str(key), value)
                    for key, value in policy.items()
                }
            )
        candidate = {} if replace else dict(_SESSION_POLICY)
        candidate.update(updates)
        return _apply_session_candidate(candidate)


def reset_session_policy(keys: list[str] | None = None) -> dict[str, Any]:
    initialize_runtime_policy()
    if keys is not None:
        _validate_policy_keys({key: "" for key in keys})

    with _SESSION_POLICY_LOCK:
        candidate = {} if keys is None else dict(_SESSION_POLICY)
        if keys is not None:
            for key in keys:
                candidate.pop(key, None)
        return _apply_session_candidate(candidate)


def dynamic_policy_snapshot() -> dict[str, Any]:
    initialize_runtime_policy()
    with _SESSION_POLICY_LOCK:
        assert _DEPLOYMENT_POLICY is not None
        return {
            "mcp_config_path": _DEPLOYMENT_CONFIG_PATH,
            "session_policy": dict(_SESSION_POLICY),
            "mcp_env": _redact_env(_DEPLOYMENT_ENV),
            "defaults": {**_FULL_PERMISSION_DEFAULTS, **_ENFORCEMENT_DEFAULTS},
            "deployment_policy": dict(_DEPLOYMENT_POLICY),
            "effective_policy": {**_DEPLOYMENT_POLICY, **_SESSION_POLICY},
            "supported_keys": supported_policy_keys(),
            "administrator_only_keys": sorted(_ADMIN_POLICY_KEYS),
            "approval_mode": _APPROVAL_MODE,
            "approval_available": _APPROVAL_STORE is not None,
            "policy_revision": _POLICY_REVISION,
            "presets": sorted(_PRESET_POLICIES),
        }
