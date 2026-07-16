#
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

import logging
import json
import os
from pathlib import Path
import tempfile

from mcp.types import TextContent

from iotdb_mcp_server.config import Config
from iotdb_mcp_server.services.json_response import payload_response
from iotdb_mcp_server.services.target_selection import (
    apply_target_registry,
    iotdb_target_response_context,
    maybe_reload_target_registry,
    select_target_config,
)
from iotdb_mcp_server.target_registry import (
    IoTDBTargetRegistry,
    mark_target_last_known_good,
    target_from_mapping,
)
from iotdb_mcp_server.target_lifecycle import (
    IoTDBTargetCandidateStore,
    candidate_target,
    same_target_identity,
    verify_target_once,
)


def _registry(config: Config) -> IoTDBTargetRegistry:
    maybe_reload_target_registry(config)
    if config.target_registry is not None:
        return config.target_registry
    registry = IoTDBTargetRegistry({})
    config.target_registry = registry
    return registry


def _targets_file(config: Config, targets_file: str | None = None) -> Path:
    resolved = (
        targets_file
        or config.targets_file
        or os.getenv("TIMESEEK_IOTDB_TARGETS_FILE", "")
    ).strip()
    if not resolved:
        raise ValueError(
            "No IoTDB targets file is configured. Pass targets_file or set "
            "TIMESEEK_IOTDB_TARGETS_FILE to persist dynamic target changes."
        )
    return Path(resolved).expanduser()


