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

from typing import Any, Callable, Mapping

from iotdb.SessionPool import PoolConfig, SessionPool
from iotdb.table_session import TableSession
from iotdb.table_session_pool import TableSessionPoolConfig
from iotdb.utils.exception import IoTDBConnectionException

from iotdb_mcp_server.target_registry import (
    IoTDBTarget,
    IoTDBTargetRegistry,
    merge_target,
    target_from_mapping,
)


def target_from_config(config: Any) -> IoTDBTarget:
    if isinstance(config, IoTDBTarget):
        return config
    if hasattr(config, "to_target"):
        return config.to_target()

    keys = (
        "target_id",
        "display_name",
        "target_kind",
        "kind",
        "host",
        "port",
        "node_urls",
        "user",
        "password",
        "database",
        "sql_dialect",
        "timezone",
        "export_path",
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
        "policy",
        "last_known_good_credential",
        "verified_at",
    )
    payload: dict[str, Any] = {}
    for key in keys:
        if hasattr(config, key):
            payload[key] = getattr(config, key)
    if "target_kind" in payload and "kind" not in payload:
        payload["kind"] = payload["target_kind"]
    return target_from_mapping(payload)


def _target_with_overrides(
    config_or_target: Any,
    overrides: Mapping[str, Any] | None = None,
) -> IoTDBTarget:
    target = target_from_config(config_or_target)
    clean_overrides = {
        key: value for key, value in (overrides or {}).items() if value is not None
    }
    if not clean_overrides:
        return target
    return merge_target(target, clean_overrides)


def make_tree_pool_config(target: IoTDBTarget) -> PoolConfig:
    return PoolConfig(
        host=target.host,
        port=target.port,
        node_urls=None,
        user_name=target.user,
        password=target.password,
        fetch_size=target.fetch_size,
        time_zone=target.timezone,
        max_retry=target.max_retry,
        enable_compression=target.enable_compression,
        enable_redirection=target.enable_redirection,
        use_ssl=target.use_ssl,
        ca_certs=target.ca_certs or None,
        connection_timeout_in_ms=target.connection_timeout_in_ms,
    )


def make_table_pool_config(
    target: IoTDBTarget, database: str | None = None
) -> TableSessionPoolConfig:
    resolved_database = database if database is not None else target.database
    return TableSessionPoolConfig(
        node_urls=list(target.node_urls),
        username=target.user,
        password=target.password,
        max_pool_size=target.max_pool_size,
        database=resolved_database or None,
        fetch_size=target.fetch_size,
        time_zone=target.timezone,
        enable_redirection=target.enable_redirection,
        enable_compression=target.enable_compression,
        wait_timeout_in_ms=target.table_wait_timeout_in_ms,
        max_retry=target.max_retry,
        use_ssl=target.use_ssl,
        ca_certs=target.ca_certs or None,
        connection_timeout_in_ms=target.connection_timeout_in_ms,
    )


def create_tree_session_pool(
    config_or_target: Any,
    max_pool_size: int | None = None,
    wait_timeout_in_ms: int | None = None,
) -> SessionPool:
    target = _target_with_overrides(
        config_or_target,
        {
            "max_pool_size": max_pool_size,
            "tree_wait_timeout_in_ms": wait_timeout_in_ms,
        },
    )
    return SessionPool(
        make_tree_pool_config(target),
        target.max_pool_size,
        target.tree_wait_timeout_in_ms,
    )


def create_table_session_pool(
    config_or_target: Any,
    max_pool_size: int | None = None,
    database: str | None = None,
    wait_timeout_in_ms: int | None = None,
) -> Any:
    target = _target_with_overrides(
        config_or_target,
        {
            "max_pool_size": max_pool_size,
            "database": database,
            "table_wait_timeout_in_ms": wait_timeout_in_ms,
        },
    )
    return _SingleEndpointTableSessionPool(target)


class _SingleEndpointTableSessionPool:
    """TableSessionPool equivalent that opens only the target's primary endpoint."""

    def __init__(self, target: IoTDBTarget) -> None:
        self.database = target.database or None
        self._session_pool = SessionPool(
            make_tree_pool_config(target),
            target.max_pool_size,
            target.table_wait_timeout_in_ms,
        )
        self._session_pool.sql_dialect = "table"
        self._session_pool.database = self.database

    def get_session(self) -> TableSession:
        return TableSession(None, session_pool=self._session_pool)

    @staticmethod
    def discard_session(table_session: TableSession) -> None:
        """Close a borrowed raw Session without returning it to its pool."""
        raw_session = getattr(table_session, "_TableSession__session", None)
        if raw_session is None:
            raise RuntimeError(
                "Cannot discard TableSession: underlying Session is unavailable."
            )
        raw_session.close()

    def close(self) -> None:
        self._session_pool.close()


def is_iotdb_connection_error(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TimeoutError):
            return False
        if isinstance(current, (IoTDBConnectionException, ConnectionError, OSError)):
            return True
        message = str(current).lower()
        if any(
            marker in message
            for marker in (
                "801: authentication failed",
                "status code 801",
                "822:",
                "status code 822",
            )
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


class _ManagedSession:
    def __init__(
        self,
        session: Any,
        on_connection_error: Callable[[BaseException], None],
        release_session: Callable[[Any], None] | None = None,
        discard_session: Callable[[Any], None] | None = None,
    ):
        self._session = session
        self._on_connection_error = on_connection_error
        self._release_session = release_session
        self._discard_session = discard_session
        self._broken = False
        self._closed = False

    def _discard(self) -> bool:
        if self._discard_session is None:
            return False
        try:
            self._discard_session(self._session)
        except Exception:
            # Discard is best-effort cleanup. It must not replace either the
            # database error or a successful result from another pool lease.
            pass
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self._broken and self._discard():
            return

        if self._release_session is None:
            try:
                self._session.close()
            except ConnectionError:
                if not self._discard():
                    raise
            return

        if not self._broken:
            try:
                self._release_session(self._session)
            except ConnectionError:
                if not self._discard():
                    raise
            return

        try:
            self._session.close()
        finally:
            try:
                self._release_session(self._session)
            except ConnectionError:
                # The connection-error callback may already have closed and
                # evicted the pool. The broken session itself is closed above.
                pass

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._session, name)
        if not callable(attribute):
            return attribute

        def managed_call(*args: Any, **kwargs: Any) -> Any:
            try:
                return attribute(*args, **kwargs)
            except Exception as exc:
                if is_iotdb_connection_error(exc):
                    self._broken = True
                    self._on_connection_error(exc)
                raise

        return managed_call


