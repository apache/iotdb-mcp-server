import argparse
from dataclasses import dataclass
import os


@dataclass
class Config:
    """
    Configuration for the IoTDB mcp server.
    """

    host: str
    """
    IoTDB host
    """

    port: int
    """
    IoTDB port
    """

    user: str
    """
    IoTDB username
    """

    password: str
    """
    IoTDB password
    """

    database: str
    """
    IoTDB database name
    """

    @staticmethod
    def from_env_arguments() -> "Config":
        """
        Parse command line arguments.
        """
        parser = argparse.ArgumentParser(description="IoTDB MCP Server")

        parser.add_argument(
            "--host",
            type=str,
            help="IoTDB host",
            default=os.getenv("IOTDB_HOST", "127.0.0.1"),
        )

        parser.add_argument(
            "--port",
            type=int,
            help="IoTDB MySQL protocol port",
            default=os.getenv("IOTDB_PORT", 6667),
        )

        parser.add_argument(
            "--database",
            type=str,
            help="IoTDB connect database name",
            default=os.getenv("IOTDB_DATABASE", "test"),
        )

        parser.add_argument(
            "--user",
            type=str,
            help="IoTDB username",
            default=os.getenv("IOTDB_USER", "root"),
        )

        parser.add_argument(
            "--password",
            type=str,
            help="IoTDB password",
            default=os.getenv("IOTDB_PASSWORD", "root"),
        )

        args = parser.parse_args()
        return Config(
            host=args.host,
            port=args.port,
            database=args.database,
            user=args.user,
            password=args.password,
        )
