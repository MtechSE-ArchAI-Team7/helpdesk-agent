# Helpdesk Agent

The Helpdesk Agent triages incoming support requests, routes them to the right workflow, answers FAQ-style questions, and creates Jira tickets when action is needed.

## Key workflow

- Receive and classify incoming helpdesk requests.
- Gather missing context and verify whether the issue is actionable.
- Route FAQs to knowledge answers or escalate to the right human/system process.
- Create or update Jira work items when follow-up is required.

## Run locally

1. Copy the sample environment file and set the required values:

```bash
cd helpdesk-agent
cp .env.example .env
# edit .env and set at minimum:
# HELPDESK_AGENT_LLM_API_KEY=...
```

2. Install dependencies:

```bash
uv sync --frozen
```

3. Start the app:

```bash
uv run uvicorn helpdesk_agent.api.app:create_app --factory --host 0.0.0.0 --port 8080
```

The service is available at http://localhost:8080.

## Run with Docker

```bash
docker build -t helpdesk-agent:local .
docker run --rm -p 8080:8080 --env-file .env helpdesk-agent:local
```

Notes:
- `HELPDESK_AGENT_LLM_API_KEY` is required for the app to start.
- Additional `HELPDESK_AGENT_*` variables in `.env` enable MCP tools, memory, Jira, and remote agent integrations.
