"""TEST FIXTURE — a real MCP server built with the official MCP Python SDK (used only by tests)."""
import sys
from mcp.server.mcpserver import MCPServer
m = MCPServer("demo")
@m.tool()
def add(a: int, b: int) -> int:
    """Add two numbers"""
    return a + b
@m.tool()
def plant_status(unit: str) -> str:
    """Return the (fictional) running status of a unit"""
    return f"Unit {unit} is running normally (fictional test data)."


if __name__ == "__main__":
    t = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if t == "http":
        m.run("streamable-http", port=int(sys.argv[2]))
    else:
        m.run("stdio")