def _persist_registry(
    config: Config, registry: IoTDBTargetRegistry, targets_file: str | None
) -> None:
    path = _targets_file(config, targets_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(registry.as_dict(include_secret=True), ensure_ascii=False, indent=2)
        + "\n"
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    try:
        path.chmod(0o600)
    except OSError:
        pass
    config.targets_file = str(path)
    try:
        config.targets_file_mtime_ns = path.stat().st_mtime_ns
    except OSError:
        config.targets_file_mtime_ns = None


def _targets_payload(config: Config) -> dict[str, object]:
    registry = _registry(config)
    return {
        "default_target_id": registry.default_target_id,
        "active_target_id": config.target_id or None,
        "targets": registry.list_targets(include_secret=False),
        "selector_fields": [
            "target_id",
            "id",
            "display_name",
            "kind",
            "host",
            "port",
            "database",
            "sql_dialect",
            "user",
            "node_url",
            "node_urls",
        ],
        "dynamic_tools": [
            "prepare_iotdb_target",
            "connect_iotdb_target",
            "register_iotdb_target",
            "remove_iotdb_target",
            "set_default_iotdb_target",
            "reload_iotdb_targets",
        ],
        "targets_file": config.targets_file
        or os.getenv("TIMESEEK_IOTDB_TARGETS_FILE", ""),
    }


def register_target_tools(mcp, config: Config, logger: logging.Logger) -> None:
    """Register IoTDB target discovery tools."""
    candidate_store = IoTDBTargetCandidateStore()

    def publish_target(
        target,
        *,
        set_default: bool,
        persist: bool | None,
        targets_file: str | None,
    ):
        registry = _registry(config)
        updated = registry
        duplicate_ids = []
        for item in registry.list_targets(include_secret=True):
            existing = target_from_mapping(item)
            if existing.target_id != target.target_id and same_target_identity(
                existing, target
            ):
                duplicate_ids.append(existing.target_id)
        for duplicate_id in duplicate_ids:
            updated = updated.without_target(duplicate_id)
        make_default = (
            set_default
            or updated.default_target_id is None
            or registry.default_target_id in duplicate_ids
        )
        updated = updated.with_target(
            target,
            default_target_id=(
                target.target_id if make_default else updated.default_target_id
            ),
        )
        persist_resolved = (
            persist
            if persist is not None
            else bool(
                targets_file
                or config.targets_file
                or os.getenv("TIMESEEK_IOTDB_TARGETS_FILE", "")
            )
        )
        if persist_resolved:
            _persist_registry(config, updated, targets_file)
        apply_target_registry(
            config,
            updated,
            active_target_id=(
                target.target_id if make_default else updated.default_target_id
            ),
            close_pools=True,
        )
        return updated, duplicate_ids, persist_resolved

    def evict_on_connection_error(
        target_id: str,
        error: BaseException,
        updated: IoTDBTargetRegistry,
    ) -> None:
        apply_target_registry(
            config,
            updated,
            active_target_id=updated.default_target_id,
            close_pools=False,
        )
        if config.targets_file or os.getenv("TIMESEEK_IOTDB_TARGETS_FILE", ""):
            try:
                _persist_registry(config, updated, None)
            except Exception as persist_error:
                logger.error(
                    "Removed failed IoTDB target %s in memory but could not persist: %s",
                    target_id,
                    persist_error,
                )
        logger.error(
            "Removed IoTDB target %s after connection error %s: %s",
            target_id,
            type(error).__name__,
            error,
        )

    if config.session_manager is not None:
        config.session_manager.set_connection_error_callback(evict_on_connection_error)

    @mcp.tool()
    async def list_iotdb_targets() -> list[TextContent]:
        """List registered IoTDB targets with redacted credentials."""
        return payload_response(
            "list_iotdb_targets",
            _targets_payload(config),
            message="Registered IoTDB targets.",
        )

    @mcp.tool()
    async def describe_iotdb_target(
        target_id: str | None = None,
        target: dict[str, object] | None = None,
    ) -> list[TextContent]:
        """Describe one IoTDB target selected by id or selector fields."""
        selected_config = select_target_config(
            config, target_id=target_id, target=target
        )
        with iotdb_target_response_context(selected_config):
            return payload_response(
                "describe_iotdb_target",
                selected_config.to_target().as_dict(include_secret=False),
                message="IoTDB target description.",
            )

    @mcp.tool()
    async def prepare_iotdb_target(
        target_template: dict[str, object],
        user_confirmed_retry: bool = False,
    ) -> list[TextContent]:
        """Create a credential-free, non-routable target candidate.

        A previous failed endpoint requires an explicit user-requested retry.
        Templates are memory-only and never appear in list_iotdb_targets.
        """
        candidate = candidate_store.prepare(
            target_template,
            user_confirmed_retry=user_confirmed_retry,
        )
        return payload_response(
            "prepare_iotdb_target",
            candidate.as_dict(),
            message="IoTDB target candidate prepared; credentials are still required.",
        )

    @mcp.tool()
    async def connect_iotdb_target(
        candidate_id: str,
        username: str,
        password: str,
        set_default: bool = True,
        persist: bool | None = None,
        targets_file: str | None = None,
    ) -> list[TextContent]:
        """Authenticate once and publish only after success.

        When a targets file is configured, the verified credential is persisted
        by default so MCP and iotdb-target-cli read the same target state.
        """
        candidate = candidate_store.consume(candidate_id)
        target = candidate_target(candidate, user=username, password=password)
        try:
            verified = verify_target_once(target)
        except Exception as exc:
            candidate_store.record_failure(candidate)
            logger.error(
                "IoTDB candidate %s was deleted after connection failure: %s",
                candidate_id,
                exc,
            )
            raise ConnectionError(
                "IoTDB connection failed; the one-time candidate was deleted. "
                "Do not retry until the user explicitly requests reconnection. "
                f"Cause: {type(exc).__name__}: {exc}"
            ) from None
        updated, duplicate_ids, persisted = publish_target(
            verified,
            set_default=set_default,
            persist=persist,
            targets_file=targets_file,
        )
        return payload_response(
            "connect_iotdb_target",
            {
                "registered_target": verified.as_dict(include_secret=False),
                "replaced_target_ids": duplicate_ids,
                "registry": _targets_payload(config),
                "persisted": persisted,
            },
            message="IoTDB target authenticated and registered.",
        )

    @mcp.tool()
    async def register_iotdb_target(
        target: dict[str, object],
        set_default: bool = False,
        persist: bool | None = None,
        targets_file: str | None = None,
        user_confirmed_retry: bool = False,
    ) -> list[TextContent]:
        """Compatibility entry point that verifies credentials before registration."""
        if not isinstance(target, dict):
            raise ValueError("target must be an object.")
        username = target.get("user", target.get("username"))
        if username is None or "password" not in target:
            raise ValueError(
                "register_iotdb_target requires user and an explicitly supplied password. "
                "When credentials are unavailable, call prepare_iotdb_target and ask the user."
            )
        template = {
            key: value
            for key, value in target.items()
            if key
            not in {
                "user",
                "username",
                "password",
                "last_known_good_credential",
                "verified_at",
            }
        }
        candidate = candidate_store.prepare(
            template,
            user_confirmed_retry=user_confirmed_retry,
        )
        consumed = candidate_store.consume(candidate.candidate_id)
        parsed_target = candidate_target(
            consumed,
            user=str(username),
            password="" if target["password"] is None else str(target["password"]),
        )
        try:
            verified = verify_target_once(parsed_target)
        except Exception as exc:
            candidate_store.record_failure(consumed)
            raise ConnectionError(
                "IoTDB connection failed; no target was registered. Do not retry "
                "until the user explicitly requests reconnection. "
                f"Cause: {type(exc).__name__}: {exc}"
            ) from None
        updated, duplicate_ids, persisted = publish_target(
            verified,
            set_default=set_default,
            persist=persist,
            targets_file=targets_file,
        )
        logger.info("Authenticated and registered IoTDB target %s", verified.target_id)
        return payload_response(
            "register_iotdb_target",
            {
                "registered_target": verified.as_dict(include_secret=False),
                "replaced_target_ids": duplicate_ids,
                "registry": _targets_payload(config),
                "persisted": persisted,
            },
            message="IoTDB target authenticated and registered.",
        )

    @mcp.tool()
    async def remember_iotdb_target_credential(
        target_id: str | None = None,
        target: dict[str, object] | None = None,
        persist: bool = False,
        targets_file: str | None = None,
    ) -> list[TextContent]:
        """Record the selected target's current credential as last-known-good.

        Call this only after a connection/query has succeeded with the selected
        target. This tool does not test login and does not try fallback
        passwords.
        """
        registry = _registry(config)
        selected_target = registry.resolve_selector(
            target,
            target_id=target_id or (None if target is not None else config.target_id),
        )
        updated_target = mark_target_last_known_good(selected_target)
        updated = registry.with_target(updated_target)
        apply_target_registry(
            config,
            updated,
            active_target_id=(
                config.target_id
                if config.target_id
                in {
                    item["target_id"]
                    for item in updated.list_targets(include_secret=False)
                }
                else updated.default_target_id
            ),
            close_pools=False,
        )
        if persist:
            _persist_registry(config, updated, targets_file)
        logger.info(
            "Remembered IoTDB target credential for %s",
            updated_target.target_id,
        )
        return payload_response(
            "remember_iotdb_target_credential",
            {
                "remembered_target": updated_target.as_dict(include_secret=False),
                "registry": _targets_payload(config),
                "persisted": persist,
            },
            message="IoTDB target last-known-good credential recorded.",
        )

    @mcp.tool()
    async def remove_iotdb_target(
        target_id: str,
        persist: bool = False,
        targets_file: str | None = None,
    ) -> list[TextContent]:
        """Remove an IoTDB target from this running MCP process."""
        registry = _registry(config)
        updated = registry.without_target(target_id)
        apply_target_registry(
            config,
            updated,
            active_target_id=updated.default_target_id,
            close_pools=True,
        )
        if persist:
            _persist_registry(config, updated, targets_file)
        logger.info("Removed IoTDB target %s", target_id)
        return payload_response(
            "remove_iotdb_target",
            {
                "removed_target_id": target_id,
                "registry": _targets_payload(config),
                "persisted": persist,
            },
            message="IoTDB target removed.",
        )

    @mcp.tool()
    async def set_default_iotdb_target(
        target_id: str,
        persist: bool = False,
        targets_file: str | None = None,
    ) -> list[TextContent]:
        """Set the default IoTDB target for this running MCP process."""
        registry = _registry(config)
        updated = registry.with_default(target_id)
        apply_target_registry(
            config,
            updated,
            active_target_id=target_id,
            close_pools=False,
        )
        if persist:
            _persist_registry(config, updated, targets_file)
        logger.info("Set default IoTDB target %s", target_id)
        return payload_response(
            "set_default_iotdb_target",
            {
                "default_target_id": target_id,
                "registry": _targets_payload(config),
                "persisted": persist,
            },
            message="Default IoTDB target updated.",
        )

    @mcp.tool()
    async def reload_iotdb_targets(
        targets_file: str | None = None,
        targets_json: str | None = None,
    ) -> list[TextContent]:
        """Reload IoTDB targets from env, a targets file, or an explicit JSON string."""
        resolved_file = (
            targets_file
            or config.targets_file
            or os.getenv("TIMESEEK_IOTDB_TARGETS_FILE", "")
            or None
        )
        resolved_json = (
            targets_json
            or config.targets_json
            or os.getenv("TIMESEEK_IOTDB_TARGETS_JSON", "")
            or None
        )
        registry = IoTDBTargetRegistry.from_env(
            targets_file=resolved_file,
            targets_json=resolved_json,
        )
        mtime_ns = None
        if resolved_file:
            try:
                mtime_ns = Path(resolved_file).expanduser().stat().st_mtime_ns
            except OSError:
                mtime_ns = None
            config.targets_file = resolved_file
        if resolved_json:
            config.targets_json = resolved_json
        apply_target_registry(
            config,
            registry,
            targets_file_mtime_ns=mtime_ns,
            close_pools=True,
        )
        logger.info("Reloaded IoTDB targets")
        return payload_response(
            "reload_iotdb_targets",
            _targets_payload(config),
            message="IoTDB targets reloaded.",
        )
