# Helpdesk Agent

AI-powered helpdesk triage agent with email intake, LLM-based triage, FAQ resolution, and Jira ticket creation.

## Agent CICD

The separate [Agent CICD workflow](.github/workflows/agent-cicd.yaml) runs only
through **Actions > Agent CICD > Run workflow**. Dispatch a trusted ref. It leaves
the existing CI and Image workflows unchanged.

Stages run in order: pytest unit tests, pytest integration tests, DeepEval
functional evaluations, SonarQube Cloud analysis, Snyk SCA and SAST, ARM64
container build, Trivy image scan, ECR push, and AgentCore runtime update.
Each stage has its own job/box in the Actions graph, followed by a Summary job.
Jobs run on separate runners and pass coverage and the container through artifacts.

**Tests and scanners are advisory.** They continue on error without enforcing
quality gates or vulnerability thresholds. A successful deployment does not mean
tests or scans passed. Inspect the raw step outcomes in the Summary job and the
per-stage `agent-cicd-reports-*` artifacts, which contain available JUnit,
coverage, DeepEval, Snyk, and Trivy reports. Snyk exit 1 means findings; exits 2/3 mean tool errors or
no supported project. A clean Snyk Code scan may not produce a JSON file.
Dependency-installation and coverage errors are also reported without blocking
the build. A failed advisory job does not skip the remaining stages, but cancellation
prevents downstream work. Build/artifact-export failure prevents scanning and
publishing; push failure prevents runtime updates. Credential setup and
runtime-update errors also fail deployment. Summary runs even after failures.

Unit and integration jobs upload independent coverage data, which SonarQube
combines into one coverage report. The workflow builds once and exports an image
archive. Trivy and ECR Push download that same archive and independently verify
its recorded SHA-256 checksum; Push loads it without rebuilding. Image tags include
commit SHA, run ID, and run attempt, rather than mutable `latest` or `main` tags.
Runs targeting the same runtime are serialized. Report artifacts are retained for
14 days and image artifacts for 3 days; cross-runner image transfers add runtime
and artifact storage usage.

Configure these GitHub repository **secrets** before running:

| Secret | Purpose |
| --- | --- |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Existing AWS deployment credentials |
| `SONAR_TOKEN` | SonarQube Cloud analysis token |
| `SNYK_TOKEN` | Snyk token with SCA and Snyk Code access |
| `HELPDESK_AGENT_LLM_API_KEY` | Live triage model credential, not the pytest dummy key |

Configure these repository **variables**:

| Variable | Purpose/default |
| --- | --- |
| `AGENTCORE_RUNTIME_ID` | Required existing helpdesk container runtime ID, not an ARN |
| `AWS_REGION` | `ap-southeast-1` |
| `ECR_REPOSITORY` | Existing repository; defaults to `aios/helpdesk-agent` |
| `SONAR_PROJECT_KEY`, `SONAR_ORGANIZATION` | Required for Cloud analysis |
| `SONAR_HOST_URL` | `https://sonarcloud.io`; override for your Cloud region |
| `SNYK_ORG` | Optional organization ID/slug; otherwise token's preferred organization |
| `HELPDESK_AGENT_LLM_MODEL` | `gpt-4o-mini` |
| `HELPDESK_AGENT_LLM_BASE_URL` | Optional OpenAI-compatible gateway URL |

Disable SonarQube Cloud automatic analysis for this project when enabling CI
analysis. Enable Snyk Code in the selected Snyk organization. Missing scanner or
LLM configuration produces advisory errors, not a clean assessment.

SCA installs production dependencies from the frozen universal lock export, then
generates a marker-free scan manifest from packages installed for the runner's
Python version and platform. This avoids Snyk treating conditional dependencies
for other environments as missing. Both manifests are included in the SCA reports.
Compatible setuptools is installed only for scanner tooling; it does not change
the application lockfile or image and is excluded from the scan manifest.

The ECR repository and AgentCore runtime must already exist in the deployment
account/region. The AWS deployment principal needs ECR authentication, layer
upload, image-push and `DescribeImages` permissions, AgentCore `GetAgentRuntime` and
`UpdateAgentRuntime`, and narrowly scoped `iam:PassRole` for the runtime role
where required. The runtime execution role must be able to pull the ECR image.
Its environment must already contain the production LLM and service configuration.
The diagnosis-agent ARN elsewhere in this README is a remote dependency, **not**
the deployment target.

