from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
import uuid
from typing import Any, Mapping

from iotdb.Session import Session

from iotdb_mcp_server.target_registry import (
    IoTDBTarget,
    mark_target_last_known_good,
    target_from_mapping,
)


_SECRET_FIELDS = {
    "user",
    "username",
    "password",
    "IOTDB_USER",
    "IOTDB_PASSWORD",
    "last_known_good_credential",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _endpoint_key(spec: Mapping[str, Any]) -> str:
    host = str(spec.get("host") or "127.0.0.1").strip().lower()
    port = int(spec.get("port") or 6667)
    dialect = str(spec.get("sql_dialect") or "tree").strip().lower()
    return f"{host}:{port}:{dialect}"


@dataclass(frozen=True)
class IoTDBTargetCandidate:
    candidate_id: str
    spec: dict[str, Any]
    created_at: str
    expires_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "template": dict(self.spec),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "credentials_required": True,
            "registered": False,
        }


class IoTDBTargetCandidateStore:
    def __init__(self, ttl_seconds: int = 900, retry_guard_seconds: int = 900) -> None:
        self.ttl_seconds = ttl_seconds
        self.retry_guard_seconds = retry_guard_seconds
        self._candidates: dict[str, IoTDBTargetCandidate] = {}
        self._failed_endpoints: dict[str, datetime] = {}
        self._lock = threading.Lock()

    def _prune(self, now: datetime) -> None:
        self._candidates = {
            candidate_id: candidate
            for candidate_id, candidate in self._candidates.items()
            if datetime.fromisoformat(candidate.expires_at) > now
        }
        self._failed_endpoints = {
            endpoint: expires_at
            for endpoint, expires_at in self._failed_endpoints.items()
            if expires_at > now
        }

    def prepare(
        self,
        spec: Mapping[str, Any],
        *,
        user_confirmed_retry: bool = False,
    ) -> IoTDBTargetCandidate:
        if not isinstance(spec, Mapping):
            raise ValueError("target template must be an object.")
        secret_fields = sorted(_SECRET_FIELDS.intersection(spec))
        if secret_fields:
            raise ValueError(
                "Target templates cannot contain credentials. Pass username/password "
                "only to connect_iotdb_target after the user provides them."
            )
        clean = {str(key): value for key, value in spec.items()}
        clean.setdefault("host", "127.0.0.1")
        clean.setdefault("port", 6667)
        clean.setdefault("sql_dialect", "tree")
        # Validate endpoint and dialect without publishing a target.
        target_from_mapping({**clean, "user": "candidate", "password": ""})

        now = _now()
        endpoint = _endpoint_key(clean)
        with self._lock:
            self._prune(now)
            if endpoint in self._failed_endpoints and not user_confirmed_retry:
                raise PermissionError(
                    "A previous connection attempt for this endpoint failed. "
                    "Create a new candidate only after the user explicitly requests "
                    "reconnection, then pass user_confirmed_retry=true."
                )
            candidate = IoTDBTargetCandidate(
                candidate_id=f"candidate-{uuid.uuid4().hex}",
                spec=clean,
                created_at=_iso(now),
                expires_at=_iso(now + timedelta(seconds=self.ttl_seconds)),
            )
            self._candidates[candidate.candidate_id] = candidate
            return candidate

    def consume(self, candidate_id: str) -> IoTDBTargetCandidate:
        now = _now()
        with self._lock:
            self._prune(now)
            try:
                return self._candidates.pop(candidate_id)
            except KeyError as exc:
                raise KeyError(
                    "IoTDB target candidate is missing, expired, or already consumed."
                ) from exc

    def record_failure(self, candidate: IoTDBTargetCandidate) -> None:
        with self._lock:
            self._failed_endpoints[_endpoint_key(candidate.spec)] = _now() + timedelta(
                seconds=self.retry_guard_seconds
            )


def canonical_target_id(spec: Mapping[str, Any], user: str) -> str:
    explicit = str(spec.get("target_id") or spec.get("id") or "").strip()
    if explicit:
        return explicit
    identity = {
        "host": str(spec.get("host") or "127.0.0.1").strip().lower(),
        "port": int(spec.get("port") or 6667),
        "user": user.strip().lower(),
        "sql_dialect": str(spec.get("sql_dialect") or "tree").strip().lower(),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"iotdb-{digest}"


def candidate_target(
    candidate: IoTDBTargetCandidate,
    *,
    user: str,
    password: str,
) -> IoTDBTarget:
    if not user.strip():
        raise ValueError("IoTDB username cannot be empty.")
    spec = dict(candidate.spec)
    spec["target_id"] = canonical_target_id(spec, user)
    spec["user"] = user
    spec["password"] = password
    return target_from_mapping(spec)


def verify_target_once(target: IoTDBTarget) -> IoTDBTarget:
    """Authenticate once and complete one read-only operation before publishing."""
    session = Session(
        target.host,
        target.port,
        target.user,
        target.password,
        target.fetch_size,
        target.timezone,
        enable_redirection=False,
        use_ssl=target.use_ssl,
        ca_certs=target.ca_certs or None,
        connection_timeout_in_ms=target.connection_timeout_in_ms,
    )
    session.sql_dialect = target.sql_dialect
    session.database = target.database
    try:
        session.open(target.enable_compression)
        result = session.execute_query_statement("SHOW VERSION")
        close_result = getattr(result, "close_operation_handle", None)
        if close_result is not None:
            close_result()
    finally:
        try:
            session.close()
        except Exception:
            pass
    return mark_target_last_known_good(target, password_source="verified_connection")


def same_target_identity(left: IoTDBTarget, right: IoTDBTarget) -> bool:
    return (
        left.host.strip().lower(),
        left.port,
        left.user.strip().lower(),
        left.sql_dialect,
    ) == (
        right.host.strip().lower(),
        right.port,
        right.user.strip().lower(),
        right.sql_dialect,
    )
