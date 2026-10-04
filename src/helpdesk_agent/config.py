import json
import os
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()


@dataclass
class LangfuseConfig:
    enabled: bool = False
    base_url: str = "https://cloud.langfuse.com"
    public_key: str | None = field(default=None, repr=False)
    secret_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "LangfuseConfig":
        enabled = os.getenv("HELPDESK_AGENT_LANGFUSE_ENABLED", "").lower() in ("1", "true", "yes")
        config = cls(
            enabled=enabled,
            base_url=os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com").rstrip("/"),
            public_key=os.getenv("LANGFUSE_PUBLIC_KEY") or None,
            secret_key=os.getenv("LANGFUSE_SECRET_KEY") or None,
        )
        if not enabled:
            return config
        secret_id = os.getenv("HELPDESK_AGENT_LANGFUSE_SECRET_ARN")
        if secret_id and (not config.public_key or not config.secret_key):
            import boto3

            client = boto3.client(
                "secretsmanager", region_name=os.getenv("HELPDESK_AGENT_LANGFUSE_SECRET_REGION") or None
            )
            credentials = json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])
            if not isinstance(credentials, dict):
                raise ValueError("Langfuse secret must be a JSON object")
            config.public_key = config.public_key or credentials.get("public_key")
            config.secret_key = config.secret_key or credentials.get("secret_key")
        if not all(isinstance(value, str) and value.strip() for value in (config.public_key, config.secret_key)):
            raise ValueError("Langfuse requires public and secret keys")
        if not config.base_url.startswith("https://"):
            raise ValueError("Langfuse Cloud requires an HTTPS base URL")
        return config


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
    # Prefix prepended to Zoho Mail tool names (server uses "ZohoMail_").
    zohomail_tool_prefix: str = "ZohoMail_"
    # Helpdesk mailbox address; required as fromAddress on Zoho sends/replies.
    zohomail_from_address: str | None = None
    # Optional URL of the FAQ MCP server. When set, faq.* tools are routed
    # there instead of base_url.
    faq_url: str | None = None
    faq_tool_prefix: str | None = None
    faq_auth_mode: str = "bearer"
    faq_region: str | None = None
    # Optional URL of the hosted Playwright MCP runtime.
    playwright_url: str | None = None
    playwright_timeout_seconds: float = 30.0
    playwright_allowed_hosts: list[str] = field(default_factory=list)
    playwright_tool_prefix: str = "mcp-playwright___"
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
            zohomail_tool_prefix=os.getenv("HELPDESK_AGENT_MCP_ZOHOMAIL_TOOL_PREFIX", "ZohoMail_"),
            zohomail_from_address=os.getenv("HELPDESK_AGENT_ZOHOMAIL_FROM_ADDRESS") or None,
            faq_url=os.getenv("HELPDESK_AGENT_MCP_FAQ_URL") or None,
            faq_tool_prefix=os.getenv("HELPDESK_AGENT_MCP_FAQ_TOOL_PREFIX"),
            faq_auth_mode=os.getenv("HELPDESK_AGENT_MCP_FAQ_AUTH_MODE", "bearer").lower(),
            faq_region=os.getenv("HELPDESK_AGENT_MCP_FAQ_REGION") or None,
            playwright_url=os.getenv("HELPDESK_AGENT_MCP_PLAYWRIGHT_URL") or None,
            playwright_timeout_seconds=float(os.getenv("HELPDESK_AGENT_MCP_PLAYWRIGHT_TIMEOUT_SECONDS", "30.0")),
            playwright_allowed_hosts=[
                host.strip().lower().rstrip(".")
                for host in os.getenv("HELPDESK_AGENT_PLAYWRIGHT_ALLOWED_HOSTS", "").split(",")
                if host.strip()
            ],
            playwright_tool_prefix=os.getenv("HELPDESK_AGENT_MCP_PLAYWRIGHT_TOOL_PREFIX", "mcp-playwright___"),
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
    max_clarification_rounds: int = 2

    def __post_init__(self) -> None:
        if self.max_clarification_rounds < 0:
            raise ValueError("HELPDESK_AGENT_MAX_CLARIFICATION_ROUNDS must be non-negative")

    @classmethod
    def from_env(cls) -> "TriageConfig":
        return cls(
            confidence_threshold=float(os.getenv("HELPDESK_AGENT_TRIAGE_CONFIDENCE_THRESHOLD", "0.6")),
            faq_relevance_threshold=float(os.getenv("HELPDESK_AGENT_FAQ_RELEVANCE_THRESHOLD", "0.7")),
            max_clarification_rounds=int(os.getenv("HELPDESK_AGENT_MAX_CLARIFICATION_ROUNDS", "2")),
        )


