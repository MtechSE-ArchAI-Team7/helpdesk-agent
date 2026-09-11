import json
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()


@dataclass
class MCPConfig:
    base_url: str = "http://127.0.0.1:8765/mcp"
    timeout_seconds: float = 5.0
    enable_async: bool = False
    auth_token: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # Optional URL of the real mcp-atlassian server. When set, jira.* and
    # confluence.* tools are routed there; everything else uses base_url.
    atlassian_url: str | None = None
    # Optional URL of the Zoho Mail MCP server. When set, zohomail.* tools are
    # routed there instead of base_url.
    zohomail_url: str | None = None
    # Optional URL of the FAQ MCP server. When set, faq.* tools are routed
    # there instead of base_url.
    faq_url: str | None = None
    # Prefix prepended to mcp-atlassian tool names (e.g. "atlassian_" when the
    # server is mounted behind a gateway that namespaces its tools).
    atlassian_tool_prefix: str = ""
    # Jira project key used when creating issues via mcp-atlassian.
    jira_project_key: str = "HELP"

    @classmethod
    def from_env(cls) -> "MCPConfig":
        auth_token = os.getenv("HELPDESK_AGENT_MCP_AUTH_TOKEN")
        headers: dict[str, str] = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        return cls(
            base_url=os.getenv("HELPDESK_AGENT_MCP_BASE_URL", "http://127.0.0.1:8765/mcp"),
            timeout_seconds=float(os.getenv("HELPDESK_AGENT_MCP_TIMEOUT_SECONDS", "5.0")),
            enable_async=os.getenv("HELPDESK_AGENT_MCP_ENABLE_ASYNC", "0") == "1",
            auth_token=auth_token,
            headers=headers,
            atlassian_url=os.getenv("HELPDESK_AGENT_MCP_ATLASSIAN_URL") or None,
            zohomail_url=os.getenv("HELPDESK_AGENT_MCP_ZOHOMAIL_URL") or None,
            faq_url=os.getenv("HELPDESK_AGENT_MCP_FAQ_URL") or None,
            atlassian_tool_prefix=os.getenv("HELPDESK_AGENT_MCP_TOOL_PREFIX", ""),
            jira_project_key=os.getenv("HELPDESK_AGENT_JIRA_PROJECT_KEY", "HELP"),
        )


@dataclass
class LLMConfig:
    """Configuration for LLM client."""

    api_key: str | None = None
    model: str = "gpt-4o-mini"
    base_url: str | None = None
    timeout_seconds: float = 30.0

    @staticmethod
    def _normalize_base_url(base_url: str | None) -> str | None:
        """Normalize Bedrock AgentCore gateway URLs to the OpenAI-compatible inference path."""
        if not base_url:
            return None

        normalized = base_url.rstrip("/")

        if "bedrock-agentcore" in normalized and "/inference" not in normalized:
            return f"{normalized}/inference/v1"

        if normalized.endswith("/inference"):
            return f"{normalized}/v1"

        if normalized.endswith("/inference/v1"):
            return normalized

        return normalized

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            api_key=os.getenv("HELPDESK_AGENT_LLM_API_KEY"),
            model=os.getenv("HELPDESK_AGENT_LLM_MODEL", "gpt-4o-mini"),
            base_url=cls._normalize_base_url(os.getenv("HELPDESK_AGENT_LLM_BASE_URL")),
            timeout_seconds=float(os.getenv("HELPDESK_AGENT_LLM_TIMEOUT_SECONDS", "30.0")),
        )


@dataclass
class TriageConfig:
    """Configuration for triage behavior."""

    confidence_threshold: float = 0.6
    faq_relevance_threshold: float = 0.7

    @classmethod
    def from_env(cls) -> "TriageConfig":
        return cls(
            confidence_threshold=float(os.getenv("HELPDESK_AGENT_TRIAGE_CONFIDENCE_THRESHOLD", "0.6")),
            faq_relevance_threshold=float(os.getenv("HELPDESK_AGENT_FAQ_RELEVANCE_THRESHOLD", "0.7")),
        )


@dataclass
class A2AConfig:
    """Configuration for the A2A (Agent2Agent) protocol interface."""

    base_url: str = "http://localhost:8080"
    agent_name: str = "helpdesk-agent"
    timeout_seconds: float = 30.0
    # Remote A2A agents this agent may call, as a name -> base URL map.
    remote_agents: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "A2AConfig":
        remote_raw = os.getenv("HELPDESK_AGENT_A2A_REMOTE_AGENTS", "").strip()
        remote_agents: dict[str, str] = {}
        if remote_raw:
            try:
                parsed = json.loads(remote_raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "HELPDESK_AGENT_A2A_REMOTE_AGENTS must be a JSON object mapping agent names to base URLs, "
                    f'e.g. {{"jira-bot": "http://localhost:9000"}}: {exc}'
                ) from exc
            if not isinstance(parsed, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
            ):
                raise ValueError(
                    "HELPDESK_AGENT_A2A_REMOTE_AGENTS must be a JSON object mapping agent names to base URLs"
                )
            remote_agents = dict(parsed)

        return cls(
            base_url=os.getenv("HELPDESK_AGENT_A2A_BASE_URL", "http://localhost:8080"),
            agent_name=os.getenv("HELPDESK_AGENT_A2A_AGENT_NAME", "helpdesk-agent"),
            timeout_seconds=float(os.getenv("HELPDESK_AGENT_A2A_TIMEOUT_SECONDS", "30.0")),
            remote_agents=remote_agents,
        )
