"""Chungdo tools exposed through the MCP stdio transport."""

from mcp.server import MCPServer

server = MCPServer("chungdo", version="0.0.1")


@server.tool()
def ping() -> str:
    """Check that the Chungdo MCP server responds."""
    return "pong"


if __name__ == "__main__":
    server.run(transport="stdio")
