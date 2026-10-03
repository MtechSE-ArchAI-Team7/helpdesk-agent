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
│   └── workflow.py         # LangGraph workflow orchestrator
└── modules/
    ├── email/
    │   ├── router.py       # POST /webhooks/email
    │   ├── schema.py       # EmailWebhook, WebhookResponse
    │   └── service.py      # ZohoMailFlow (MCP)
    ├── triage/
    │   ├── schema.py       # TriageResult
    │   └── llm.py          # LLM triage client (OpenAI)
    ├── faq/
    │   └── service.py      # FAQFlow (MCP)
    ├── ticketing/
    │   └── service.py      # JiraTicketFlow (MCP)
    ├── playwright.py       # Browser feasibility and validation flow
    ├── a2a/                # Agent2Agent interface and diagnosis hand-off
    └── chat/
        ├── router.py       # /chat, /conversations endpoints
        ├── schema.py       # ChatMessage, ChatResponse
        └── service.py      # ConversationStore
```

## Workflow

For each incoming email, the workflow first checks the subject for a Jira key
from the configured project (default `HELP`). It loads a matching ticket, or
creates a new ticket if there is no key or the referenced ticket cannot be
found. New tickets are acknowledged before triage when the webhook includes a
message ID. The LLM then classifies the request and the workflow updates the
ticket fields.

After triage, the workflow assesses whether a read-only browser check can
validate the reported error. It only invokes the hosted Playwright MCP runtime
when the report includes an explicit HTTP or HTTPS URL whose exact host is in
`HELPDESK_AGENT_PLAYWRIGHT_ALLOWED_HOSTS`. The browser check navigates to the
page and collects a page snapshot, browser console errors, and network request
metadata in one MCP session. It does not log in, submit forms, or modify data.
If the report lacks details needed for a safe check, those questions join the
normal clarification email. Otherwise, ineligible or unavailable checks
continue to FAQ search without blocking the case.

Next, the workflow asks for clarification if triage confidence is below
`HELPDESK_AGENT_TRIAGE_CONFIDENCE_THRESHOLD` (default `0.6`) or triage reports
missing information. Clarification attempts are counted from Jira comments;
after two prior rounds, the case goes to diagnosis instead. Otherwise, the
workflow searches the FAQ service. A top result at or above
`HELPDESK_AGENT_FAQ_RELEVANCE_THRESHOLD` (default `0.7`) is emailed to the
requester and recorded on the ticket. If there is no sufficiently relevant FAQ
result, the ticket is marked as requiring diagnosis and may be handed off to a
remote diagnosis agent over A2A. That hand-off is fire-and-forget; this agent
does not poll for the diagnosis result.

Browser outcomes are `reproduced`, `not_reproduced`, or `inconclusive`. A concise
summary and per-tool statuses are added to the workflow result and Jira ticket.
Logs include case, stage, decision/outcome, host, tool status, and duration;
they do not include the browser page contents or full target URL.

The workflow's terminal states are `awaiting_clarification`,
`faq_answer_provided`, `diagnosis_required`, and `ticket_creation_failed`.
Ticket data in Jira is used to reconnect email follow-ups: replies should keep
the ticket key in the subject.

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
- `HELPDESK_AGENT_LLM_TIMEOUT_SECONDS` - LLM request timeout (default: `30.0`)
- `HELPDESK_AGENT_TRIAGE_CONFIDENCE_THRESHOLD` - Confidence threshold for clarification (default: `0.6`)
- `HELPDESK_AGENT_FAQ_RELEVANCE_THRESHOLD` - FAQ relevance threshold (default: `0.7`)

### MCP Integration

- `HELPDESK_AGENT_USE_MCP` - Enable real MCP calls (default: `0`; services are mocked when disabled)
- `HELPDESK_AGENT_MCP_BASE_URL` - Default MCP endpoint URL (default: `http://127.0.0.1:8765/mcp`)
- `HELPDESK_AGENT_MCP_ATLASSIAN_URL` - Optional dedicated Jira/Atlassian MCP endpoint
- `HELPDESK_AGENT_MCP_ZOHOMAIL_URL` - Optional dedicated Zoho Mail MCP endpoint
- `HELPDESK_AGENT_MCP_FAQ_URL` - Optional dedicated FAQ MCP endpoint
- `HELPDESK_AGENT_MCP_FAQ_AUTH_MODE` - FAQ inbound authentication: `iam` for SigV4 or `bearer` (default). Other MCP services retain their existing authentication.
- `HELPDESK_AGENT_MCP_FAQ_REGION` - Optional FAQ IAM signing-region override; otherwise inferred from the AWS gateway hostname.
- `HELPDESK_AGENT_MCP_FAQ_TOOL_PREFIX` - Optional managed Knowledge Base gateway target prefix, e.g. `mcp-knowledgebase___`; when set, calls `<prefix>Retrieve` with `retrievalQuery.text`. Unset retains legacy `faq.search`; empty uses unprefixed `Retrieve`.
- `HELPDESK_AGENT_MCP_PLAYWRIGHT_URL` - Hosted Playwright MCP runtime endpoint
- `HELPDESK_AGENT_MCP_PLAYWRIGHT_TOOL_PREFIX` - AgentCore Gateway target namespace prefix (default: `mcp-playwright___`; set empty for direct MCP server access)
- `HELPDESK_AGENT_MCP_PLAYWRIGHT_TIMEOUT_SECONDS` - Playwright MCP timeout (default: `30.0`)
- `HELPDESK_AGENT_PLAYWRIGHT_ALLOWED_HOSTS` - Comma-separated exact host allowlist for browser navigation
- `HELPDESK_AGENT_MCP_TIMEOUT_SECONDS` - Request timeout (default: `5.0`)
- `HELPDESK_AGENT_MCP_AUTH_TOKEN` - Bearer token for MCP endpoint
- `HELPDESK_AGENT_JIRA_PROJECT_KEY` - Jira project key (default: `HELP`)
- `HELPDESK_AGENT_MCP_TOOL_PREFIX` - Optional prefix for Atlassian MCP tool names
- `HELPDESK_AGENT_MCP_ZOHOMAIL_TOOL_PREFIX` - Zoho Mail tool prefix (default: `ZohoMail_`)
- `HELPDESK_AGENT_ZOHOMAIL_FROM_ADDRESS` - Helpdesk mailbox address used for outgoing mail

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

