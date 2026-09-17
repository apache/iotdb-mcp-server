# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements. See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership. The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License. You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied. See the License for the
# specific language governing permissions and limitations
# under the License.

"""Out-of-band policy approvals. Never register administrative functions as tools.

The directory and this CLI must be outside the agent's filesystem/shell authority.
Filesystem permissions alone do not isolate an unsandboxed same-UID agent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any
import uuid

_APPROVAL_TTL_SECONDS = 300
_MAX_PENDING = 32


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _check_directory(directory: Path) -> None:
    if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
        raise ValueError(
            "Approval directory must be an existing, absolute, non-symlink directory."
        )
    if os.name == "posix":
        info = directory.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError(
                "Approval directory must be owned by the service user with mode 0700."
            )


def _write_json(path: Path, data: dict[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=".policy-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class PolicyApprovalStore:
    """Bind one decision to an exact proposal, revision, and running process."""

    def __init__(self, directory: Path):
        _check_directory(directory)
        self.directory = directory
        self._pending: dict[str, dict[str, Any]] = {}

    def _remove(self, key: str) -> None:
        request = self._pending.pop(key)
        for suffix in ("request", "decision"):
            (self.directory / f"{request['request_id']}.{suffix}.json").unlink(
                missing_ok=True
            )

    def authorize(self, proposal: dict[str, Any]) -> dict[str, Any]:
        _check_directory(self.directory)
        now = time.time()
        for key, request in list(self._pending.items()):
            if (
                request["expires_at"] <= now
                or request["revision"] != proposal["revision"]
            ):
                self._remove(key)
        key = _digest(proposal)
        if key not in self._pending:
            if len(self._pending) >= _MAX_PENDING:
                raise PermissionError(
                    "Too many pending policy approvals. Wait for requests to expire."
                )
            request = {
                **proposal,
                "request_id": uuid.uuid4().hex,
                "created_at": now,
                "expires_at": now + _APPROVAL_TTL_SECONDS,
            }
            _write_json(
                self.directory / f"{request['request_id']}.request.json", request
            )
            self._pending[key] = request
        request = self._pending[key]
        result = {
            "status": "approval_required",
            "request_id": request["request_id"],
            "expires_at": request["expires_at"],
            "requested_changes": request["changes"],
        }
        decision_path = self.directory / f"{request['request_id']}.decision.json"
        try:
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return result
        # The in-memory request, not the file shown to the administrator, is
        # authoritative. Tampering with that file cannot approve another change.
        if not isinstance(decision, dict) or decision.get("request_digest") != _digest(
            request
        ):
            raise PermissionError("Invalid administrator decision; policy unchanged.")
        if decision.get("decision") == "approve":
            if time.time() >= request["expires_at"]:
                self._remove(key)
                return self.authorize(proposal)
            self._remove(key)
            result["status"] = "approved"
        else:
            result["status"] = "approval_rejected"
        return result


def main(argv: list[str] | None = None) -> int:
    """Run only from an operator-controlled terminal or trusted management UI."""
    parser = argparse.ArgumentParser(
        description="Review and decide an MCP session policy request."
    )
    parser.add_argument("action", choices=("show", "approve", "deny"))
    parser.add_argument("request_id")
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args(argv)
    _check_directory(args.directory)
    if not re.fullmatch(r"[0-9a-f]{32}", args.request_id):
        parser.error("Invalid request ID")
    request = json.loads(
        (args.directory / f"{args.request_id}.request.json").read_text(encoding="utf-8")
    )
    if request["request_id"] != args.request_id or request["expires_at"] <= time.time():
        parser.error("Request is invalid or expired")
    print(json.dumps(request, indent=2, sort_keys=True))
    if args.action != "show":
        _write_json(
            args.directory / f"{args.request_id}.decision.json",
            {
                "request_digest": _digest(request),
                "decision": args.action,
            },
        )
        print(
            f"Decision recorded: {args.action}. Retry the original MCP request to apply it."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
