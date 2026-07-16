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

from fastmcp import FastMCP

from iotdb_mcp_server.config import Config
from iotdb_mcp_server.services.database import register_database_tools
from iotdb_mcp_server.services.explain import register_explain_tools
from iotdb_mcp_server.services.metadata import register_metadata_tools
from iotdb_mcp_server.services.model import register_model_tools
from iotdb_mcp_server.services.query import register_query_tools
from iotdb_mcp_server.services.results import register_result_store_tools
from iotdb_mcp_server.services.runtime_policy import register_runtime_policy_tools
from iotdb_mcp_server.services.sql_driver import register_sql_driver_tools
from iotdb_mcp_server.services.table import register_table_tools
from iotdb_mcp_server.services.targets import register_target_tools
from iotdb_mcp_server.services.timeseries import register_timeseries_tools
from iotdb_mcp_server.services.ttl import register_ttl_tools
from iotdb_mcp_server.services.udf import register_udf_tools
from iotdb_mcp_server.services.write import register_write_tools

mcp = FastMCP("iotdb_mcp_server")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("iotdb_mcp_server")

config = Config.from_env_arguments()

logger.info(
    "IoTDB Config: %s",
    config.safe_dict(),
)

register_target_tools(mcp, config, logger)
register_runtime_policy_tools(mcp, config, logger)
register_result_store_tools(mcp, config, logger)
register_query_tools(mcp, config, logger)
register_sql_driver_tools(mcp, config, logger)
register_metadata_tools(mcp, config, logger)
register_explain_tools(mcp, config, logger)
register_database_tools(mcp, config, logger)
register_timeseries_tools(mcp, config, logger)
register_ttl_tools(mcp, config, logger)
register_table_tools(mcp, config, logger)
register_udf_tools(mcp, config, logger)
register_write_tools(mcp, config, logger)
register_model_tools(mcp, config, logger)


def main() -> None:
    logger.info("iotdb_mcp_server running with stdio transport")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
