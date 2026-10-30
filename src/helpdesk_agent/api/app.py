from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from a2a.server.apps import A2AFastAPIApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from fastapi import Depends, FastAPI, HTTPException

from helpdesk_agent.api.a2a.agent_card import A2A_RPC_PATH, build_agent_card
from helpdesk_agent.api.a2a.executor import HelpdeskAgentExecutor
from helpdesk_agent.api.dependencies import get_a2a_config, get_conversation_store, get_llm_config, get_workflow
from helpdesk_agent.api.routes.chat import router as chat_router
from helpdesk_agent.api.routes.email import email_webhook
from helpdesk_agent.api.routes.email import router as email_router
from helpdesk_agent.api.routes.ticketing import JiraMemoryWebhook, jira_memory_webhook
from helpdesk_agent.api.routes.ticketing import router as ticketing_router
from helpdesk_agent.api.schemas.email import EmailWebhook
from helpdesk_agent.observability.tracing import flush_observability, setup_observability
from helpdesk_agent.workflows.workflow import HelpdeskWorkflow


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await asyncio.to_thread(flush_observability)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # Ensure helpdesk_agent stage logs render even when uvicorn's own log
    # config doesn't propagate them.
    logging.basicConfig(
        level=os.getenv("HELPDESK_AGENT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Validate LLM configuration at startup (fail fast)
    llm_config = get_llm_config()
    if not llm_config.api_key:
        raise RuntimeError(
            "HELPDESK_AGENT_LLM_API_KEY environment variable is required. "
            "Set it to your Bedrock gateway auth token or OpenAI API key."
        )

    app = FastAPI(title="Helpdesk Agent", lifespan=_lifespan)
    setup_observability(app)

    # Health endpoints
    @app.get("/")
    async def hello() -> dict[str, str]:
        return {"message": "helpdesk agent is running"}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "healthy"}

    # Bedrock AgentCore runtime contract: GET /ping + POST /invocations.
    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"status": "Healthy"}

    @app.post("/invocations")
    async def invocations(
        body: dict[str, Any],
        workflow: Annotated[HelpdeskWorkflow, Depends(get_workflow)],
    ) -> dict[str, Any]:
        path = body.get("path", "/webhooks/email")
        payload = body.get("payload", body)
        if path == "/webhooks/jira":
            return await jira_memory_webhook(JiraMemoryWebhook.model_validate(payload), workflow)
        if path != "/webhooks/email":
            raise HTTPException(status_code=404, detail=f"unsupported path: {path}")
        return await email_webhook(EmailWebhook.model_validate(payload), workflow)

    # Include module routers
    app.include_router(email_router)
    app.include_router(chat_router)
    app.include_router(ticketing_router)

    # Mount the A2A (Agent2Agent) interface: Agent Card at
    # /.well-known/agent-card.json and JSON-RPC message/send at /a2a.
    # The email webhook and /invocations Lambda path are unaffected.
    a2a_request_handler = DefaultRequestHandler(
        agent_executor=HelpdeskAgentExecutor(get_workflow(), get_conversation_store()),
        task_store=InMemoryTaskStore(),
    )
    A2AFastAPIApplication(
        agent_card=build_agent_card(get_a2a_config()),
        http_handler=a2a_request_handler,
    ).add_routes_to_app(app, rpc_url=A2A_RPC_PATH)

    return app
