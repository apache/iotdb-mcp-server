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
import sys

from iotdb.Session import Session
from iotdb.SessionPool import SessionPool, PoolConfig
from iotdb.utils.SessionDataSet import SessionDataSet
from mcp.server.fastmcp import FastMCP
from mcp.types import (
    TextContent,
)

from iotdb_mcp_server.config import Config

# Initialize FastMCP server
mcp = FastMCP("iotdb_mcp_server")

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger("iotdb_mcp_server")

config = Config.from_env_arguments()

db_config = {
    "host": config.host,
    "port": config.port,
    "user": config.user,
    "password": config.password,
}

logger.info(f"IoTDB Config: {db_config}")

pool_config = PoolConfig(
    node_urls=[str(config.host) + ":" + str(config.port)],
    user_name=config.user,
    password=config.password,
    fetch_size=1024,
    time_zone="UTC+8",
    max_retry=3
)
max_pool_size = 5
wait_timeout_in_ms = 3000
print('@@@@@@@@@@@@@', file=sys.stderr)
session_pool = SessionPool(pool_config, max_pool_size, wait_timeout_in_ms)
print('############', file=sys.stderr)

@mcp.tool()
async def metadata_query(query_sql: str) -> list[TextContent]:
    """Execute metadata queries on IoTDB to explore database structure and statistics.

    Args:
        query_sql: The metadata query to execute. Supported queries:
            - SHOW DATABASES [path]: List all databases or databases under a specific path
            - SHOW TIMESERIES [path]: List all time series or time series under a specific path
            - SHOW CHILD PATHS [path]: List child paths under a specific path
            - SHOW CHILD NODES [path]: List child nodes under a specific path
            - SHOW DEVICES [path]: List all devices or devices under a specific path
            - COUNT TIMESERIES [path]: Count time series under a specific path
            - COUNT NODES [path]: Count nodes under a specific path
            - COUNT DEVICES [path]: Count devices under a specific path  
            - if path is not provided, the query will be applied to root.**

    Examples:
        SHOW DATABASES root.**
        SHOW TIMESERIES root.ln.**
        SHOW CHILD PATHS root.ln
        SHOW CHILD PATHS root.ln.*.*
        SHOW CHILD NODES root.ln
        SHOW DEVICES root.ln.**
        COUNT TIMESERIES root.ln.**
        COUNT NODES root.ln
        COUNT DEVICES root.ln
    """
    session = session_pool.get_session()
    try:
        stmt = query_sql.strip().upper()
        
        # 处理SHOW DATABASES
        if (
            stmt.startswith("SHOW DATABASES")
            or stmt.startswith("SHOW TIMESERIES")
            or stmt.startswith("SHOW CHILD PATHS")
            or stmt.startswith("SHOW CHILD NODES")
            or stmt.startswith("SHOW DEVICES")
            or stmt.startswith("COUNT TIMESERIES")
            or stmt.startswith("COUNT NODES")
            or stmt.startswith("COUNT DEVICES")
        ):
            res = session.execute_query_statement(query_sql)
            return prepare_res(res, session)
        else:
            raise ValueError("Unsupported metadata query. Please use one of the supported query types.")
            
    except Exception as e:
        session.close()
        raise e

@mcp.tool()
async def select_query(query_sql: str) -> list[TextContent]:
    """Execute a SELECT query on the IoTDB tree SQL dialect.

    Args:
        query_sql: The SQL query to execute (using TREE dialect)

    SQL Syntax:
        SELECT [LAST] selectExpr [, selectExpr] ...
            [INTO intoItem [, intoItem] ...]
            FROM prefixPath [, prefixPath] ...
            [WHERE whereCondition]
            [GROUP BY {
                ([startTime, endTime), interval [, slidingStep]) |
                LEVEL = levelNum [, levelNum] ... |
                TAGS(tagKey [, tagKey] ... |
                VARIATION(expression[,delta][,ignoreNull=true/false]) |
                CONDITION(expression,[keep>/>=/=/</<=]threshold[,ignoreNull=true/false]) |
                SESSION(timeInterval) |
                COUNT(expression, size[,ignoreNull=true/false])
            }]
            [HAVING havingCondition]
            [ORDER BY sortKey {ASC | DESC}]
            [FILL ({PREVIOUS | LINEAR | constant}) (, interval=DURATION_LITERAL)?)]
            [SLIMIT seriesLimit] [SOFFSET seriesOffset]
            [LIMIT rowLimit] [OFFSET rowOffset]
            [ALIGN BY {TIME | DEVICE}]

    Examples:
        select temperature from root.ln.wf01.wt01 where time < 2017-11-01T00:08:00.000
        select status, temperature from root.ln.wf01.wt01 where (time > 2017-11-01T00:05:00.000 and time < 2017-11-01T00:12:00.000) or (time >= 2017-11-01T16:35:00.000 and time <= 2017-11-01T16:37:00.000)
        select * from root.ln.** where time > 1 order by time desc limit 10;

    Supported Aggregate Functions:
        SUM
        COUNT
        MAX_VALUE
        MIN_VALUE
        AVG
        VARIANCE
        MAX_TIME
        MIN_TIME
        ...
    """
    session = session_pool.get_session()
    res = session.execute_query_statement(query_sql)

    stmt = query_sql.strip().upper()
    # Regular SELECT queries
    if (
        stmt.startswith("SELECT")
    ):
        return prepare_res(res, session)
    # Non-SELECT queries
    else:
        raise ValueError("Only SELECT queries are allowed for read_query")

def prepare_res(
    _res: SessionDataSet, _session: Session
) -> list[TextContent]:
    columns = _res.get_column_names()
    result = []
    while _res.has_next():
        row = _res.next().get_fields()
        result.append(",".join(map(str, row)))
    _session.close()
    return [
        TextContent(
            type="text",
            text="\n".join([",".join(columns)] + result),
        )
    ]


if __name__ == "__main__":
    logger.info("iotdb_mcp_server running with stdio transport")
    # Initialize and run the server
    mcp.run(transport="stdio")
