"""Entry point that serves the CloudWatch MCP server over streamable-HTTP for Bedrock AgentCore Runtime."""

from awslabs.cloudwatch_mcp_server.server import mcp


if __name__ == "__main__":
    # AgentCore Runtime requires MCP servers to listen on 0.0.0.0:8000/mcp
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = 8000
    mcp.run(transport="streamable-http")
