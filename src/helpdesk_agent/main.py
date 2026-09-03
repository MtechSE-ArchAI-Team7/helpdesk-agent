from __future__ import annotations

import os
from typing import Annotated, Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException

from .core.cases import CaseStore
from .core.workflow import HelpdeskWorkflow
from .dependencies import get_case_store, get_llm_config, get_workflow
from .modules.chat.router import router as chat_router
from .modules.email.router import email_webhook
from .modules.email.router import router as email_router
from .modules.email.schema import EmailWebhook


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    # Validate LLM configuration at startup (fail fast)
    llm_config = get_llm_config()
    if not llm_config.api_key:
        raise RuntimeError(
            "HELPDESK_AGENT_LLM_API_KEY environment variable is required. "
            "Set it to your OpenAI API key."
        )

    app = FastAPI(title="Helpdesk Agent")

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
        case_store: Annotated[CaseStore, Depends(get_case_store)],
    ) -> dict[str, Any]:
        path = body.get("path", "/webhooks/email")
        if path != "/webhooks/email":
            raise HTTPException(status_code=404, detail=f"unsupported path: {path}")
        payload = body.get("payload", body)
        return await email_webhook(EmailWebhook.model_validate(payload), workflow, case_store)

    # Include module routers
    app.include_router(email_router)
    app.include_router(chat_router)

    return app


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("helpdesk_agent.main:create_app", host="0.0.0.0", port=port, factory=True)  # nosec B104


if __name__ == "__main__":
    main()
