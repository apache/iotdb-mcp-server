from __future__ import annotations

import copy
from contextlib import contextmanager
import os
from pathlib import Path
import time
from typing import Any, Iterator, Mapping

from iotdb.Session import Session
from iotdb.table_session import TableSession, TableSessionConfig

from iotdb_mcp_server.config import Config
from iotdb_mcp_server.session_manager import (
    create_table_session_pool,
    create_tree_session_pool,
)
from iotdb_mcp_server.target_registry import IoTDBTargetRegistry
from iotdb_mcp_server.services.json_response import (
    reset_iotdb_target_context,
    set_iotdb_target_context,
)


TargetSelector = str | Mapping[str, Any] | None
_AINODE_PROBE_CACHE: dict[str, dict[str, Any]] = {}
_DEFAULT_AINODE_PROBE_TTL_SECONDS = 10.0


def _copy_target_fields(config: Config, source: Config) -> None:
    for field_name in (
        "host",
        "port",
        "user",
        "password",
        "database",
        "sql_dialect",
        "timezone",
        "export_path",
        "target_id",
        "display_name",
        "target_kind",
        "node_urls",
        "iotdb_home",
        "use_ssl",
        "ca_certs",
        "connection_timeout_in_ms",
        "enable_redirection",
        "enable_compression",
        "fetch_size",
        "max_retry",
        "max_pool_size",
        "tree_wait_timeout_in_ms",
        "table_wait_timeout_in_ms",
        "target_fingerprint",
        "policy",
        "last_known_good_credential",
        "verified_at",
    ):
        setattr(config, field_name, getattr(source, field_name))


def apply_target_registry(
    config: Config,
    registry: IoTDBTargetRegistry,
    *,
    active_target_id: str | None = None,
    targets_file_mtime_ns: int | None = None,
    close_pools: bool = True,
) -> Config:
    if not registry.list_targets(include_secret=False):
        config.target_registry = registry
        config.target_id = ""
        config.display_name = ""
        config.target_kind = ""
        config.host = ""
        config.port = 0
        config.node_urls = ()
        config.user = ""
        config.password = ""
        config.database = ""
        config.target_fingerprint = ""
        config.policy = {}
        config.last_known_good_credential = {}
        config.verified_at = ""
        if config.session_manager is not None:
            config.session_manager.update_registry(registry, close_pools=close_pools)
        return config
    active_target = registry.resolve(active_target_id or registry.default_target_id)
    selected = Config.from_target(
        active_target,
        target_registry=registry,
        session_manager=config.session_manager,
    )
    _copy_target_fields(config, selected)
    config.target_registry = registry
    if targets_file_mtime_ns is not None:
        config.targets_file_mtime_ns = targets_file_mtime_ns
    if config.session_manager is not None:
        update = getattr(config.session_manager, "update_registry", None)
        if update is not None:
            update(registry, close_pools=close_pools)
        else:
            config.session_manager.registry = registry
    return config


def maybe_reload_target_registry(config: Config) -> bool:
    targets_file = (config.targets_file or os.getenv("TIMESEEK_IOTDB_TARGETS_FILE", "")).strip()
    if not targets_file:
        return False
    path = Path(targets_file).expanduser()
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return False
    if config.targets_file_mtime_ns == mtime_ns:
        return False
    registry = IoTDBTargetRegistry.from_env(
        targets_file=str(path),
        targets_json=config.targets_json or os.getenv("TIMESEEK_IOTDB_TARGETS_JSON", "") or None,
    )
    apply_target_registry(
        config,
        registry,
        targets_file_mtime_ns=mtime_ns,
        close_pools=True,
    )
    return True


def _string_fields(row: Any) -> list[str]:
    fields = row.get_fields() if hasattr(row, "get_fields") else row
    if isinstance(fields, (list, tuple)):
        return [str(field) for field in fields]
    return [str(fields)]


def _row_dict(columns: list[str], row: list[str]) -> dict[str, str] | dict[str, list[str]]:
    if len(columns) == len(row):
        return dict(zip(columns, row))
    return {"values": row}


def _has_running_ainode(columns: list[str], rows: list[list[str]]) -> bool:
    status_index = next(
        (index for index, column in enumerate(columns) if column.strip().lower() == "status"),
        None,
    )
    if status_index is not None:
        return any(
            len(row) > status_index and row[status_index].strip().lower() == "running"
            for row in rows
        )
    return any(any(value.strip().lower() == "running" for value in row) for row in rows)


def _read_dataset(res: Any) -> tuple[list[str], list[list[str]]]:
    columns = [str(column) for column in res.get_column_names()]
    rows: list[list[str]] = []
    while res.has_next():
        rows.append(_string_fields(res.next()))
    return columns, rows