[The update helper](scripts/update_agentcore_runtime.py) requires an existing
`READY` HTTP container runtime, preserves its execution role, network, environment,
authorization and other supported configuration, and changes only the image URI.
It waits up to 15 minutes for the returned version to become `READY`, and verifies
that version's image. It does not create infrastructure, change named endpoint
version pins, update Lambda functions, or automatically roll back failures.
Consumers pinned to a named endpoint/version require separate promotion.

Tests are explicitly partitioned in [the pytest configuration](tests/conftest.py).
Integration tests exercise API/workflow/transport collaboration with remote
boundaries mocked; they do not contact production services. New test modules
must be categorized there or explicitly marked `unit`/`integration`.
Live evaluations live outside `tests/` to avoid its dummy-credential fixtures.

```bash
uv sync --frozen --group eval
uv run pytest -m unit --strict-markers
uv run pytest -m integration --strict-markers
uv run --group eval pytest evaluations/test_triage.py -m unit
uv run --group eval deepeval test run evaluations/test_triage.py -m functional
```

[The initial labelled dataset](evaluations/triage_cases.json) has 12 synthetic,
explicitly described cases. DeepEval scores exact service/issue-type/priority
matches without a separate judge model or Confident AI account. This is a smoke
baseline, not production-quality certification; extend it with reviewed,
de-identified examples before relying on its quality scores. The eval dependency
group is excluded from the production image. Live evaluations incur model costs.

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
after the configured maximum (default two rounds), the case goes to diagnosis
instead. Otherwise, the workflow searches the FAQ service. A top result at or above
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

Distributed tracing is always on. By default spans are not exported or printed;
trace IDs remain available for correlation. Set `OTEL_CONSOLE_EXPORTER=1` for
local debugging, or `OTEL_EXPORTER_OTLP_ENDPOINT` to export to an OTLP collector
(Jaeger, Tempo, Azure Monitor, ...).

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
OTEL_SERVICE_NAME=helpdesk-agent
```

Instrumented surfaces: inbound FastAPI requests (health probes excluded) and
outbound httpx calls (OpenAI SDK, MCP adapters). Use
`helpdesk_agent.observability.get_tracer()` for custom spans in workflow code.

### Langfuse Cloud EU

Langfuse is optional and disabled by default. When enabled, the SDK shares the
application's OpenTelemetry provider and exports request parents, the complete
workflow, its named steps, and the three local LLM generations. Health probes,
ASGI send/receive events, and outbound HTTP noise are excluded. Diagnosis handoffs
keep the same trace ID; remote diagnosis-agent internals are not instrumented here.

**Full-content capture:** enabling Langfuse exports helpdesk text, workflow
messages, LLM prompts (including supplied history/browser evidence), and raw
responses. These may contain personal or user-supplied sensitive data; no automatic
redaction is applied. Configuration objects and authorization headers are not
captured. Restrict project access and set retention appropriately. Content is not
captured by this integration when Langfuse is disabled or fails to initialize.
When a separate OTLP backend is enabled alongside Langfuse, it also receives these
application-span attributes, including captured content.

For local development, configure keys outside source control:

```dotenv
HELPDESK_AGENT_LANGFUSE_ENABLED=1
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

For the deployed AgentCore helpdesk runtime:

1. Create an EU Langfuse project and obtain its public/secret key pair. Store a
  JSON object with `public_key` and `secret_key` in AWS Secrets Manager. Do not
  bake keys into the image, commit them, or print them in deployment logs.
2. Grant the runtime execution role `secretsmanager:GetSecretValue` scoped to
  that secret, plus `kms:Decrypt` if it uses a customer-managed KMS key.
3. Configure runtime environment variables before container startup:

  ```dotenv
  HELPDESK_AGENT_LANGFUSE_ENABLED=1
  LANGFUSE_BASE_URL=https://cloud.langfuse.com
  HELPDESK_AGENT_LANGFUSE_SECRET_ARN=<secret ARN or name>
  HELPDESK_AGENT_LANGFUSE_SECRET_REGION=<AWS secret region>
  DISABLE_ADOT_OBSERVABILITY=true
  ```

  Explicit `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` values take precedence over
  stored values; omit them to use the secret. Disabling ADOT prevents competing
  tracer initialization and removes built-in ADOT tracing, not application
  CloudWatch logs. Preserve other desired tracing through the app's OTLP exporter.
4. Ensure outbound HTTPS to `cloud.langfuse.com` and access to Secrets Manager.
  Private VPC deployments need suitable egress/NAT and AWS endpoint access.