@dataclass
class MemoryConfig:
    """One organization's ticket memory, accessed using the AWS credential chain."""

    enabled: bool = False
    memory_id: str = ""
    region: str = ""
    organization_id: str = ""
    history_events: int = 30
    context_chars: int = 12000
    similar_cases: int = 5
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not 1 <= self.history_events <= 100:
            raise ValueError("HELPDESK_AGENT_MEMORY_HISTORY_EVENTS must be between 1 and 100")
        if not 1000 <= self.context_chars <= 50000:
            raise ValueError("HELPDESK_AGENT_MEMORY_CONTEXT_CHARS must be between 1000 and 50000")
        if not 1 <= self.similar_cases <= 10:
            raise ValueError("HELPDESK_AGENT_MEMORY_SIMILAR_CASES must be between 1 and 10")
        if self.timeout_seconds <= 0:
            raise ValueError("HELPDESK_AGENT_MEMORY_TIMEOUT_SECONDS must be positive")
        if self.enabled:
            if not self.memory_id or not self.region:
                raise ValueError("Enabled memory requires HELPDESK_AGENT_MEMORY_ID and HELPDESK_AGENT_MEMORY_REGION")
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.organization_id):
                raise ValueError("HELPDESK_AGENT_MEMORY_ORGANIZATION_ID must be a lowercase ID without slashes")

    @classmethod
    def from_env(cls) -> "MemoryConfig":
        enabled = os.getenv("HELPDESK_AGENT_MEMORY_ENABLED", "0")
        if enabled not in ("0", "1"):
            raise ValueError("HELPDESK_AGENT_MEMORY_ENABLED must be 0 or 1")
        return cls(
            enabled=enabled == "1",
            memory_id=os.getenv("HELPDESK_AGENT_MEMORY_ID", "").strip(),
            region=os.getenv("HELPDESK_AGENT_MEMORY_REGION", "").strip(),
            organization_id=os.getenv("HELPDESK_AGENT_MEMORY_ORGANIZATION_ID", "").strip(),
            history_events=int(os.getenv("HELPDESK_AGENT_MEMORY_HISTORY_EVENTS", "30")),
            context_chars=int(os.getenv("HELPDESK_AGENT_MEMORY_CONTEXT_CHARS", "12000")),
            similar_cases=int(os.getenv("HELPDESK_AGENT_MEMORY_SIMILAR_CASES", "5")),
            timeout_seconds=float(os.getenv("HELPDESK_AGENT_MEMORY_TIMEOUT_SECONDS", "5.0")),
        )