class _ManagedPool:
    def __init__(
        self,
        pool: Any,
        on_connection_error: Callable[[BaseException], None],
        release_session: Callable[[Any], None] | None = None,
        discard_session: Callable[[Any], None] | None = None,
    ):
        self._pool = pool
        self._on_connection_error = on_connection_error
        self._release_session = release_session
        self._discard_session = discard_session

    def get_session(self) -> _ManagedSession:
        try:
            session = self._pool.get_session()
        except Exception as exc:
            if is_iotdb_connection_error(exc):
                self._on_connection_error(exc)
            raise
        return _ManagedSession(
            session,
            self._on_connection_error,
            release_session=self._release_session,
            discard_session=self._discard_session,
        )

    def close(self) -> None:
        self._pool.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._pool, name)


class IoTDBSessionManager:
    """Connection-pool manager keyed by IoTDB target and auth fingerprint."""

    def __init__(self, registry: IoTDBTargetRegistry) -> None:
        self.registry = registry
        self._tree_pools: dict[tuple[str, str, int, int], Any] = {}
        self._table_pools: dict[tuple[str, str, int, int, str], Any] = {}
        self._connection_error_callback: Callable[
            [str, BaseException, IoTDBTargetRegistry], None
        ] | None = None

    @classmethod
    def from_env(cls) -> "IoTDBSessionManager":
        return cls(IoTDBTargetRegistry.from_env())

    def tree_pool(
        self,
        target_id: str | None = None,
        max_pool_size: int | None = None,
        wait_timeout_in_ms: int | None = None,
    ) -> SessionPool:
        target = self.registry.resolve(target_id)
        target = _target_with_overrides(
            target,
            {
                "max_pool_size": max_pool_size,
                "tree_wait_timeout_in_ms": wait_timeout_in_ms,
            },
        )
        key = (
            target.target_id,
            target.auth_fingerprint(),
            target.max_pool_size,
            target.tree_wait_timeout_in_ms,
        )
        if key not in self._tree_pools:
            pool: Any = create_tree_session_pool(target)
            if self._connection_error_callback is not None:
                pool = _ManagedPool(
                    pool,
                    lambda error: self._evict_failed_target(target.target_id, error),
                    release_session=pool.put_back,
                    discard_session=lambda session: session.close(),
                )
            self._tree_pools[key] = pool
        return self._tree_pools[key]

    def table_pool(
        self,
        target_id: str | None = None,
        max_pool_size: int | None = None,
        database: str | None = None,
        wait_timeout_in_ms: int | None = None,
    ) -> Any:
        target = self.registry.resolve(target_id)
        target = _target_with_overrides(
            target,
            {
                "max_pool_size": max_pool_size,
                "database": database,
                "table_wait_timeout_in_ms": wait_timeout_in_ms,
            },
        )
        key = (
            target.target_id,
            target.auth_fingerprint(),
            target.max_pool_size,
            target.table_wait_timeout_in_ms,
            target.database,
        )
        if key not in self._table_pools:
            pool = create_table_session_pool(target)
            if self._connection_error_callback is not None:
                pool = _ManagedPool(
                    pool,
                    lambda error: self._evict_failed_target(target.target_id, error),
                    discard_session=pool.discard_session,
                )
            self._table_pools[key] = pool
        return self._table_pools[key]

    def close(self) -> None:
        for pool in [*self._tree_pools.values(), *self._table_pools.values()]:
            close = getattr(pool, "close", None)
            if close is not None:
                close()
        self._tree_pools.clear()
        self._table_pools.clear()

    def update_registry(
        self, registry: IoTDBTargetRegistry, *, close_pools: bool = True
    ) -> None:
        if close_pools:
            self.close()
        self.registry = registry

    def close_target_pools(self, target_id: str) -> None:
        tree_pools = [
            pool for key, pool in self._tree_pools.items() if key[0] == target_id
        ]
        table_pools = [
            pool for key, pool in self._table_pools.items() if key[0] == target_id
        ]
        self._tree_pools = {
            key: pool for key, pool in self._tree_pools.items() if key[0] != target_id
        }
        self._table_pools = {
            key: pool for key, pool in self._table_pools.items() if key[0] != target_id
        }
        for pool in [*tree_pools, *table_pools]:
            try:
                pool.close()
            except Exception:
                pass

    def set_connection_error_callback(
        self,
        callback: Callable[[str, BaseException, IoTDBTargetRegistry], None] | None,
    ) -> None:
        self._connection_error_callback = callback

    def _evict_failed_target(self, target_id: str, error: BaseException) -> None:
        try:
            updated = self.registry.without_target(target_id)
        except KeyError:
            return
        self.registry = updated
        self.close_target_pools(target_id)
        if self._connection_error_callback is not None:
            self._connection_error_callback(target_id, error, updated)
