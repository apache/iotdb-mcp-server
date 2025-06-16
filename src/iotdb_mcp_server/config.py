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
    IoTDB database
    """

    sql_dialect: str
    """
    SQL dialect: tree or table
    """
    
    export_path: str
    """
    Path for exporting query results
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
        
        parser.add_argument(
            "--database",
            type=str,
            help="IoTDB connect database name",
            default=os.getenv("IOTDB_DATABASE", "test"),
        )

        parser.add_argument(
            "--sql-dialect",
            type=str,
            help="SQL dialect: tree or table",
            default=os.getenv("IOTDB_SQL_DIALECT", "table"),
        )
        
        parser.add_argument(
            "--export-path",
            type=str,
            help="Path for exporting query results",
            default=os.getenv("IOTDB_EXPORT_PATH", "/tmp"),
        )

        args = parser.parse_args()
        return Config(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            database=args.database,
            sql_dialect=args.sql_dialect,
            export_path=args.export_path,
        )