@dataclass
class A2AConfig:
    """Configuration for the A2A (Agent2Agent) protocol interface."""

    base_url: str = "http://localhost:8080"
    agent_name: str = "helpdesk-agent"
    timeout_seconds: float = 30.0
    # Remote A2A agents this agent may call, as a name -> base URL map.
    remote_agents: dict[str, str] = field(default_factory=dict)
    # Bearer tokens for remote agents that require auth, as a name -> token map.
    remote_agent_tokens: dict[str, str] = field(default_factory=dict)
    # Secrets Manager pointers for remote-agent tokens, as a name -> secret
    # ARN (or secret name) map. Resolved via GetSecretValue at startup under
    # the default AWS credential chain; a literal token in remote_agent_tokens
    # for the same agent wins, so local dev overrides are never replaced.
    remote_agent_token_secret_arns: dict[str, str] = field(default_factory=dict)
    # AgentCore Runtime ARNs for remote agents, as a name -> ARN map. An agent
    # present here is invoked via SigV4-signed InvokeAgentRuntime instead of
    # direct HTTP to its base URL.
    remote_agent_runtime_arns: dict[str, str] = field(default_factory=dict)
    # Region of the AgentCore runtimes in remote_agent_runtime_arns.
    agentcore_region: str = "ap-southeast-1"

    @staticmethod
    def _parse_name_map(env_var: str, description: str, example: str) -> dict[str, str]:
        raw = os.getenv(env_var, "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{env_var} must be a JSON object mapping agent names to {description}, e.g. {example}: {exc}"
            ) from exc
        if not isinstance(parsed, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
        ):
            raise ValueError(f"{env_var} must be a JSON object mapping agent names to {description}")
        return dict(parsed)

    @staticmethod
    def _resolve_secret_tokens(secret_arns: dict[str, str]) -> dict[str, str]:
        """Resolve Secrets Manager pointers to token values via GetSecretValue.

        Accepts secret ARNs or plain secret names as SecretId. Secrets stored
        as a JSON object resolve to their single value (multi-key secrets are
        rejected — ambiguous). boto3 is imported lazily so local dev without
        pointers never needs AWS credentials.
        """
        import boto3

        client = boto3.client("secretsmanager")
        resolved: dict[str, str] = {}
        for agent_name, secret_id in secret_arns.items():
            value = client.get_secret_value(SecretId=secret_id)["SecretString"]
            value = value.strip()
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                resolved[agent_name] = value
                continue
            if isinstance(parsed, dict) and len(parsed) == 1:
                resolved[agent_name] = str(next(iter(parsed.values())))
            else:
                raise ValueError(
                    f"secret {secret_id!r} for agent {agent_name!r} is JSON with "
                    f"{len(parsed)} keys; expected a plain string or a single-key object"
                )
        return resolved

    @classmethod
    def from_env(cls) -> "A2AConfig":
        remote_agents = cls._parse_name_map(
            "HELPDESK_AGENT_A2A_REMOTE_AGENTS",
            "base URLs",
            '{"jira-bot": "http://localhost:9000"}',
        )
        remote_agent_tokens = cls._parse_name_map(
            "HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKENS",
            "bearer tokens",
            '{"diagnosis-agent": "secret-token"}',
        )
        remote_agent_token_secret_arns = cls._parse_name_map(
            "HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKEN_SECRET_ARNS",
            "Secrets Manager secret ARNs",
            '{"diagnosis-agent": "arn:aws:secretsmanager:ap-southeast-1:123456789012:secret:example"}',
        )
        remote_agent_runtime_arns = cls._parse_name_map(
            "HELPDESK_AGENT_A2A_REMOTE_AGENT_ARNS",
            "AgentCore runtime ARNs",
            '{"diagnosis-agent": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/abc"}',
        )

        pending_secret_arns = {
            name: arn for name, arn in remote_agent_token_secret_arns.items() if name not in remote_agent_tokens
        }
        if pending_secret_arns:
            remote_agent_tokens.update(cls._resolve_secret_tokens(pending_secret_arns))

        return cls(
            base_url=os.getenv("HELPDESK_AGENT_A2A_BASE_URL", "http://localhost:8080"),
            agent_name=os.getenv("HELPDESK_AGENT_A2A_AGENT_NAME", "helpdesk-agent"),
            timeout_seconds=float(os.getenv("HELPDESK_AGENT_A2A_TIMEOUT_SECONDS", "30.0")),
            remote_agents=remote_agents,
            remote_agent_tokens=remote_agent_tokens,
            remote_agent_token_secret_arns=remote_agent_token_secret_arns,
            remote_agent_runtime_arns=remote_agent_runtime_arns,
            agentcore_region=os.getenv("HELPDESK_AGENT_A2A_AGENTCORE_REGION", "ap-southeast-1"),
        )