def _ainode_probe_ttl_seconds() -> float:
    raw = os.getenv("TIMESEEK_AINODE_PROBE_TTL_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_AINODE_PROBE_TTL_SECONDS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_AINODE_PROBE_TTL_SECONDS


def _ainode_probe_cache_key(config: Config) -> str:
    try:
        return config.to_target().auth_fingerprint()
    except Exception:
        return config.target_fingerprint or f"{config.target_id}:{config.host}:{config.port}"


def _cached_ainode_probe_result(
    entry: dict[str, Any], age_seconds: float, ttl_seconds: float
) -> dict[str, Any]:
    result = copy.deepcopy(entry["result"])
    source = result.get("ainode_availability_source")
    if isinstance(source, dict):
        source["cached"] = True
        source["cache_age_ms"] = int(max(0.0, age_seconds) * 1000)
        source["cache_ttl_seconds"] = ttl_seconds
    return result


def _last_success_for_stale_result(
    cache_key: str, error_source: dict[str, Any]
) -> dict[str, Any] | None:
    cached = _AINODE_PROBE_CACHE.get(cache_key)
    if not cached:
        return None
    age_seconds = time.monotonic() - float(cached["monotonic"])
    cached_result = cached["result"]
    source = {
        **error_source,
        "stale_from_last_success": True,
        "cache_age_ms": int(max(0.0, age_seconds) * 1000),
        "last_success": copy.deepcopy(
            cached_result.get("ainode_availability_source")
        ),
    }
    return {
        "ainode_available": cached_result.get("ainode_available", "unknown"),
        "ainode_availability_source": source,
    }


def _open_ainode_probe_session(config: Config):
    """Open a short-lived direct session for AINode discovery.

    This intentionally avoids the shared SessionPool used by business tools.
    The IoTDB Python SessionPool requires `put_back()`, while many call sites
    close raw sessions; a one-session probe pool can therefore time out under
    repeated response annotation. A direct session keeps probe health isolated.
    """
    if config.sql_dialect == "tree":
        session = Session.init_from_node_urls(
            list(config.node_urls),
            config.user,
            config.password,
            config.fetch_size,
            config.timezone,
            enable_redirection=config.enable_redirection,
            use_ssl=config.use_ssl,
            ca_certs=config.ca_certs or None,
            connection_timeout_in_ms=config.connection_timeout_in_ms,
        )
        session.sql_dialect = "tree"
        session.database = config.database
        session.open(config.enable_compression)
        return session

    return TableSession(
        TableSessionConfig(
            node_urls=list(config.node_urls),
            username=config.user,
            password=config.password,
            database=config.database or None,
            fetch_size=config.fetch_size,
            time_zone=config.timezone,
            enable_redirection=config.enable_redirection,
            enable_compression=config.enable_compression,
            use_ssl=config.use_ssl,
            ca_certs=config.ca_certs or None,
            connection_timeout_in_ms=config.connection_timeout_in_ms,
        )
    )


def _probe_ainode_availability(config: Config) -> dict[str, Any]:
    session = None
    cache_key = _ainode_probe_cache_key(config)
    ttl_seconds = _ainode_probe_ttl_seconds()
    cached = _AINODE_PROBE_CACHE.get(cache_key)
    if cached and ttl_seconds > 0:
        age_seconds = time.monotonic() - float(cached["monotonic"])
        if age_seconds <= ttl_seconds:
            return _cached_ainode_probe_result(cached, age_seconds, ttl_seconds)

    try:
        session = _open_ainode_probe_session(config)
        res = session.execute_query_statement("SHOW AINODES")
        columns, raw_rows = _read_dataset(res)
        running_count = sum(
            1
            for row in raw_rows
            if _has_running_ainode(columns, [row])
        )
        result = {
            "ainode_available": "yes" if running_count > 0 else "no",
            "ainode_availability_source": {
                "method": "SHOW AINODES",
                "ok": True,
                "columns": columns,
                "rows": [_row_dict(columns, row) for row in raw_rows],
                "row_count": len(raw_rows),
                "running_count": running_count,
            },
        }
        _AINODE_PROBE_CACHE[cache_key] = {
            "monotonic": time.monotonic(),
            "result": copy.deepcopy(result),
        }
        return result
    except Exception as exc:
        error_source = {
            "method": "SHOW AINODES",
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        stale = _last_success_for_stale_result(cache_key, error_source)
        if stale:
            return stale
        return {
            "ainode_available": "unknown",
            "ainode_availability_source": error_source,
        }
    finally:
        if session is not None:
            close = getattr(session, "close", None)
            if close is not None:
                close()


def _target_payload(config: Config) -> dict[str, Any]:
    payload = {
        "target_id": config.target_id,
        "display_name": config.display_name,
        "kind": config.target_kind,
        "host": config.host,
        "port": config.port,
        "node_urls": list(config.node_urls),
        "database": config.database,
        "sql_dialect": config.sql_dialect,
        "timezone": config.timezone,
        "policy": dict(config.policy),
        "fingerprint": config.target_fingerprint,
        "ainode_available": "unknown",
        "ainode_availability_source": {"method": "not_probed", "ok": None},
    }
    return payload


def select_target_config(
    config: Config,
    target_id: str | None = None,
    target: TargetSelector = None,
    required_sql_dialect: str | None = None,
    tool_name: str | None = None,
) -> Config:
    maybe_reload_target_registry(config)
    registry = config.target_registry
    if registry is None:
        if target_id and target_id != config.target_id:
            raise KeyError(
                f"IoTDB target '{target_id}' is not registered in this MCP server."
            )
        if isinstance(target, str) and target != config.target_id:
            raise KeyError(
                f"IoTDB target '{target}' is not registered in this MCP server."
            )
        if isinstance(target, Mapping):
            requested_id = target.get("target_id") or target.get("id") or target.get("name")
            if requested_id and str(requested_id) != config.target_id:
                raise KeyError(
                    f"IoTDB target '{requested_id}' is not registered in this MCP server."
                )
            candidate = _target_payload(config)
            candidate["target_kind"] = candidate["kind"]
            candidate["dialect"] = candidate["sql_dialect"]
            for key in (
                "display_name",
                "kind",
                "target_kind",
                "host",
                "port",
                "database",
                "sql_dialect",
                "dialect",
            ):
                expected = target.get(key)
                if expected is not None and str(candidate[key]) != str(expected):
                    raise KeyError(
                        "IoTDB target selector does not match the active target."
                    )
            endpoint = target.get("node_url") or target.get("endpoint")
            if endpoint and str(endpoint).strip() not in config.node_urls:
                raise KeyError(
                    "IoTDB target selector does not match the active target."
                )
            raw_node_urls = (
                target.get("node_urls")
                or target.get("endpoints")
                or target.get("nodes")
            )
            if raw_node_urls is not None:
                if isinstance(raw_node_urls, (list, tuple)):
                    requested_node_urls = [
                        str(item).strip() for item in raw_node_urls if str(item).strip()
                    ]
                else:
                    requested_node_urls = [
                        item.strip()
                        for item in str(raw_node_urls).split(",")
                        if item.strip()
                    ]
                if not all(item in config.node_urls for item in requested_node_urls):
                    raise KeyError(
                        "IoTDB target selector does not match the active target."
                    )
        selected = config
    else:
        selected_target = registry.resolve_selector(
            target,
            target_id=target_id or (None if target is not None else config.target_id),
        )
        selected = Config.from_target(
            selected_target,
            target_registry=config.target_registry,
            session_manager=config.session_manager,
        )
        # Nested pool helpers select again. Preserve the reload watermark so a
        # derived explicit-target config is not reset to the registry default.
        selected.targets_file = config.targets_file
        selected.targets_json = config.targets_json
        selected.targets_file_mtime_ns = config.targets_file_mtime_ns

    if required_sql_dialect and selected.sql_dialect != required_sql_dialect:
        owner = f"{tool_name} " if tool_name else ""
        raise ValueError(
            f"{owner}requires {required_sql_dialect} SQL dialect, but IoTDB target "
            f"'{selected.target_id}' uses {selected.sql_dialect}."
        )
    return selected


def tree_session_pool(
    config: Config,
    target_id: str | None = None,
    target: TargetSelector = None,
    max_pool_size: int | None = None,
    wait_timeout_in_ms: int | None = None,
    tool_name: str | None = None,
):
    selected = select_target_config(
        config,
        target_id=target_id,
        target=target,
        required_sql_dialect="tree",
        tool_name=tool_name,
    )
    if selected.session_manager is not None:
        return selected, selected.session_manager.tree_pool(
            selected.target_id,
            max_pool_size=max_pool_size,
            wait_timeout_in_ms=wait_timeout_in_ms,
        )
    return selected, create_tree_session_pool(
        selected,
        max_pool_size=max_pool_size,
        wait_timeout_in_ms=wait_timeout_in_ms,
    )


def table_session_pool(
    config: Config,
    target_id: str | None = None,
    target: TargetSelector = None,
    max_pool_size: int | None = None,
    database: str | None = None,
    wait_timeout_in_ms: int | None = None,
    tool_name: str | None = None,
):
    selected = select_target_config(
        config,
        target_id=target_id,
        target=target,
        required_sql_dialect="table",
        tool_name=tool_name,
    )
    if selected.session_manager is not None:
        return selected, selected.session_manager.table_pool(
            selected.target_id,
            max_pool_size=max_pool_size,
            database=database,
            wait_timeout_in_ms=wait_timeout_in_ms,
        )
    return selected, create_table_session_pool(
        selected,
        max_pool_size=max_pool_size,
        database=database,
        wait_timeout_in_ms=wait_timeout_in_ms,
    )


@contextmanager
def iotdb_target_response_context(config: Config) -> Iterator[None]:
    token = set_iotdb_target_context(_target_payload(config))
    try:
        yield
    finally:
        reset_iotdb_target_context(token)
