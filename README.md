# Helpdesk Agent

AI-powered helpdesk triage agent with email intake, LLM-based triage, FAQ resolution, and Jira ticket creation.

## Architecture

The agent is organized into domain modules:

```
src/helpdesk_agent/
├── main.py                 # FastAPI app factory
├── dependencies.py         # Dependency injection providers
├── config.py               # Configuration (MCP, LLM, Triage)
├── mcp.py                  # MCP proxy adapter (async, httpx)
├── models.py               # WorkflowState model
├── core/
│   ├── workflow.py         # LangGraph workflow orchestrator
│   └── cases.py            # CaseStore for parked cases
└── modules/
    ├── email/
    │   ├── router.py       # POST /webhooks/email
    │   ├── schema.py       # EmailWebhook, WebhookResponse
    │   └── service.py      # GmailFlow (MCP)
    ├── triage/
    │   ├── schema.py       # TriageResult
    │   └── llm.py          # LLM triage client (OpenAI)
    ├── faq/
    │   └── service.py      # FAQFlow (MCP)
    ├── ticketing/
    │   └── service.py      # JiraTicketFlow (MCP)
    ├── approval/
    │   └── service.py      # SlackApprovalFlow (MCP)
    └── chat/
        ├── router.py       # /chat, /conversations endpoints
        ├── schema.py       # ChatMessage, ChatResponse
        └── service.py      # ConversationStore
```

## Workflow

The agent follows this flow for incoming emails:

1. **Triage**: LLM analyzes the email and extracts service, issue type, priority, confidence
2. **Clarification**: If confidence is low, sends a clarification email and parks the case
3. **FAQ Check**: Searches FAQ repository for existing solutions
4. **Resolution**: If FAQ match found, sends resolution email (no ticket created)
5. **Ticketing**: If no FAQ match, creates Jira ticket
6. **Approval**: If production-impacting, requests Slack approval
7. **Notification**: Sends confirmation email to user

## Configuration

Configuration is read from environment variables. The app automatically loads a `.env` file from the project root (via `python-dotenv`) if present.

```bash
cp .env.example .env
# edit .env with your values
```

`.env` is git-ignored; `.env.example` documents all available settings.

## Observability (OpenTelemetry)

