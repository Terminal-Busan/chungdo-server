import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_ping_over_stdio():
    async def check():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / "mcp_server.py")],
        )
        async with asyncio.timeout(15):
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert "ping" in [tool.name for tool in tools.tools]
                    result = await session.call_tool("ping", {})
                    assert not result.is_error
                    assert result.content[0].text == "pong"

    asyncio.run(check())