5. Rebuild and redeploy using the existing pipeline. Its runtime update script
  preserves environment variables. No pipeline credential injection is needed.

Do not configure `OTEL_EXPORTER_OTLP_ENDPOINT` or its traces-specific equivalent
to the same Langfuse destination while enabling the SDK. Use OTLP only for a
different backend; duplicate Langfuse configuration disables SDK initialization
with a sanitized warning. Invalid credentials/configuration or secret resolution
failure similarly leaves helpdesk processing running without Langfuse. Export
failures are handled in the background, and shutdown attempts a bounded flush.

After deployment, send a synthetic support request through `/invocations`. Confirm
a connected request/workflow/step/generation hierarchy in the EU project's traces,
including model, prompts, responses, durations, errors, and provider-returned token
usage. Check that `/ping` is absent and the diagnosis payload's trace ID matches.
Usage remains unknown when the gateway supplies none. Cost estimates require a
recognized model/pricing entry; configure custom pricing for gateway model IDs
when necessary. No live credentials are checked during application startup.

To roll back, set `HELPDESK_AGENT_LANGFUSE_ENABLED=0` and restart/redeploy the
runtime. Restore ADOT separately if you need its built-in tracing again.

### Required

- `HELPDESK_AGENT_LLM_API_KEY` - OpenAI API key (required)

### Optional

- `HELPDESK_AGENT_LLM_MODEL` - LLM model (default: `gpt-4o-mini`)
- `HELPDESK_AGENT_LLM_BASE_URL` - Custom LLM endpoint
- `HELPDESK_AGENT_LLM_TIMEOUT_SECONDS` - LLM request timeout (default: `30.0`)
- `HELPDESK_AGENT_TRIAGE_CONFIDENCE_THRESHOLD` - Confidence threshold for clarification (default: `0.6`)
- `HELPDESK_AGENT_FAQ_RELEVANCE_THRESHOLD` - FAQ relevance threshold (default: `0.7`)
- `HELPDESK_AGENT_MAX_CLARIFICATION_ROUNDS` - Maximum clarification rounds before escalation
  (default: `2`; `0` disables clarification)

### AgentCore ticket memory

Memory is opt-in (`HELPDESK_AGENT_MEMORY_ENABLED=0` by default). It uses the
AWS SDK and the default AWS credential chain, not the FAQ MCP gateway or its
authentication setting. The LLM provider does not need to change.

Provision a resource explicitly; startup does not create AWS resources:

```bash
uv run helpdesk-memory-create --name HelpdeskMemory --region ap-southeast-2 --retention-days 90
# Optional: --kms-key-arn <customer-managed-key-arn>
```

The command creates summary and semantic strategies and waits up to five
minutes for activation. It prints the memory ID immediately, so an activation
timeout can be investigated in the AWS console without creating another resource.
The provisioning identity needs `bedrock-agentcore:CreateMemory` and
`bedrock-agentcore:GetMemory`, plus any applicable KMS permissions.

Then configure:

```bash
HELPDESK_AGENT_MEMORY_ENABLED=1
HELPDESK_AGENT_MEMORY_ID=<memory-id-from-provisioning>
HELPDESK_AGENT_MEMORY_REGION=ap-southeast-2
HELPDESK_AGENT_MEMORY_ORGANIZATION_ID=your-organization
```

The runtime role needs `bedrock-agentcore:GetMemory` for fail-fast startup
validation of activation and extraction namespaces, and `bedrock-agentcore:CreateEvent`,
`bedrock-agentcore:ListEvents`, and `bedrock-agentcore:RetrieveMemoryRecords`
on that memory ARN, plus any applicable customer-managed KMS permissions.
Use least-privilege policies; no static AWS keys are required.

The organization ID must be a lowercase identifier without slashes. A deployment
serves **one trusted organization**, whose members are permitted to share case
descriptions and troubleshooting. Namespace scoping is not user authentication:
protect the email, chat, and A2A entry points with your existing trusted ingress
and ticket-access controls. Do not expose conversation IDs as authorization
tokens or deploy this configuration as a public multi-tenant helpdesk.

**Short-term memory:** actor = configured organization; session = Jira ticket ID.
For a new ticket, the original report is saved before its acknowledgment.
For replies, the workflow retrieves recent ticket turns before storing the
incoming message. Sent email responses are saved only after successful delivery;
chat/A2A responses are saved when prepared for return. Raw turns use
`extractionMode=SKIP`, so they are not automatically turned into shared insights.
Chat/A2A conversation-to-ticket bindings are also persisted as non-extracted
events, allowing follow-ups with the same conversation/context ID to reconnect
after a process restart. Jira remains authoritative for status and the
two-clarification limit; no LangGraph checkpoint is resumed.