Distributed tracing is always on. By default spans are printed to stdout;
set `OTEL_EXPORTER_OTLP_ENDPOINT` to export to an OTLP collector (Jaeger,
Tempo, Azure Monitor, ...).

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_SERVICE_NAME=helpdesk-agent
```

Instrumented surfaces: inbound FastAPI requests (health probes excluded) and
outbound httpx calls (OpenAI SDK, MCP adapters). Use
`helpdesk_agent.observability.get_tracer()` for custom spans in workflow code.

### Required

- `HELPDESK_AGENT_LLM_API_KEY` - OpenAI API key (required)

### Optional

- `HELPDESK_AGENT_LLM_MODEL` - LLM model (default: `gpt-4o-mini`)
- `HELPDESK_AGENT_LLM_BASE_URL` - Custom LLM endpoint
- `HELPDESK_AGENT_TRIAGE_CONFIDENCE_THRESHOLD` - Confidence threshold for clarification (default: `0.6`)
- `HELPDESK_AGENT_FAQ_RELEVANCE_THRESHOLD` - FAQ relevance threshold (default: `0.7`)

### MCP Integration

- `HELPDESK_AGENT_USE_MCP` - Enable MCP adapter (default: `0`)
- `HELPDESK_AGENT_MCP_BASE_URL` - MCP endpoint URL
- `HELPDESK_AGENT_MCP_TIMEOUT_SECONDS` - Request timeout (default: `5.0`)
- `HELPDESK_AGENT_MCP_AUTH_TOKEN` - Bearer token for MCP endpoint

## Diagnosis agent hand-off (Agent 2)

When triage cannot resolve a case (no FAQ match, clarifications exhausted), the
`diagnose` workflow node hands the case to the diagnosis agent as an A2A 1.0
`SendMessage` JSON-RPC request, **fire-and-forget**: the helpdesk agent does
not poll for results — Agent 2 owns the investigation and posts its diagnosis
back to the Jira ticket itself.

Transport is selected by configuration:

- **Deployed (AgentCore)**: set `HELPDESK_AGENT_A2A_REMOTE_AGENT_ARNS` to a
  name → runtime ARN map. The envelope is submitted via SigV4-signed
  `bedrock-agentcore:InvokeAgentRuntime`. The bearer token rides on the
  `X-Amzn-Bedrock-AgentCore-Runtime-Custom-Authorization` header (AgentCore
  consumes `Authorization` for the signature), `A2A-Version: 1.0` is sent on
  every call, and botocore retries are disabled because the invocation is not
  idempotent (a retried call starts a second investigation). The execution
  role needs `bedrock-agentcore:InvokeAgentRuntime` scoped to the diagnosis
  runtime ARN.
- **Local/dev (HTTP)**: set `HELPDESK_AGENT_A2A_REMOTE_AGENTS` to a name →
  base URL map; the envelope is POSTed to `{base_url}/` with a bearer token
  on `Authorization`.

Current deployment:

| Setting | Value |
| --- | --- |
| Runtime ARN | `arn:aws:bedrock-agentcore:ap-southeast-1:734849394833:runtime/diagnosis_agent-GpDELMCxll` |
| Region | `ap-southeast-1` |
| Token | Secrets Manager `aios/diagnosis-agent/agent-auth-token` (account `734849394833`) — inject as `HELPDESK_AGENT_A2A_REMOTE_AGENT_TOKENS`; never commit |

Case payloads are built by `src/helpdesk_agent/modules/a2a/diagnosis.py`
(`build_case_input`) and carry the full `CaseInput` contract (`requester`,
`classification`, `affected_services`, `time_window`); the current OTEL trace
id rides along as `trace_id` for cross-agent tracing. Note: do NOT use agent
card discovery for Agent 2 — its deployed card advertises a localhost URL;
call the ARN directly.

## MCP Tools

The agent uses these MCP tools:

- `gmail.send_email` - Send emails
- `gmail.get_messages` - Retrieve messages
- `gmail.reply_to_email` - Reply to emails
- `jira.create_issue` - Create tickets
- `slack.post_approval` - Request approvals
- `faq.search` - Search FAQ repository

### Self-Hosted Slack MCP Deployment

The **Slack MCP Server** workflow (`.github/workflows/mcp-slack.yml`) builds
`docker/mcp-slack/Dockerfile` for Linux ARM64 and publishes
`<account-id>.dkr.ecr.ap-southeast-1.amazonaws.com/aios/mcp-slack:latest`.
It follows the Playwright image workflow and uses the existing GitHub secrets
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.

Before running it, create the `aios/mcp-slack` ECR repository in `ap-southeast-1`
if it does not exist, and ensure the workflow's AWS identity can push to it.
Once the workflow is available on GitHub, run it from **Actions > Slack MCP Server
> Run workflow**. Like the other MCP image workflows, this publishes an image;
it does not create or update an AgentCore runtime or configure a gateway target.
Point your existing runtime deployment process at the resulting ECR image.

The image pins `korotovsky/slack-mcp-server` to `v1.3.0`, listens on `0.0.0.0:8000`,
and serves Streamable HTTP MCP at `/mcp`. Supply these environment variables to
the runtime, not to the image build:

| Variable | Purpose |
| --- | --- |
| `SLACK_MCP_XOXB_TOKEN` | Slack bot OAuth token; inject securely from your secret store |
| `SLACK_MCP_API_KEY` | Separate client-to-server bearer key; configure securely if using server-level authentication |
| `SLACK_MCP_ENABLED_TOOLS` | Defaults to `channels_list,conversations_history,conversations_replies,users_search,conversations_add_message` |
| `SLACK_MCP_ADD_MESSAGE_TOOL` | Defaults to `C0C67H6P8E6`; restricts message posting to this channel |

The bot must have `users:read`, `channels:read`, `groups:read`, `im:read`, and
`mpim:read` for the server's startup caches. Add history scopes for the
conversation types you need to read and `chat:write` for posting. Install or
reinstall the Slack app after scope changes, and invite the bot to the target
channel. Bot tokens do not support message search. No Slack credentials are
baked into the image or stored in the workflow.

For direct HTTP access, send `Authorization: Bearer <SLACK_MCP_API_KEY>` if a key
is configured. For AgentCore-hosted access, configure the runtime/gateway's own
authentication and invocation path as required; the image workflow does not
provision that integration. This change does not add Slack routing to the
helpdesk agent's MCP adapter.

## API Endpoints

### Email Webhook

```
POST /webhooks/email
```

Request:
```json
{
  "sender": "alice@example.com",
  "subject": "Unable to access portal",
  "body": "I cannot log into the portal after the latest password reset.",
  "message_id": "<12345@example.com>",
  "in_reply_to": "<abcde@example.com>",
  "references": ["<abcde@example.com>"],
  "attachments": [{"filename": "screenshot.png", "url": "https://..."}]
}
```

### Chat

```
POST /chat
```

Request:
```json
{
  "message": "I need help resetting my password",
  "conversation_id": "optional-existing-id"
}
```

### Conversations

- `GET /conversations/{id}` - Get conversation
- `GET /conversations` - List all conversations
- `DELETE /conversations/{id}` - Delete conversation

## Running

### Development

```bash
# Install dependencies
uv sync

# Set required environment variable
export HELPDESK_AGENT_LLM_API_KEY=your-api-key

# Run the server
uv run python -m helpdesk_agent.main
```

### Docker

```bash
# Build
docker build -t helpdesk-agent:local .

# Run
docker run -p 8080:8080 -e HELPDESK_AGENT_LLM_API_KEY=your-api-key helpdesk-agent:local
```

## Testing

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=src

# Run lint
uv run ruff check .

# Run typecheck
uv run mypy
```

## Error Handling

The agent follows a fail-fast policy:

- Missing LLM API key → Startup error
- LLM call failure → HTTP 502
- MCP tool failure → HTTP 502

## License

MIT
