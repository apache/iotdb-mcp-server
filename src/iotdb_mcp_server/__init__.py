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

logger = logging.getLogger("iotdb_mcp_server")


def main() -> int:
    """Console script entrypoint."""
    from . import server

    try:
        logger.info("Starting IoTDB MCP Server...")
        server.main()
        return 0
    except KeyboardInterrupt:
        logger.info("IoTDB MCP Server interrupted")
        return 0
    except Exception as e:
        logger.error(f"Error in IoTDB MCP Server: {e}")
        return 1


__all__ = ["main"]