**Long-term memory:** completed helpdesk runs submit separate case snapshots:
the report, existing Jira description and recent comment bodies, triage summary,
clarification questions, browser diagnostic summaries, and suggested FAQ content.
Summary and semantic strategies extract records asynchronously under:

```text
/org/{actorId}/tickets/{sessionId}/summary/
/org/{actorId}/tickets/{sessionId}/facts/
```

Use these exact namespace templates if provisioning through the console.
Retrieval searches only `/org/<organization-id>/tickets/`, excludes the current
ticket, and combines records from each source ticket. Results feed triage and
the diagnosis hand-off, and appear as `similar_cases` in chat response metadata
and email workflow metadata. Historical reports and troubleshooting are labeled
as unverified; an FAQ suggestion or escalation is **not** a confirmed resolution.
Memory never replaces the authoritative FAQ gate or directly authorizes tools.
Extraction may take a minute or more, so recent snapshots may not be searchable
immediately. Existing tickets are learned when processed; there is no historical
backfill. To learn later diagnosis/human troubleshooting and resolution comments,
register `POST /webhooks/jira` as a Jira webhook for issue updates and comment
creation/updates. The endpoint accepts Jira's `{"issue": {"key": "HELP-123"}}`
envelope (other fields are ignored), validates the project, re-fetches Jira data,
and submits a fresh snapshot. It requires memory and real Jira MCP to be enabled.
Protect this endpoint at trusted ingress as well; it does not implement webhook
authentication itself. Without the Jira webhook, later comments are learned only
on the next ticket reply. Snapshots remain unverified observations even when Jira
reports a resolution; the LLM must assess applicability rather than assume success.
Duplicate update snapshots use content-based idempotency tokens. The endpoint
returns `submitted` (not extraction-completed); failures return 4xx/5xx.
For AgentCore Runtime deployments, `/invocations` accepts the same Jira envelope
with `"path": "/webhooks/jira"`. The included Lambda/SAM bridge exposes a
`JiraWebhookUrl` alongside the email URL. Configure its `WebhookSecret` and
provide `x-webhook-secret` on trusted webhook requests (or use an authenticated
upstream relay); do not leave internet-facing webhook ingress unauthenticated.

Shared snapshots omit sender fields and comment author objects and redact email
addresses, common credential assignments, bearer tokens, AWS access-key IDs,
and private-key blocks. This is defense in depth, **not complete PII detection**.
Approve your data-sharing policy before enabling memory; use upstream redaction
for regulated or restricted ticket content. Raw ticket history can contain
sensitive content and must be protected accordingly.

Tuning (all optional):

| Setting | Default | Behavior |
|---|---|---|
| `HELPDESK_AGENT_MEMORY_HISTORY_EVENTS` | `30` | Maximum recent events fetched (1–100); snapshots also occupy events |
| `HELPDESK_AGENT_MEMORY_CONTEXT_CHARS` | `12000` | Character budget for ticket history and, separately, related-case context |
| `HELPDESK_AGENT_MEMORY_SIMILAR_CASES` | `5` | Maximum distinct related tickets (1–10) |
| `HELPDESK_AGENT_MEMORY_TIMEOUT_SECONDS` | `5.0` | AWS connection/read timeout, with at most two SDK attempts |

AWS I/O runs off the async event loop. Memory failures are logged without
request content and recorded in workflow history and `memory_errors` metadata;
ticket processing continues using Jira when history/search/event writes fail.
A chat binding read/write failure or unavailable linked Jira ticket fails the
request rather than knowingly starting a new case. Event writes use stable
idempotency tokens; this does not make the entire Jira/email workflow exactly-once.

Raw event retention is set at provisioning (3–365 days). Expiring/deleting events
does **not** delete extracted long-term records. The local conversation-delete
endpoint does not delete shared ticket memory: use AgentCore event/record deletion
APIs for an authorized purge, including derived records. Conversation-list/get
endpoints and A2A task records remain process-local; persisted ticket history and
bindings are independent of those UI/task stores.

See the [AgentCore Memory guide](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html)
and its [event extraction controls](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_CreateEvent.html).

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

The conversation API's transcripts are stored in process memory and are lost on
restart. Optional AgentCore ticket history and chat-to-ticket bindings survive
within their configured retention period (see AgentCore ticket memory above).
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
