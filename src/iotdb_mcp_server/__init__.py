from iotdb_mcp_server.config import Config
import sys

if not "-m" in sys.argv:
    from . import server
import asyncio


def main():
    """Main entry point for the package."""
    _config = Config.from_env_arguments()
    asyncio.run(server.main(_config))


# Expose important items at package level
__all__ = ["main", "server"]
