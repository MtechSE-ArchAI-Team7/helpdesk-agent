"""AWS Lambda handler that forwards API Gateway requests to the helpdesk agent on Bedrock AgentCore."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import uuid
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN", "")
AGENT_QUALIFIER = os.environ.get("AGENT_QUALIFIER", "DEFAULT")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "180"))

# Canonical field -> accepted alias (see EmailWebhook schema).
REQUIRED_FIELDS = {"sender": "from_email", "subject": "subject", "body": "body_plain"}

_client = boto3.client(
    "bedrock-agentcore",
    config=Config(read_timeout=REQUEST_TIMEOUT, connect_timeout=10, retries={"max_attempts": 2}),
)


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _lower_headers(event: dict[str, Any]) -> dict[str, str]:
    return {k.lower(): v for k, v in (event.get("headers") or {}).items()}


def _parse_body(event: dict[str, Any]) -> Any:
    body = event.get("body")
    if body is None:
        return None
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return json.loads(body)


def _session_id(payload: dict[str, Any]) -> str:
    """AgentCore requires a session id of at least 33 characters."""
    thread = payload.get("in_reply_to") or payload.get("message_id") or payload.get("messageId")
    seed = str(thread) if thread else uuid.uuid4().hex
    return f"helpdesk-email-{uuid.uuid5(uuid.NAMESPACE_URL, seed).hex}"


def _read_agent_response(result: dict[str, Any]) -> Any:
    stream = result["response"]
    if result.get("contentType", "").startswith("text/event-stream"):
        raw_text = "".join(
            line[len("data: ") :]
            for raw in stream.iter_lines(chunk_size=10)
            if (line := raw.decode("utf-8")).startswith("data: ")
        )
    else:
        raw_text = stream.read().decode("utf-8")

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        return {"raw": raw_text}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if not AGENT_RUNTIME_ARN:
        logger.error("AGENT_RUNTIME_ARN is not configured")
        return _response(500, {"error": "agent runtime not configured"})

    if WEBHOOK_SECRET:
        provided = _lower_headers(event).get("x-webhook-secret", "")
        if not hmac.compare_digest(provided, WEBHOOK_SECRET):
            return _response(401, {"error": "unauthorized"})

    try:
        payload = _parse_body(event)
    except (ValueError, UnicodeDecodeError):
        return _response(400, {"error": "request body must be valid JSON"})

    if not isinstance(payload, dict):
        return _response(400, {"error": "request body must be a JSON object"})

    # Zoho payloads are normalized server-side, so only validate the canonical shape.
    if "fromAddress" not in payload:
        missing = [
            field
            for field, alias in REQUIRED_FIELDS.items()
            if not payload.get(field) and not payload.get(alias)
        ]
        if missing:
            return _response(400, {"error": f"missing required field(s): {', '.join(missing)}"})

    try:
        result = _client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            qualifier=AGENT_QUALIFIER,
            runtimeSessionId=_session_id(payload),
            contentType="application/json",
            accept="application/json",
            payload=json.dumps({"path": "/webhooks/email", "payload": payload}).encode("utf-8"),
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "InvokeFailed")
        logger.error("invoke_agent_runtime failed: %s", exc)
        status = 504 if code in {"ThrottlingException", "ServiceQuotaExceededException"} else 502
        return _response(status, {"error": "agent invocation failed", "detail": code})

    logger.info("forwarded email webhook to AgentCore runtime")
    return _response(200, _read_agent_response(result))
