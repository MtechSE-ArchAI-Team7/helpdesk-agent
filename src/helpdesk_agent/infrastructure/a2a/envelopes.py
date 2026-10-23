from __future__ import annotations

import json
from typing import Any
from uuid import uuid4


def build_send_message_envelope(
    case_payload: dict[str, Any],
    *,
    task_id: str | None = None,
    context_id: str | None = None,
) -> dict[str, Any]:
    """Wrap a CaseInput dict in an A2A 1.0 JSON-RPC ``SendMessage`` envelope.

    The case travels as a DataPart (``{"data": ...}``). ``taskId`` and
    ``contextId`` are set only when resuming an existing task — on a new
    submission they are omitted and the server assigns them: an inbound
    ``taskId`` means "continue the task with this id", and an unknown one is
    rejected with ``Task <id> not found``.
    """
    message: dict[str, Any] = {
        "role": "ROLE_USER",
        "messageId": f"m-{uuid4().hex}",
        "parts": [{"data": case_payload}],
    }
    if task_id is not None:
        message["taskId"] = task_id
    if context_id is not None:
        message["contextId"] = context_id
    return {
        "jsonrpc": "2.0",
        "id": uuid4().hex,
        "method": "SendMessage",
        "params": {"message": message},
    }


def envelope_digest(envelope: dict[str, Any]) -> str:
    """Stable digest of an envelope, for idempotency keys / session ids."""
    import hashlib

    return hashlib.sha256(json.dumps(envelope, sort_keys=True).encode("utf-8")).hexdigest()[:16]
