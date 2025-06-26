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

from iotdb_mcp_server.config import Config
import sys
import signal
import asyncio
import logging

if not "-m" in sys.argv:
    from . import server

# Configure logging for the main module
logger = logging.getLogger("iotdb_mcp_server")

def main():
    """Main entry point for the package."""
    # Create an event loop with proper exception handling
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Graceful shutdown handler
    def handle_shutdown(sig, frame):
        logger.info(f"Received shutdown signal {sig}, shutting down gracefully...")
        # Stop the event loop
        if loop.is_running():
            loop.stop()
    
    # Register signal handlers
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)
    
    try:
        logger.info("Starting IoTDB MCP Server...")
        # Run the server in the event loop
        loop.run_until_complete(server.main())
        # Keep the loop running until stopped
        loop.run_forever()
    except Exception as e:
        logger.error(f"Error in IoTDB MCP Server: {e}")
    finally:
        # Clean up tasks
        pending = asyncio.all_tasks(loop)
        if pending:
            logger.info(f"Cancelling {len(pending)} pending tasks...")
            for task in pending:
                task.cancel()
            # Wait for tasks to be cancelled
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        # Close the loop
        loop.close()
        logger.info("IoTDB MCP Server shutdown complete")
    
    return 0


# Expose important items at package level
__all__ = ["main", "server"]
