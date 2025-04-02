import logging

from iotdb.table_session import TableSession
from iotdb.table_session_pool import TableSessionPool, TableSessionPoolConfig
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
    "database": config.database,
}

logger.info(f"IoTDB Config: {db_config}")

session_pool_config = TableSessionPoolConfig(
    node_urls=[str(config.host) + ":" + str(config.port)],
    username=config.user,
    password=config.password,
    database=None if len(config.database) == 0 else config.database,
)

session_pool = TableSessionPool(session_pool_config)


@mcp.tool()
async def read_query(query_sql: str) -> list[TextContent]:
    """Execute a SELECT query on the IoTDB. Please use table sql_dialect when generating SQL queries.

    Args:
        query_sql: The SQL query to execute (using TABLE dialect)
    """
    table_session = session_pool.get_session()
    res = table_session.execute_query_statement(query_sql)

    stmt = query_sql.strip().upper()
    # Regular SELECT queries
    if (
        stmt.startswith("SELECT")
        or stmt.startswith("DESCRIBE")
        or stmt.startswith("SHOW")
    ):
        return prepare_res(res, table_session)
    # Non-SELECT queries
    else:
        raise ValueError("Only SELECT queries are allowed for read_query")


@mcp.tool()
async def list_tables() -> list[TextContent]:
    """List all tables in the IoTDB database."""
    table_session = session_pool.get_session()
    res = table_session.execute_query_statement("SHOW TABLES")

    result = ["Tables_in_" + db_config["database"]]  # Header
    while res.has_next():
        result.append(str(res.next().get_fields()[0]))
    table_session.close()
    return [TextContent(type="text", text="\n".join(result))]


@mcp.tool()
async def describe_table(table_name: str) -> list[TextContent]:
    """Get the schema information for a specific table
    Args:
         table_name: name of the table to describe
    """
    table_session = session_pool.get_session()
    res = table_session.execute_query_statement("DESC " + table_name)

    return prepare_res(res, table_session)


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


if __name__ == "__main__":
    logger.info("iotdb_mcp_server running with stdio transport")
    # Initialize and run the server
    mcp.run(transport="stdio")
