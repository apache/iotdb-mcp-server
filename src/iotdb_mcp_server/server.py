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
import datetime
import asyncio
import os
import uuid
import pandas as pd
from typing import Dict, Any, List, Union

from iotdb.Session import Session
from iotdb.SessionPool import SessionPool, PoolConfig
from iotdb.utils.SessionDataSet import SessionDataSet
from iotdb.table_session import TableSession
from iotdb.table_session_pool import TableSessionPool, TableSessionPoolConfig
from fastmcp import FastMCP
from mcp.types import TextContent

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
    "database": config.database,
    "sql_dialect": config.sql_dialect,
    "export_path": config.export_path,
}

max_pool_size = 100  # Increased from 100 for better concurrency

logger.info(f"IoTDB Config: {db_config}")

# Ensure export directory exists
if not os.path.exists(config.export_path):
    try:
        os.makedirs(config.export_path)
        logger.info(f"Created export directory: {config.export_path}")
    except Exception as e:
        logger.warning(f"Failed to create export directory {config.export_path}: {str(e)}")

if config.sql_dialect == "tree":

    # Configure connection pool with optimized settings
    pool_config = PoolConfig(
        node_urls=[str(config.host) + ":" + str(config.port)],
        user_name=config.user,
        password=config.password,
        fetch_size=1024,  # Fetch size for queries
        time_zone="UTC+8", # Consistent timezone
        max_retry=3  # Connection retry attempts
    )
    # Optimize pool size based on expected concurrent queries
    wait_timeout_in_ms = 5000  # Increased from 3000 for better reliability
    session_pool = SessionPool(pool_config, max_pool_size, wait_timeout_in_ms)
    
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
        session = None
        try:
            session = session_pool.get_session()
            stmt = query_sql.strip().upper()
            
            # Process SHOW DATABASES
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
                session.close()
                raise ValueError("Unsupported metadata query. Please use one of the supported query types.")
        except Exception as e:
            if session:
                session.close()
            logger.error(f"Failed to execute metadata query: {str(e)}")
            raise

    @mcp.tool()
    async def select_query(query_sql: str) -> list[TextContent]:
        """Execute a SELECT query on the IoTDB tree SQL dialect.

        Args:
            query_sql: The SQL query to execute (using TREE dialect, time using ISO 8601 format, e.g. 2017-11-01T00:08:00.000).

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
        session = None
        try:
            session = session_pool.get_session()
            stmt = query_sql.strip().upper()
            
            # Regular SELECT queries
            if stmt.startswith("SELECT"):
                res = session.execute_query_statement(query_sql)
                return prepare_res(res, session)
            else:
                session.close()
                raise ValueError("Only SELECT queries are allowed for select_query")
        except Exception as e:
            if session:
                session.close()
            logger.error(f"Failed to execute select query: {str(e)}")
            raise

    @mcp.tool()
    async def export_query(query_sql: str, format: str = "csv", filename: str = None) -> list[TextContent]:
        """Execute a query and export the results to a CSV or Excel file.
        
        Args:
            query_sql: The SQL query to execute (using TREE dialect, time using ISO 8601 format, e.g. 2017-11-01T00:08:00.000)
            format: Export format, either "csv" or "excel" (default: "csv")
            filename: Optional filename for the exported file. If not provided, a unique filename will be generated.

        SQL Syntax:
            SELECT ⟨select_list⟩
              FROM ⟨tables⟩
              [WHERE ⟨condition⟩]
              [GROUP BY ⟨groups⟩]
              [HAVING ⟨group_filter⟩]
              [FILL ⟨fill_methods⟩]
              [ORDER BY ⟨order_expression⟩]
              [OFFSET ⟨n⟩]
              [LIMIT ⟨n⟩];
            
        Returns:
            Information about the exported file and a preview of the data (max 10 rows)
        """
        session = None
        try:
            session = session_pool.get_session()
            stmt = query_sql.strip().upper()
            
            if stmt.startswith("SELECT") or stmt.startswith("SHOW"):
                # Execute the query
                res = session.execute_query_statement(query_sql)
                
                # Create a pandas DataFrame
                df = res.todf()
                # Close the session
                session.close()
                
                # Generate unique filename with timestamp
                timestamp = int(datetime.datetime.now().timestamp())
                if filename is None:
                    # Generate a unique filename if not provided
                    filename = f"dump_{uuid.uuid4().hex[:4]}_{timestamp}"
                filepath = ""
                
                if format.lower() == "csv":
                    if(filename.lower().endswith(".csv")):
                        filename = filename[:-4]
                    filepath = f"{config.export_path}/{filename}.csv"
                    df.to_csv(filepath, index=False)
                elif format.lower() == "excel":
                    if(filename.lower().endswith(".xlsx")):
                        filename = filename[:-5]
                    filepath = f"{config.export_path}/{filename}.xlsx"
                    df.to_excel(filepath, index=False)
                else:
                    raise ValueError("Format must be either 'csv' or 'excel'")
                
                # Generate preview (first 10 rows)
                preview_rows = min(10, len(df))
                preview_data = []
                preview_data.append(",".join(df.columns))  # Header

                for i in range(preview_rows):
                    preview_data.append(",".join(map(str, df.iloc[i])))

                # Return information
                return [
                    TextContent(
                        type="text",
                        text=f"Query results exported to {filepath}\n\nPreview (first {preview_rows} rows):\n" + "\n".join(preview_data)
                    )
                ]
            else:
                raise ValueError("Only SELECT or SHOW queries are allowed for export")
        except Exception as e:
            if session:
                session.close()
            logger.error(f"Failed to export query: {str(e)}")
            raise

    def prepare_res(
        _res: SessionDataSet, _session: Session
    ) -> list[TextContent]:
        columns = _res.get_column_names()
        result = []
        while _res.has_next():
            record = _res.next()
            if columns[0] == "Time":
                timestamp = record.get_timestamp()
                # formatted_time = datetime.datetime.fromtimestamp(timestamp/1000).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]
                row = record.get_fields()
                result.append(str(timestamp) + "," + ",".join(map(str, row)))
            else:
                row = record.get_fields()
                result.append(",".join(map(str, row)))
        _session.close()
        return [
            TextContent(
                type="text",
                text="\n".join([",".join(columns)] + result),
            )
        ]

elif config.sql_dialect == "table":

    session_pool_config = TableSessionPoolConfig(
        node_urls=[str(config.host) + ":" + str(config.port)],
        username=config.user,
        password=config.password,
        max_pool_size=max_pool_size,  # Increased from 5 for better concurrency
        database=None if len(config.database) == 0 else config.database,
    )
    session_pool = TableSessionPool(session_pool_config)
    
    @mcp.tool()
    async def read_query(query_sql: str) -> list[TextContent]:
        """Execute a SELECT query on the IoTDB. Please use table sql_dialect when generating SQL queries.

        Args:
            query_sql: The SQL query to execute (using TABLE dialect, time using ISO 8601 format, e.g. 2017-11-01T00:08:00.000)
        """
        table_session = None
        try:
            table_session = session_pool.get_session()
            stmt = query_sql.strip().upper()
            
            # Regular SELECT queries
            if (
                stmt.startswith("SELECT")
                or stmt.startswith("DESCRIBE")
                or stmt.startswith("SHOW")
            ):
                res = table_session.execute_query_statement(query_sql)
                return prepare_res(res, table_session)
            else:
                table_session.close()
                raise ValueError("Only SELECT queries are allowed for read_query")
        except Exception as e:
            if table_session:
                table_session.close()
            logger.error(f"Failed to execute query: {str(e)}")
            raise
            
    @mcp.tool()
    async def list_tables() -> list[TextContent]:
        """List all tables in the IoTDB database."""
        table_session = None
        try:
            table_session = session_pool.get_session()
            res = table_session.execute_query_statement("SHOW TABLES")

            result = ["Tables_in_" + db_config["database"]]  # Header
            while res.has_next():
                result.append(str(res.next().get_fields()[0]))
            table_session.close()
            return [TextContent(type="text", text="\n".join(result))]
        except Exception as e:
            if table_session:
                table_session.close()
            logger.error(f"Failed to list tables: {str(e)}")
            raise
            
    @mcp.tool()
    async def describe_table(table_name: str) -> list[TextContent]:
        """Get the schema information for a specific table
        Args:
            table_name: name of the table to describe
        """
        table_session = None
        try:
            table_session = session_pool.get_session()
            res = table_session.execute_query_statement("DESC " + table_name + " details")
            return prepare_res(res, table_session)
        except Exception as e:
            if table_session:
                table_session.close()
            logger.error(f"Failed to describe table {table_name}: {str(e)}")
            raise
            
    @mcp.tool()
    async def export_table_query(query_sql: str, format: str = "csv", filename: str = None) -> list[TextContent]:
        """Execute a query and export the results to a CSV or Excel file.
        
        Args:
            query_sql: The SQL query to execute (using TABLE dialect, time using ISO 8601 format, e.g. 2017-11-01T00:08:00.000)
            format: Export format, either "csv" or "excel" (default: "csv")
            filename: Optional filename for the exported file. If not provided, a unique filename will be generated.
                    
        SQL Syntax:
            SELECT ⟨select_list⟩
              FROM ⟨tables⟩
              [WHERE ⟨condition⟩]
              [GROUP BY ⟨groups⟩]
              [HAVING ⟨group_filter⟩]
              [FILL ⟨fill_methods⟩]
              [ORDER BY ⟨order_expression⟩]
              [OFFSET ⟨n⟩]
              [LIMIT ⟨n⟩];

        Returns:
            Information about the exported file and a preview of the data (max 10 rows)
        """
        table_session = None
        try:
            table_session = session_pool.get_session()
            stmt = query_sql.strip().upper()
            
            if stmt.startswith("SELECT") or stmt.startswith("SHOW") or stmt.startswith("DESCRIBE") or stmt.startswith("DESC"):
                # Execute the query
                res = table_session.execute_query_statement(query_sql)
                
                # Create a pandas DataFrame
                df = res.todf()
                
                # Close the session
                table_session.close()
                
                # Generate unique filename with timestamp
                timestamp = int(datetime.datetime.now().timestamp())
                if filename is None:
                    filename = f"dump_{uuid.uuid4().hex[:4]}_{timestamp}"
                filepath = ""
                
                if format.lower() == "csv":
                    if(filename.lower().endswith(".csv")):
                        filename = filename[:-4]
                    filepath = f"{config.export_path}/{filename}.csv"
                    df.to_csv(filepath, index=False)
                elif format.lower() == "excel":
                    if(filename.lower().endswith(".xlsx")):
                        filename = filename[:-5]
                    filepath = f"{config.export_path}/{filename}.xlsx"
                    df.to_excel(filepath, index=False)
                else:
                    raise ValueError("Format must be either 'csv' or 'excel'")
                
                # Generate preview (first 10 rows)
                preview_rows = min(10, len(df))
                preview_data = []
                preview_data.append(",".join(df.columns))  # Header

                for i in range(preview_rows):
                    preview_data.append(",".join(map(str, df.iloc[i])))

                # Return information
                return [
                    TextContent(
                        type="text",
                        text=f"Query results exported to {filepath}\n\nPreview (first {preview_rows} rows):\n" + "\n".join(preview_data)
                    )
                ]
            else:
                raise ValueError("Only SELECT, SHOW or DESCRIBE queries are allowed for export")
        except Exception as e:
            if table_session:
                table_session.close()
            logger.error(f"Failed to export table query: {str(e)}")
            raise

    def prepare_res(
        _res: SessionDataSet, _table_session: TableSession
    ) -> list[TextContent]:
        columns = _res.get_column_names()
        result = []
        while _res.has_next():
            row = _res.next().get_fields()
            result.append(",".join(map(str, row)))
        _table_session.close()
        return [
            TextContent(
                type="text",
                text="\n".join([",".join(columns)] + result),
            )
        ]


def main():
    logger.info("iotdb_mcp_server running with stdio transport")
    # Initialize and run the server
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
