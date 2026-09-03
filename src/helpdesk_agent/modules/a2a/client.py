"""Outbound A2A client plumbing for calling remote A2A agents."""

from __future__ import annotations

import logging
from uuid import uuid4

import httpx
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.types import AgentCard, Message, Part, Role, Task, TaskArtifactUpdateEvent, TaskStatusUpdateEvent, TextPart

from ...config import A2AConfig

logger = logging.getLogger(__name__)

# Items yielded by Client.send_message during a message/send exchange.
SendMessageEvent = tuple[Task, TaskStatusUpdateEvent | TaskArtifactUpdateEvent | None] | Message


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

    async def aclose(self) -> None:
        """Release the shared HTTP client and drop cached connections."""
        if self._httpx_client is not None:
            await self._httpx_client.aclose()
        self._httpx_client = None
        self._factory = None
        self._clients = {}
