"""Outbound A2A client plumbing for calling remote A2A agents."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.types import AgentCard, Message, Part, Role, Task, TaskArtifactUpdateEvent, TaskStatusUpdateEvent, TextPart

from ...config import A2AConfig
from .diagnosis import build_send_message_envelope, envelope_digest

logger = logging.getLogger(__name__)

# Items yielded by Client.send_message during a message/send exchange.
SendMessageEvent = tuple[Task, TaskStatusUpdateEvent | TaskArtifactUpdateEvent | None] | Message

#: A2A protocol version header required by A2A 1.0 servers (a2a-sdk >= 1.0
#: rejects requests without it, defaulting to the unsupported 0.3 dialect).
A2A_VERSION_HEADER = {"A2A-Version": "1.0"}


@dataclass(frozen=True)
class DeliveryOutcome:
    """Result of a fire-and-forget case hand-off to a remote agent.

    ``submitted`` is True when the remote accepted the request (or, on the
    AgentCore path, when the request was transmitted but the response was not
    awaited). This client never polls for the final result.
    """

    submitted: bool
    reason: str | None = None
    task_id: str | None = None
    run_id: str | None = None


class A2AClientManager:
    """Config-driven manager for delegating to remote A2A agents.

    Plumbing only — nothing calls this yet. Remote agents are declared via the
    HELPDESK_AGENT_A2A_REMOTE_AGENTS env var (JSON map of name -> base URL).
    """

    def __init__(self, config: A2AConfig) -> None:
        self._config = config
        self._httpx_client: httpx.AsyncClient | None = None
        self._factory: ClientFactory | None = None
        self._cards: dict[str, AgentCard] = {}
        self._clients: dict[str, Client] = {}

    @property
    def agent_names(self) -> list[str]:
        """Names of configured remote agents."""
        return sorted(self._config.remote_agents)

    def has_target(self, name: str) -> bool:
        """True if the agent is reachable by any transport (base URL or runtime ARN)."""
        return name in self._config.remote_agents or name in self._config.remote_agent_runtime_arns

    async def _ensure_client_stack(self) -> tuple[httpx.AsyncClient, ClientFactory]:
        if self._httpx_client is None or self._factory is None:
            self._httpx_client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
            self._factory = ClientFactory(ClientConfig(streaming=False, httpx_client=self._httpx_client))
        return self._httpx_client, self._factory

    async def get_agent_card(self, name: str) -> AgentCard:
        """Fetch (and cache) the Agent Card of a configured remote agent."""
        if name not in self._config.remote_agents:
            raise KeyError(f"unknown A2A remote agent: {name!r}")
        if name not in self._cards:
            http_client, _ = await self._ensure_client_stack()
            resolver = A2ACardResolver(http_client, self._config.remote_agents[name])
            self._cards[name] = await resolver.get_agent_card()
        return self._cards[name]

    async def list_agents(self) -> dict[str, AgentCard]:
        """Return the Agent Cards of every configured remote agent."""
        return {name: await self.get_agent_card(name) for name in self.agent_names}

    async def _get_client(self, name: str) -> Client:
        if name not in self._clients:
            _, factory = await self._ensure_client_stack()
            card = await self.get_agent_card(name)
            logger.debug("creating A2A client for remote agent %s", name)
            self._clients[name] = factory.create(card)
        return self._clients[name]

    async def send_message(
        self,
        agent_name: str,
        text: str,
        context_id: str | None = None,
    ) -> list[SendMessageEvent]:
        """Send a text message via message/send and return the received events.

        ``context_id`` continues an existing conversation context on the remote
        agent (multi-turn); omit it to start a new one.
        """
        client = await self._get_client(agent_name)
        message = Message(
            kind="message",
            role=Role.user,
            message_id=uuid4().hex,
            parts=[Part(root=TextPart(text=text))],
            context_id=context_id,
        )
        events: list[SendMessageEvent] = []
        async for event in client.send_message(message):
            events.append(event)
        return events

    def _resolve_target(self, agent_name: str) -> tuple[str, str]:
        """Return (base_url, token) or raise KeyError for an unconfigured agent."""
        if agent_name not in self._config.remote_agents and agent_name not in self._config.remote_agent_runtime_arns:
            raise KeyError(f"unknown A2A remote agent: {agent_name!r}")
        base_url = self._config.remote_agents.get(agent_name, "")
        token = self._config.remote_agent_tokens.get(agent_name, "")
        return base_url, token

    async def send_case(
        self,
        agent_name: str,
        case_payload: dict[str, Any],
        context_id: str | None = None,
    ) -> DeliveryOutcome:
        """Hand a triaged case to a remote agent as an A2A 1.0 DataPart, fire-and-forget.

        Builds a ``SendMessage`` envelope and submits it, then returns without
        polling: the remote agent owns the investigation and delivers results
        itself (e.g. to Jira). Transport is chosen by configuration: an agent
        with a runtime ARN is invoked via SigV4-signed ``InvokeAgentRuntime``
        (the deployed AgentCore path); otherwise the envelope is POSTed as
        JSON-RPC to ``{base_url}/`` with a bearer token (local/dev path).
        """
        base_url, token = self._resolve_target(agent_name)
        task_id = uuid4().hex
        envelope = build_send_message_envelope(
            case_payload,
            task_id=task_id,
            context_id=context_id or uuid4().hex,
        )

        runtime_arn = self._config.remote_agent_runtime_arns.get(agent_name)
        if runtime_arn:
            return await self._send_via_agentcore(agent_name, runtime_arn, envelope, task_id, token)
        return await self._send_via_http(agent_name, base_url, envelope, task_id, token)

    async def _send_via_http(
        self,
        agent_name: str,
        base_url: str,
        envelope: dict[str, Any],
        task_id: str,
        token: str,
    ) -> DeliveryOutcome:
        """POST the JSON-RPC envelope directly to the remote agent's root path."""
        http_client, _ = await self._ensure_client_stack()
        headers = dict(A2A_VERSION_HEADER)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await http_client.post(f"{base_url.rstrip('/')}/", json=envelope, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("case hand-off to %s failed: %s", agent_name, exc)
            return DeliveryOutcome(
                submitted=False, reason=f"transport error: {exc.__class__.__name__}", task_id=task_id
            )
        if response.status_code >= 400:
            logger.warning("case hand-off to %s rejected: status=%s", agent_name, response.status_code)
            return DeliveryOutcome(
                submitted=False, reason=f"remote returned HTTP {response.status_code}", task_id=task_id
            )
        logger.info("case handed to %s via HTTP, task_id=%s", agent_name, task_id)
        return DeliveryOutcome(submitted=True, task_id=task_id)

    async def _send_via_agentcore(
        self,
        agent_name: str,
        runtime_arn: str,
        envelope: dict[str, Any],
        task_id: str,
        token: str,
    ) -> DeliveryOutcome:
        """Invoke the remote agent's AgentCore Runtime with the envelope as payload.

        Mirrors the diagnosis agent's own AgentCoreDeliverer: deterministic
        ``runtimeSessionId`` derived from the envelope digest so a retried
        hand-off reuses the same run id, and a client-side read timeout counts
        as submitted (the request was already transmitted; the remote run is
        long-running by design). boto3 is imported lazily so local dev does
        not require it.
        """
        if not token:
            # AgentCore forwards only the custom authorization header to the
            # container; without it the runtime rejects the call with 401.
            logger.warning(
                "case hand-off to %s via AgentCore skipped: no bearer token configured "
                "(set HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKENS)",
                agent_name,
            )
            return DeliveryOutcome(
                submitted=False,
                reason="missing bearer token for AgentCore runtime",
                task_id=task_id,
            )

        run_id = str(uuid5(NAMESPACE_URL, f"{agent_name}:{envelope_digest(envelope)}"))
        payload = json.dumps(envelope).encode("utf-8")

        def _invoke() -> dict[str, Any] | None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "bedrock-agentcore",
                region_name=self._config.agentcore_region,
                # total_max_attempts=1: InvokeAgentRuntime is not idempotent —
                # a botocore retried read timeout starts a SECOND investigation
                # on the remote side. Fire-and-forget must never duplicate.
                config=Config(
                    connect_timeout=10,
                    read_timeout=self._config.timeout_seconds,
                    retries={"total_max_attempts": 1},
                ),
            )

            # InvokeAgentRuntime models no header parameter, and the standard
            # Authorization header is consumed by the SigV4 signature, so the
            # bearer token and the A2A protocol version go on via a before-sign
            # hook — the documented boto3 mechanism, and the only carrier the
            # diagnosis agent's runtime forwards to the container. Note:
            # botocore's HTTPHeaders supports item assignment but not
            # dict-style .update().
            def _add_a2a_headers(request: Any, **kwargs: Any) -> None:
                for name, value in A2A_VERSION_HEADER.items():
                    request.headers[name] = value
                request.headers["X-Amzn-Bedrock-AgentCore-Runtime-Custom-Authorization"] = f"Bearer {token}"

            client.meta.events.register("before-sign.bedrock-agentcore.InvokeAgentRuntime", _add_a2a_headers)
            kwargs: dict[str, Any] = {
                "agentRuntimeArn": runtime_arn,
                "runtimeSessionId": run_id,
                "qualifier": "DEFAULT",
                "contentType": "application/json",
                "accept": "application/json",
                "payload": payload,
            }
            response = client.invoke_agent_runtime(**kwargs)
            body = response.get("response")
            if body is None:
                return None
            try:
                return json.loads(body.read())
            except Exception:  # noqa: BLE001 - an unreadable body is not a hand-off failure
                return None

        try:
            result = await asyncio.to_thread(_invoke)
        except Exception as exc:  # noqa: BLE001 - hand-off failure must not sink the workflow
            from botocore.exceptions import ReadTimeoutError

            if isinstance(exc, ReadTimeoutError):
                logger.info("case handed to %s via AgentCore; response not awaited, run_id=%s", agent_name, run_id)
                return DeliveryOutcome(
                    submitted=True, reason="submitted; response not awaited", task_id=task_id, run_id=run_id
                )
            logger.warning("case hand-off to %s via AgentCore failed: %s", agent_name, exc)
            return DeliveryOutcome(
                submitted=False, reason=f"AgentCore error: {exc.__class__.__name__}", task_id=task_id, run_id=run_id
            )
        if isinstance(result, dict) and "error" in result:
            error = result["error"]
            message = error.get("message", error) if isinstance(error, dict) else error
            logger.warning("case hand-off to %s via AgentCore returned JSON-RPC error: %s", agent_name, message)
            return DeliveryOutcome(submitted=False, reason=f"JSON-RPC error: {message}", task_id=task_id, run_id=run_id)
        logger.info("case handed to %s via AgentCore, run_id=%s", agent_name, run_id)
        return DeliveryOutcome(submitted=True, task_id=task_id, run_id=run_id)

    async def aclose(self) -> None:
        """Release the shared HTTP client and drop cached connections."""
        if self._httpx_client is not None:
            await self._httpx_client.aclose()
        self._httpx_client = None
        self._factory = None
        self._clients = {}