When the FAQ tool prefix is configured, Bedrock Knowledge Base retrieval results
are normalized from `retrievalResults[].content.text` to `body`, retaining scores,
locations, and metadata. Non-text/empty passages are excluded, and `count` is the
number of usable passages. The managed connector target controls `numberOfResults`;
the agent sends only `retrievalQuery.text`, not the legacy five-result limit or a
knowledge base ID. Configure retrieval settings in AWS. These results may be incomplete document excerpts or
internal operational material. The existing FAQ relevance gate is unchanged;
review knowledge-base content and calibrate its score threshold before enabling
automatic requester-facing answers.

The gateway handles IAM authentication to the managed Knowledge Base backend.
This is separate from inbound gateway authentication. Set
`HELPDESK_AGENT_MCP_FAQ_AUTH_MODE=iam` to sign FAQ MCP requests with SigV4 using
the AWS default credential chain, including refreshable runtime execution-role
credentials. IAM mode omits the shared bearer Authorization header for FAQ
calls. The caller role needs `bedrock-agentcore:InvokeGateway` scoped to the FAQ
gateway ARN; the gateway service role separately needs backend retrieval access.
For a gateway in `ap-southeast-2`, the signing region must also be
`ap-southeast-2`, regardless of the helpdesk runtime's region. No AWS keys should
be placed in the env template or committed to the repository.

The agent's service layer calls these tools (the MCP adapter translates Jira
tool names and routes calls to the configured server):

- `zohomail.ZohoMail_getMailAccounts` - Resolve the Zoho account ID
- `zohomail.ZohoMail_sendEmail` - Send email
- `zohomail.ZohoMail_sendReplyEmail` - Reply to an email
- `zohomail.ZohoMail_listEmails` - Retrieve messages
- `jira.create_issue`, `jira.get_issue`, `jira.update_issue`, `jira.add_comment` - Manage Jira tickets
- `faq.search` - Search the FAQ repository
- `playwright.browser_navigate`, `playwright.browser_snapshot`, `playwright.browser_console_messages`, `playwright.browser_network_requests` - Read-only browser validation; routed to `HELPDESK_AGENT_MCP_PLAYWRIGHT_URL` and prefixed for the gateway target

When MCP is disabled, these flows return mock results instead of performing
external actions. The FAQ mock has no matches, so a normal run proceeds to the
diagnosis-required path.

Playwright validation is skipped unless MCP is enabled, the runtime endpoint is
configured, and the host allowlist is non-empty. The initial URL must also be
present in the user's report, use HTTP or HTTPS, and match an allowlisted host
exactly. Only default ports are allowed. HTTP traffic is unencrypted; prefer
HTTPS and never send credentials through browser validation.
Apply network egress restrictions at the hosted runtime as well, since browser
pages can redirect or load resources from other hosts.

Zoho's reply tool expects its own numeric message ID. If an inbound webhook
provides an RFC822 message ID instead, the mail flow sends a new email (with the
ticketed subject) when recipient and subject are available; it cannot thread
that message as a Zoho reply.

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

Chat conversations are stored in memory and are lost when the process restarts.
Chat and A2A requests can also trigger an unread-mail lookup when the message
contains both a mail-related term and a read/list/fetch-related term. Inbound
email requests always go through normal triage.

### A2A

- `GET /.well-known/agent-card.json` - Public agent card
- `POST /a2a` - A2A JSON-RPC endpoint

The A2A interface uses the same chat/workflow handler. Clarification responses
are returned as input-required tasks; other results complete the task. For
outbound diagnosis hand-off configuration, see [Diagnosis agent hand-off](#diagnosis-agent-hand-off-agent-2).

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

Failure handling depends on the workflow stage:

- Missing LLM API key → Startup error.
- LLM triage failure → The workflow raises an error; the request fails.
- Jira ticket creation failure → The workflow stops before acknowledgment and triage, with `ticket_creation_failed` as its state.
- FAQ search failure or no sufficiently relevant match → The workflow routes to diagnosis-required.
- Email send/reply failures → Recorded in workflow history; they do not by themselves stop the workflow.
- Outbound diagnosis hand-off failure → Recorded in workflow metadata/history; the workflow continues and adds a Jira comment when a ticket exists.

## License

MIT
