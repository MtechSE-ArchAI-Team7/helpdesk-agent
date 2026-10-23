from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Protocol

from botocore.exceptions import BotoCoreError, ClientError

from helpdesk_agent.application.context import bounded_snippets, redact_shared_text, shared_snapshot
from helpdesk_agent.config import MemoryConfig
from helpdesk_agent.domain.exceptions import MemoryError

logger = logging.getLogger(__name__)

SUMMARY_NAMESPACE = "/org/{actorId}/tickets/{sessionId}/summary/"
FACTS_NAMESPACE = "/org/{actorId}/tickets/{sessionId}/facts/"


class MemoryClient(Protocol):
    def create_event(self, **kwargs: Any) -> dict[str, Any]: ...

    def list_events(self, **kwargs: Any) -> dict[str, Any]: ...

    def retrieve_memory_records(self, **kwargs: Any) -> dict[str, Any]: ...


class AgentCoreMemory:
    def __init__(self, config: MemoryConfig, client: MemoryClient | None = None) -> None:
        self.config = config
        if client is None:
            import boto3
            from botocore.config import Config

            sdk_config = Config(
                connect_timeout=config.timeout_seconds,
                read_timeout=config.timeout_seconds,
                retries={"mode": "standard", "total_max_attempts": 2},
            )
            control = boto3.client("bedrock-agentcore-control", region_name=config.region, config=sdk_config)
            try:
                resource = control.get_memory(memoryId=config.memory_id)["memory"]
            except (BotoCoreError, ClientError) as exc:
                logger.error("AgentCore memory startup validation error_type=%s", type(exc).__name__)
                raise MemoryError(f"Could not validate AgentCore Memory ({type(exc).__name__})") from exc
            templates = {
                template
                for strategy in resource.get("strategies", [])
                if strategy.get("status") == "ACTIVE"
                for template in (strategy.get("namespaceTemplates") or strategy.get("namespaces") or [])
            }
            if resource.get("status") != "ACTIVE" or not {SUMMARY_NAMESPACE, FACTS_NAMESPACE}.issubset(templates):
                raise MemoryError("AgentCore Memory must be ACTIVE with the documented summary and facts namespaces")
            client = boto3.client("bedrock-agentcore", region_name=config.region, config=sdk_config)
        self.client = client

    @property
    def context_chars(self) -> int:
        return self.config.context_chars

    @property
    def namespace(self) -> str:
        return f"/org/{self.config.organization_id}/tickets/"

    def _scope(self, session_id: str) -> dict[str, str]:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", session_id):
            raise MemoryError("Ticket ID is not a valid AgentCore session ID")
        return {
            "memoryId": self.config.memory_id,
            "actorId": self.config.organization_id,
            "sessionId": session_id,
        }

    async def _call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if operation == "create_event":
                return await asyncio.to_thread(self.client.create_event, **arguments)
            if operation == "list_events":
                return await asyncio.to_thread(self.client.list_events, **arguments)
            if operation == "retrieve_memory_records":
                return await asyncio.to_thread(self.client.retrieve_memory_records, **arguments)
            raise ValueError(f"Unsupported memory operation: {operation}")
        except (BotoCoreError, ClientError) as exc:
            # AWS error messages can echo request content; log only operation and error type.
            logger.error("AgentCore memory operation=%s error_type=%s", operation, type(exc).__name__)
            raise MemoryError(f"AgentCore {operation} failed ({type(exc).__name__})") from exc

    async def _write(
        self,
        session_id: str,
        payload: list[dict[str, Any]],
        *,
        identity: str,
        extract: bool = False,
    ) -> None:
        token = hashlib.sha256(f"{self.config.organization_id}:{session_id}:{identity}".encode()).hexdigest()
        arguments: dict[str, Any] = {
            **self._scope(session_id),
            "eventTimestamp": datetime.now(timezone.utc),
            "payload": payload,
            "clientToken": token,
            "metadata": {"request_id": {"stringValue": hashlib.sha256(identity.encode()).hexdigest()}},
        }
        if not extract:
            arguments["extractionMode"] = "SKIP"
        await self._call("create_event", arguments)

    async def save_message(self, ticket_id: str, role: str, content: str, identity: str) -> None:
        if role not in ("USER", "ASSISTANT"):
            raise MemoryError("Memory message role must be USER or ASSISTANT")
        await self._write(
            ticket_id,
            [{"conversational": {"role": role, "content": {"text": content[:50000]}}}],
            identity=f"{identity}:{role}",
        )

    async def history(self, ticket_id: str, incoming_identity: str) -> list[str]:
        arguments: dict[str, Any] = {
            **self._scope(ticket_id),
            "includePayloads": True,
            "maxResults": min(self.config.history_events, 100),
        }
        events: list[dict[str, Any]] = []
        # ListEvents returns newest events first. Bound both retrieval and model context.
        while len(events) < self.config.history_events:
            response = await self._call("list_events", arguments)
            events.extend(response.get("events", []))
            token = response.get("nextToken")
            if not token or not response.get("events"):
                break
            arguments["nextToken"] = token
            arguments["maxResults"] = self.config.history_events - len(events)
        excluded = hashlib.sha256(f"{incoming_identity}:USER".encode()).hexdigest()
        newest_snippets: list[str] = []
        for event in events[: self.config.history_events]:
            if event.get("metadata", {}).get("request_id", {}).get("stringValue") == excluded:
                continue
            for item in reversed(event.get("payload", [])):
                message = item.get("conversational")
                if message:
                    newest_snippets.append(f"{message['role']}: {message['content']['text']}")
        return list(reversed(bounded_snippets(newest_snippets, self.config.context_chars)))

    async def similar(self, ticket_id: str, query: str) -> list[dict[str, Any]]:
        response = await self._call(
            "retrieve_memory_records",
            {
                "memoryId": self.config.memory_id,
                "namespacePath": self.namespace,
                "searchCriteria": {
                    "searchQuery": redact_shared_text(query)[:10000],
                    "topK": min(self.config.similar_cases * 4, 100),
                },
            },
        )
        results: list[dict[str, Any]] = []
        by_ticket: dict[str, dict[str, Any]] = {}
        for record in response.get("memoryRecordSummaries", []):
            namespaces = record.get("namespaces", [])
            if not namespaces or not namespaces[0].startswith(self.namespace):
                continue
            source_ticket = namespaces[0][len(self.namespace) :].split("/")[0]
            if source_ticket == ticket_id or not source_ticket:
                continue
            content = record.get("content", {}).get("text", "")
            if not content:
                continue
            content_limit = self.config.context_chars // self.config.similar_cases
            if source_ticket in by_ticket:
                case = by_ticket[source_ticket]
                case["content"] = f"{case['content']}\n{redact_shared_text(content)}"[:content_limit]
            elif len(results) < self.config.similar_cases:
                case = {
                    "ticket_id": source_ticket,
                    "content": redact_shared_text(content)[:content_limit],
                    "score": record.get("score"),
                    "qualification": "Historical report/troubleshooting; not a verified fix for this ticket.",
                }
                results.append(case)
                by_ticket[source_ticket] = case
        return results

    async def save_snapshot(self, ticket_id: str, snapshot: dict[str, Any], identity: str) -> None:
        content = shared_snapshot(
            {
                "source_ticket": ticket_id,
                "qualification": (
                    "Reported observations and troubleshooting, not independently verified facts. "
                    "An FAQ suggestion or diagnosis hand-off is not a confirmed resolution."
                ),
                **snapshot,
            }
        )
        if len(json.dumps(content).encode()) > 100000:
            raise MemoryError("Shared ticket snapshot exceeds the AgentCore JSON payload limit")
        # Structured JSON, unlike raw turns, is submitted to the extraction strategies.
        await self._write(
            ticket_id,
            [{"json": {"content": content}}],
            identity=f"{identity}:snapshot",
            extract=True,
        )

    @staticmethod
    def _conversation_session(conversation_id: str) -> str:
        return f"chat-{hashlib.sha256(conversation_id.encode()).hexdigest()}"

    async def conversation_ticket(self, conversation_id: str) -> str | None:
        response = await self._call(
            "list_events",
            {
                **self._scope(self._conversation_session(conversation_id)),
                "maxResults": 1,
                "includePayloads": True,
            },
        )
        for event in response.get("events", []):
            for item in event.get("payload", []):
                binding = item.get("blob")
                if isinstance(binding, dict) and isinstance(binding.get("ticket_id"), str):
                    return str(binding["ticket_id"])
        return None

    async def bind_conversation(self, conversation_id: str, ticket_id: str) -> None:
        await self._write(
            self._conversation_session(conversation_id),
            [{"blob": {"ticket_id": ticket_id}}],
            identity=f"bind:{ticket_id}",
        )
