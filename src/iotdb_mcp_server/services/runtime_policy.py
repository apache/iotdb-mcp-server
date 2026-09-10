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

import logging

from mcp.types import TextContent

from iotdb_mcp_server.config import Config
from iotdb_mcp_server.runtime_policy import dynamic_policy_snapshot
from iotdb_mcp_server.runtime_policy import reset_session_policy
from iotdb_mcp_server.runtime_policy import set_session_policy
from iotdb_mcp_server.services.json_response import payload_response


def register_runtime_policy_tools(mcp, config: Config, logger: logging.Logger) -> None:
    """Register session-scoped policy controls for this MCP process."""

    @mcp.tool()
    async def get_iotdb_session_policy() -> list[TextContent]:
        """Inspect effective IoTDB MCP permissions for this running session."""
        return payload_response(
            "get_iotdb_session_policy",
            dynamic_policy_snapshot(),
            message="IoTDB session policy snapshot.",
        )

    @mcp.tool()
    async def set_iotdb_session_policy(
        policy: dict[str, object] | None = None,
        preset: str | None = None,
        replace: bool = False,
    ) -> list[TextContent]:
        """
        Set session-scoped IoTDB MCP permissions without restarting the MCP server.

        preset accepts: full, ddl, readonly. Explicit policy values override preset values.
        """
        snapshot = set_session_policy(policy=policy, preset=preset, replace=replace)
        logger.info(
            "Updated IoTDB session policy preset=%s replace=%s keys=%s",
            preset,
            replace,
            sorted((policy or {}).keys()),
        )
        return payload_response(
            "set_iotdb_session_policy",
            snapshot,
            message="IoTDB session policy updated.",
        )

    @mcp.tool()
    async def reset_iotdb_session_policy(keys: list[str] | None = None) -> list[TextContent]:
        """Clear all or selected session-scoped IoTDB MCP permission overrides."""
        snapshot = reset_session_policy(keys=keys)
        logger.info("Reset IoTDB session policy keys=%s", keys or "*")
        return payload_response(
            "reset_iotdb_session_policy",
            snapshot,
            message="IoTDB session policy reset.",
        )
