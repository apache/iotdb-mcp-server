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

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Iterable
import uuid

_RESULT_ID_PATTERN = re.compile(r"^res_[0-9]{8}T[0-9]{6}Z_[a-f0-9]{16}$")
_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,180}$")
_MANIFEST_NAME = "manifest.json"
_STANDALONE_OWNER = "standalone"


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _bounded_int(value: Any, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    if value is None or str(value).strip() == "":
        resolved = default
    else:
        resolved = int(value)
    resolved = max(minimum, resolved)
    if maximum is not None:
        resolved = min(maximum, resolved)
    return resolved


def _safe_owner_session_id(value: str | None) -> str:
    raw = (
        value
        or os.getenv("TIMESEEK_MCP_SESSION_ID", "").strip()
        or os.getenv("TIMESEEK_CODEX_SESSION_ID", "").strip()
        or _STANDALONE_OWNER
    )
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(raw)).strip("_")
    if not cleaned:
        cleaned = _STANDALONE_OWNER
    cleaned = cleaned[:180]
    if not _OWNER_PATTERN.fullmatch(cleaned):
        raise ValueError("Invalid owner_session_id.")
    return cleaned


@dataclass(frozen=True)
class ResultStoreSettings:
    preview_rows: int = 50
    page_size_rows: int = 500
    max_page_rows: int = 5000
    shard_rows: int = 10000
    shard_bytes: int = 8 * 1024 * 1024
    ttl_seconds: int = 24 * 60 * 60
    max_session_results: int = 100
    max_session_bytes: int = 512 * 1024 * 1024
    max_global_results: int = 1000
    max_global_bytes: int = 2 * 1024 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "ResultStoreSettings":
        return cls(
            preview_rows=_env_int("TIMESEEK_RESULT_PREVIEW_ROWS", 50, minimum=0),
            page_size_rows=_env_int("TIMESEEK_RESULT_PAGE_SIZE_ROWS", 500),
            max_page_rows=_env_int("TIMESEEK_RESULT_MAX_PAGE_ROWS", 5000),
            shard_rows=_env_int("TIMESEEK_RESULT_SHARD_ROWS", 10000),
            shard_bytes=_env_int("TIMESEEK_RESULT_SHARD_BYTES", 8 * 1024 * 1024),
            ttl_seconds=_env_int(
                "TIMESEEK_RESULT_TTL_SECONDS", 24 * 60 * 60, minimum=0
            ),
            max_session_results=_env_int(
                "TIMESEEK_RESULT_MAX_SESSION_RESULTS", 100, minimum=0
            ),
            max_session_bytes=_env_int(
                "TIMESEEK_RESULT_MAX_SESSION_BYTES", 512 * 1024 * 1024, minimum=0
            ),
            max_global_results=_env_int(
                "TIMESEEK_RESULT_MAX_GLOBAL_RESULTS", 1000, minimum=0
            ),
            max_global_bytes=_env_int(
                "TIMESEEK_RESULT_MAX_GLOBAL_BYTES",
                2 * 1024 * 1024 * 1024,
                minimum=0,
            ),
        )


@dataclass(frozen=True)
class StoredCsvResult:
    result_id: str
    owner_session_id: str
    columns: list[str]
    preview_rows: list[str]
    row_count: int
    byte_count: int
    page_size_rows: int
    manifest_path: str
    shard_count: int


@dataclass(frozen=True)
class ResultStoreEntry:
    result_id: str
    owner_session_id: str
    tool: str
    row_count: int
    byte_count: int
    storage_bytes: int
    shard_count: int
    created_at: datetime
    last_accessed_at: datetime
    manifest_path: Path
    result_dir: Path
    legacy: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "owner_session_id": self.owner_session_id,
            "tool": self.tool,
            "row_count": self.row_count,
            "byte_count": self.byte_count,
            "storage_bytes": self.storage_bytes,
            "shard_count": self.shard_count,
            "created_at": self.created_at.isoformat(),
            "last_accessed_at": self.last_accessed_at.isoformat(),
            "manifest_path": str(self.manifest_path),
            "legacy": self.legacy,
        }


class FileResultStore:
    """File-backed store for large MCP result sets.

    Rows are stored as JSON lines so callers can fetch a page without parsing a
    whole CSV file. The public row representation intentionally remains the
    existing CSV row string used by the IoTDB MCP response schema.
    """

    def __init__(
        self,
        root_dir: str | os.PathLike[str],
        settings: ResultStoreSettings | None = None,
    ) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.settings = settings or ResultStoreSettings.from_env()

    def _ensure_root(self) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def _new_result_id(self) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"res_{timestamp}_{uuid.uuid4().hex[:16]}"

    def _session_dir(self, owner_session_id: str | None = None) -> Path:
        owner = _safe_owner_session_id(owner_session_id)
        session_dir = (self.root_dir / "sessions" / owner).resolve()
        sessions_root = (self.root_dir / "sessions").resolve()
        if not str(session_dir).startswith(str(sessions_root) + os.sep):
            raise ValueError("Invalid owner_session_id path.")
        return session_dir

    def _legacy_result_dir(self, result_id: str) -> Path:
        if not _RESULT_ID_PATTERN.fullmatch(result_id):
            raise ValueError("Invalid result_id.")
        result_dir = (self.root_dir / result_id).resolve()
        if not str(result_dir).startswith(str(self.root_dir) + os.sep):
            raise ValueError("Invalid result_id path.")
        return result_dir

    def _result_dir(self, result_id: str, owner_session_id: str | None = None) -> Path:
        if not _RESULT_ID_PATTERN.fullmatch(result_id):
            raise ValueError("Invalid result_id.")
        session_dir = self._session_dir(owner_session_id)
        result_dir = (session_dir / result_id).resolve()
        if not str(result_dir).startswith(str(session_dir) + os.sep):
            raise ValueError("Invalid result_id path.")
        return result_dir

    def _existing_result_dir(
        self, result_id: str, owner_session_id: str | None = None
    ) -> Path:
        result_dir = self._result_dir(result_id, owner_session_id)
        if (result_dir / _MANIFEST_NAME).is_file():
            return result_dir
        legacy_dir = self._legacy_result_dir(result_id)
        if (legacy_dir / _MANIFEST_NAME).is_file():
            return legacy_dir
        raise FileNotFoundError(f"Result '{result_id}' was not found.")

    def write_csv_result(
        self,
        *,
        tool: str,
        columns: list[str],
        rows: Iterable[str],
        source: dict[str, Any] | None = None,
        owner_session_id: str | None = None,
        preview_rows: int | None = None,
        page_size_rows: int | None = None,
    ) -> StoredCsvResult:
        self._ensure_root()
        owner = _safe_owner_session_id(owner_session_id)
        result_id = self._new_result_id()
        result_dir = self._result_dir(result_id, owner)
        result_dir.mkdir(parents=True, exist_ok=False)

        preview_limit = (
            self.settings.preview_rows if preview_rows is None else max(0, preview_rows)
        )
        page_size = (
            self.settings.page_size_rows
            if page_size_rows is None
            else max(1, page_size_rows)
        )

        preview: list[str] = []
        shards: list[dict[str, Any]] = []
        row_count = 0
        byte_count = 0
        shard_index = -1
        shard_row_count = 0
        shard_byte_count = 0
        shard_start = 0
        shard_file = None
        shard_path: Path | None = None

        def open_shard() -> None:
            nonlocal shard_index, shard_row_count, shard_byte_count
            nonlocal shard_start, shard_file, shard_path
            shard_index += 1
            shard_row_count = 0
            shard_byte_count = 0
            shard_start = row_count
            shard_path = result_dir / f"rows-{shard_index:05d}.jsonl"
            shard_file = shard_path.open("w", encoding="utf-8")

        def close_shard() -> None:
            nonlocal shard_file
            if shard_file is None or shard_path is None:
                return
            shard_file.close()
            shards.append(
                {
                    "index": shard_index,
                    "file": shard_path.name,
                    "row_start": shard_start,
                    "row_count": shard_row_count,
                    "bytes": shard_byte_count,
                }
            )
            shard_file = None

        try:
            open_shard()
            for row in rows:
                row_text = str(row)
                encoded = json.dumps(row_text, ensure_ascii=False) + "\n"
                encoded_bytes = len(encoded.encode("utf-8"))
                if shard_row_count > 0 and (
                    shard_row_count >= self.settings.shard_rows
                    or shard_byte_count + encoded_bytes > self.settings.shard_bytes
                ):
                    close_shard()
                    open_shard()

                assert shard_file is not None
                shard_file.write(encoded)
                if len(preview) < preview_limit:
                    preview.append(row_text)
                row_count += 1
                byte_count += encoded_bytes
                shard_row_count += 1
                shard_byte_count += encoded_bytes
            close_shard()
        except Exception:
            if shard_file is not None:
                try:
                    shard_file.close()
                except OSError:
                    pass
            shutil.rmtree(result_dir, ignore_errors=True)
            raise

        manifest = {
            "schema_version": "iotdb_result_store_v1",
            "result_id": result_id,
            "owner_session_id": owner,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_accessed_at": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "format": "csv",
            "columns": columns,
            "row_count": row_count,
            "byte_count": byte_count,
            "preview_rows": preview,
            "preview_row_count": len(preview),
            "page_size_rows": page_size,
            "source": source or {},
            "shards": shards,
        }
        manifest_path = result_dir / _MANIFEST_NAME
        try:
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            shutil.rmtree(result_dir, ignore_errors=True)
            raise
        self.cleanup(owner_session_id=owner)
        return StoredCsvResult(
            result_id=result_id,
            owner_session_id=owner,
            columns=columns,
            preview_rows=preview,
            row_count=row_count,
            byte_count=byte_count,
            page_size_rows=page_size,
            manifest_path=str(manifest_path),
            shard_count=len(shards),
        )

    def _parse_time(self, value: Any, fallback: datetime | None = None) -> datetime:
        if isinstance(value, str) and value.strip():
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except ValueError:
                pass
        return fallback or datetime.fromtimestamp(0, timezone.utc)

    def _dir_size(self, result_dir: Path) -> int:
        total = 0
        try:
            for path in result_dir.rglob("*"):
                if path.is_file():
                    total += path.stat().st_size
        except OSError:
            return total
        return total

    def _entry_from_manifest(
        self,
        manifest_path: Path,
        *,
        legacy: bool = False,
    ) -> ResultStoreEntry | None:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        result_id = str(manifest.get("result_id") or manifest_path.parent.name)
        if not _RESULT_ID_PATTERN.fullmatch(result_id):
            return None
        if legacy:
            owner = str(manifest.get("owner_session_id") or _STANDALONE_OWNER)
        else:
            owner = _safe_owner_session_id(
                manifest.get("owner_session_id") or manifest_path.parent.parent.name
            )
        created_at = self._parse_time(manifest.get("created_at"))
        last_accessed_at = self._parse_time(
            manifest.get("last_accessed_at"), fallback=created_at
        )
        return ResultStoreEntry(
            result_id=result_id,
            owner_session_id=owner,
            tool=str(manifest.get("tool") or ""),
            row_count=int(manifest.get("row_count") or 0),
            byte_count=int(manifest.get("byte_count") or 0),
            storage_bytes=self._dir_size(manifest_path.parent),
            shard_count=len(manifest.get("shards") or []),
            created_at=created_at,
            last_accessed_at=last_accessed_at,
            manifest_path=manifest_path,
            result_dir=manifest_path.parent,
            legacy=legacy,
        )

    def _iter_entries(
        self, owner_session_id: str | None = None
    ) -> list[ResultStoreEntry]:
        entries: list[ResultStoreEntry] = []
        sessions_root = self.root_dir / "sessions"
        owners: list[str]
        if owner_session_id is None:
            owners = []
            if sessions_root.is_dir():
                owners = [
                    path.name
                    for path in sessions_root.iterdir()
                    if path.is_dir() and _OWNER_PATTERN.fullmatch(path.name)
                ]
        else:
            owners = [_safe_owner_session_id(owner_session_id)]

        for owner in owners:
            owner_dir = sessions_root / owner
            if not owner_dir.is_dir():
                continue
            for manifest_path in owner_dir.glob(f"res_*/{_MANIFEST_NAME}"):
                entry = self._entry_from_manifest(manifest_path)
                if entry is not None:
                    entries.append(entry)

        if owner_session_id in (None, _STANDALONE_OWNER):
            for manifest_path in self.root_dir.glob(f"res_*/{_MANIFEST_NAME}"):
                entry = self._entry_from_manifest(manifest_path, legacy=True)
                if entry is not None:
                    entries.append(entry)

        return entries

    def list_results(
        self,
        owner_session_id: str | None = None,
        *,
        all_sessions: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        owner = None if all_sessions else _safe_owner_session_id(owner_session_id)
        entries = self._iter_entries(owner)
        entries.sort(key=lambda entry: entry.last_accessed_at, reverse=True)
        bounded_limit = max(1, min(int(limit), 500))
        selected = entries[:bounded_limit]
        return {
            "owner_session_id": owner,
            "all_sessions": all_sessions,
            "result_count": len(entries),
            "returned_results": len(selected),
            "storage_bytes": sum(entry.storage_bytes for entry in entries),
            "results": [entry.to_payload() for entry in selected],
        }

    def _delete_entry(
        self,
        entry: ResultStoreEntry,
        *,
        reason: str,
        deleted_dirs: set[Path],
    ) -> dict[str, Any] | None:
        if entry.result_dir in deleted_dirs:
            return None
        try:
            shutil.rmtree(entry.result_dir)
        except FileNotFoundError:
            pass
        deleted_dirs.add(entry.result_dir)
        return {
            "result_id": entry.result_id,
            "owner_session_id": entry.owner_session_id,
            "storage_bytes": entry.storage_bytes,
            "reason": reason,
        }

    def _enforce_lru_quota(
        self,
        entries: list[ResultStoreEntry],
        *,
        max_results: int,
        max_bytes: int,
        reason: str,
        deleted_dirs: set[Path],
    ) -> list[dict[str, Any]]:
        active = [
            entry
            for entry in entries
            if entry.result_dir not in deleted_dirs and entry.manifest_path.is_file()
        ]
        active.sort(key=lambda entry: entry.last_accessed_at)
        total_bytes = sum(entry.storage_bytes for entry in active)
        deleted: list[dict[str, Any]] = []

        def over_quota() -> bool:
            return (max_results > 0 and len(active) > max_results) or (
                max_bytes > 0 and total_bytes > max_bytes
            )

        while len(active) > 1 and over_quota():
            entry = active.pop(0)
            total_bytes -= entry.storage_bytes
            info = self._delete_entry(entry, reason=reason, deleted_dirs=deleted_dirs)
            if info is not None:
                deleted.append(info)
        return deleted

    def delete_result(
        self,
        result_id: str,
        owner_session_id: str | None = None,
        *,
        all_sessions: bool = False,
    ) -> dict[str, Any]:
        owner = None if all_sessions else _safe_owner_session_id(owner_session_id)
        entries = self._iter_entries(owner)
        for entry in entries:
            if entry.result_id != result_id:
                continue
            deleted_dirs: set[Path] = set()
            info = self._delete_entry(
                entry, reason="explicit_delete", deleted_dirs=deleted_dirs
            )
            return {
                "deleted": info is not None,
                "result": info or entry.to_payload(),
            }
        raise FileNotFoundError(f"Result '{result_id}' was not found.")

    def cleanup(
        self,
        owner_session_id: str | None = None,
        *,
        all_sessions: bool = False,
        ttl_seconds: int | None = None,
        max_session_results: int | None = None,
        max_session_bytes: int | None = None,
        max_global_results: int | None = None,
        max_global_bytes: int | None = None,
    ) -> dict[str, Any]:
        self._ensure_root()
        owner = None if all_sessions else _safe_owner_session_id(owner_session_id)
        deleted_dirs: set[Path] = set()
        deleted: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        effective_ttl = (
            self.settings.ttl_seconds
            if ttl_seconds is None
            else max(0, int(ttl_seconds))
        )

        ttl_candidates = self._iter_entries(owner)
        if effective_ttl > 0:
            cutoff = now - timedelta(seconds=effective_ttl)
            for entry in ttl_candidates:
                if entry.last_accessed_at >= cutoff:
                    continue
                info = self._delete_entry(
                    entry, reason="ttl_expired", deleted_dirs=deleted_dirs
                )
                if info is not None:
                    deleted.append(info)

        session_result_limit = (
            self.settings.max_session_results
            if max_session_results is None
            else max(0, int(max_session_results))
        )
        session_byte_limit = (
            self.settings.max_session_bytes
            if max_session_bytes is None
            else max(0, int(max_session_bytes))
        )
        owners = (
            sorted({entry.owner_session_id for entry in self._iter_entries(None)})
            if all_sessions
            else [owner]
        )
        for current_owner in owners:
            if current_owner is None:
                continue
            deleted.extend(
                self._enforce_lru_quota(
                    self._iter_entries(current_owner),
                    max_results=session_result_limit,
                    max_bytes=session_byte_limit,
                    reason="session_lru_quota",
                    deleted_dirs=deleted_dirs,
                )
            )

        global_result_limit = (
            self.settings.max_global_results
            if max_global_results is None
            else max(0, int(max_global_results))
        )
        global_byte_limit = (
            self.settings.max_global_bytes
            if max_global_bytes is None
            else max(0, int(max_global_bytes))
        )
        deleted.extend(
            self._enforce_lru_quota(
                self._iter_entries(None),
                max_results=global_result_limit,
                max_bytes=global_byte_limit,
                reason="global_lru_quota",
                deleted_dirs=deleted_dirs,
            )
        )

        remaining = self._iter_entries(None)
        return {
            "owner_session_id": owner,
            "all_sessions": all_sessions,
            "ttl_seconds": effective_ttl,
            "limits": {
                "max_session_results": session_result_limit,
                "max_session_bytes": session_byte_limit,
                "max_global_results": global_result_limit,
                "max_global_bytes": global_byte_limit,
            },
            "deleted_results": len(deleted),
            "deleted_storage_bytes": sum(item["storage_bytes"] for item in deleted),
            "remaining_results": len(remaining),
            "remaining_storage_bytes": sum(entry.storage_bytes for entry in remaining),
            "deleted": deleted,
        }

    def read_manifest(
        self, result_id: str, owner_session_id: str | None = None
    ) -> dict[str, Any]:
        manifest_path = (
            self._existing_result_dir(result_id, owner_session_id) / _MANIFEST_NAME
        )
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Result '{result_id}' was not found.") from exc

    def _touch_manifest(self, result_dir: Path, manifest: dict[str, Any]) -> None:
        manifest["last_accessed_at"] = datetime.now(timezone.utc).isoformat()
        (result_dir / _MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def read_page(
        self,
        result_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        owner_session_id: str | None = None,
    ) -> dict[str, Any]:
        result_dir = self._existing_result_dir(result_id, owner_session_id)
        manifest = json.loads((result_dir / _MANIFEST_NAME).read_text(encoding="utf-8"))
        row_count = int(manifest.get("row_count", 0))
        page_size = int(manifest.get("page_size_rows") or self.settings.page_size_rows)
        requested_limit = page_size if limit is None else max(1, int(limit))
        effective_limit = min(requested_limit, self.settings.max_page_rows)
        effective_offset = min(max(0, int(offset)), row_count)
        rows: list[str] = []

        for shard in manifest.get("shards", []):
            shard_start = int(shard["row_start"])
            shard_count = int(shard["row_count"])
            shard_end = shard_start + shard_count
            if shard_end <= effective_offset:
                continue
            if shard_start >= effective_offset + effective_limit:
                break

            skip = max(0, effective_offset - shard_start)
            shard_path = result_dir / str(shard["file"])
            with shard_path.open("r", encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if index < skip:
                        continue
                    if len(rows) >= effective_limit:
                        break
                    rows.append(str(json.loads(line)))
            if len(rows) >= effective_limit:
                break

        next_offset = effective_offset + len(rows)
        has_more = next_offset < row_count
        self._touch_manifest(result_dir, manifest)
        return {
            "format": "csv",
            "result_id": result_id,
            "owner_session_id": manifest.get("owner_session_id", _STANDALONE_OWNER),
            "columns": list(manifest.get("columns", [])),
            "rows": rows,
            "row_count": row_count,
            "offset": effective_offset,
            "limit": effective_limit,
            "returned_rows": len(rows),
            "next_cursor": str(next_offset) if has_more else None,
            "has_more": has_more,
            "manifest_path": str(result_dir / _MANIFEST_NAME),
        }

    def _page_request_offset(self, request: dict[str, Any]) -> int:
        if request.get("offset") is not None:
            return max(0, int(request["offset"]))
        cursor = request.get("cursor")
        if cursor is None or str(cursor).strip() == "":
            return 0
        cleaned = str(cursor).strip()
        if not cleaned.isdigit():
            raise ValueError("cursor must be a non-negative integer row offset.")
        return int(cleaned)

    def read_pages(
        self,
        requests: list[dict[str, Any]],
        *,
        owner_session_id: str | None = None,
        default_limit: int | None = None,
        max_pages: int | None = None,
        max_total_rows: int | None = None,
        continue_on_error: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(requests, list) or not requests:
            raise ValueError("pages must be a non-empty list.")
        effective_max_pages = _bounded_int(
            max_pages if max_pages is not None else os.getenv("TIMESEEK_RESULT_MAX_BATCH_PAGES"),
            32,
            minimum=1,
            maximum=256,
        )
        if len(requests) > effective_max_pages:
            raise ValueError(
                f"read_result_pages accepts at most {effective_max_pages} page requests."
            )
        effective_max_total_rows = _bounded_int(
            max_total_rows
            if max_total_rows is not None
            else os.getenv("TIMESEEK_RESULT_MAX_BATCH_READ_ROWS"),
            10000,
            minimum=1,
            maximum=1_000_000,
        )

        results: list[dict[str, Any]] = []
        total_rows = 0
        failed = 0
        skipped = 0
        stop = False

        for index, request in enumerate(requests):
            if stop:
                skipped += 1
                results.append(
                    {
                        "index": index,
                        "ok": False,
                        "status": "skipped",
                        "error_type": "BatchAborted",
                        "error": "Skipped because continue_on_error is false and an earlier page failed.",
                    }
                )
                continue

            try:
                if not isinstance(request, dict):
                    raise ValueError("Each page request must be an object.")
                result_id = str(request.get("result_id") or "").strip()
                if not result_id:
                    raise ValueError("Each page request requires result_id.")
                page_owner = request.get("owner_session_id")
                if page_owner is not None:
                    page_owner = str(page_owner)
                else:
                    page_owner = owner_session_id
                page_limit = (
                    request.get("limit")
                    if request.get("limit") is not None
                    else default_limit
                )
                page = self.read_page(
                    result_id,
                    offset=self._page_request_offset(request),
                    limit=None if page_limit is None else int(page_limit),
                    owner_session_id=page_owner,
                )
                next_total_rows = total_rows + int(page.get("returned_rows") or 0)
                if next_total_rows > effective_max_total_rows:
                    raise ValueError(
                        "read_result_pages total row quota exceeded: "
                        f"{next_total_rows}>{effective_max_total_rows}"
                    )
                total_rows = next_total_rows
                page["index"] = index
                page["ok"] = True
                page["status"] = "success"
                results.append(page)
            except Exception as exc:
                failed += 1
                if not continue_on_error:
                    stop = True
                results.append(
                    {
                        "index": index,
                        "ok": False,
                        "status": "error",
                        "result_id": request.get("result_id") if isinstance(request, dict) else None,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )

        succeeded = sum(1 for item in results if item.get("status") == "success")
        return {
            "format": "csv",
            "page_request_count": len(requests),
            "returned_pages": succeeded,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "continue_on_error": continue_on_error,
            "limits": {
                "max_pages": effective_max_pages,
                "max_total_rows": effective_max_total_rows,
                "default_limit": default_limit,
            },
            "returned_rows": total_rows,
            "pages": results,
        }


def result_store_from_export_path(export_path: str) -> FileResultStore:
    return FileResultStore(Path(export_path).expanduser() / "result_store")
