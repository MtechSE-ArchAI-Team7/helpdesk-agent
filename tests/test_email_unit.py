from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from helpdesk_agent.api.schemas.email import EmailWebhook, _strip_html
from helpdesk_agent.config.settings import MCPConfig
from helpdesk_agent.infrastructure.mcp.email import ZohoMailFlow
from helpdesk_agent.infrastructure.mcp.transport import MCPResponse

pytestmark = pytest.mark.unit


def response(content=None, status="completed"):
    return MCPResponse(
        action_id="action-1", status=status, content=content or {}, error="tool failed" if status == "failed" else None
    )


@pytest.fixture
def mail():
    adapter = MagicMock()
    adapter.config = MCPConfig(zohomail_from_address="support@example.com")
    adapter.call_tool = AsyncMock()
    return ZohoMailFlow(adapter)


@pytest.mark.parametrize(
    "content",
    [
        {"accountId": 12},
        {"data": [{"accountId": 12}]},
        {"data": {"data": [{"account_id": 12}]}},
        {"accounts": [{"accountId": 12}]},
    ],
)
@pytest.mark.asyncio
async def test_account_discovery_is_cached(mail, content):
    mail.mcp_adapter.call_tool.return_value = response(content)
    assert await mail._get_account_id() == "12"
    assert await mail._get_account_id() == "12"
    mail.mcp_adapter.call_tool.assert_awaited_once_with(
        "zohomail.ZohoMail_getMailAccounts", {}, wait_for_completion=True
    )


@pytest.mark.parametrize("content", [{}, {"data": "invalid"}, {"data": []}])
@pytest.mark.asyncio
async def test_account_discovery_rejects_missing_account(mail, content, caplog):
    mail.mcp_adapter.call_tool.return_value = response(content)
    assert await mail._get_account_id() is None
    assert "returned no accountId" in caplog.text


@pytest.mark.parametrize("operation", ["send", "reply", "list"])
@pytest.mark.asyncio
async def test_operations_stop_when_account_discovery_fails(mail, operation, caplog):
    mail.mcp_adapter.call_tool.return_value = response(status="failed")
    if operation == "send":
        result = await mail.send_email("user@example.com", "Help", "Body")
    elif operation == "reply":
        result = await mail.reply_email("123", "Body")
    else:
        result = await mail.get_messages()
    assert result["status"] == "failed"
    assert "accountId" in result["error"]
    assert "aborted" in caplog.text
    assert mail.mcp_adapter.call_tool.await_count == 1


@pytest.mark.parametrize("payload", [{"messageId": 123}, {"message_id": "123"}, [], {}])
@pytest.mark.asyncio
async def test_send_email_maps_response_and_arguments(mail, payload):
    mail._account_id = "account-1"
    mail.mcp_adapter.call_tool.return_value = response({"data": payload})
    result = await mail.send_email("user@example.com", "Help", "Body")
    assert result["status"] == "sent"
    assert result["message_id"] == ("123" if payload else "msg-action-1")
    args = mail.mcp_adapter.call_tool.await_args
    assert args.args[0] == "zohomail.ZohoMail_sendEmail"
    assert args.args[1]["body"] == {
        "fromAddress": "support@example.com",
        "toAddress": "user@example.com",
        "subject": "Help",
        "content": "Body",
        "mailFormat": "plaintext",
    }
    assert args.kwargs["idempotency_key"] == "send-email-Help"


@pytest.mark.parametrize("html", [None, "<p>Body</p>"])
@pytest.mark.asyncio
async def test_numeric_reply_preserves_thread_and_optional_fields(mail, html):
    mail._account_id = "account-1"
    mail.mcp_adapter.call_tool.return_value = response({"data": {"data": {"messageId": "456"}}})
    result = await mail.reply_email(" 123 ", "Body", subject="Re: Help", to="user@example.com", html_body=html)
    assert result["reply_message_id"] == "456"
    call = mail.mcp_adapter.call_tool.await_args
    assert call.args[1]["path_variables"] == {"accountId": "account-1", "messageId": "123"}
    assert call.args[1]["body"]["content"] == (html or "Body")
    assert call.args[1]["body"]["subject"] == "Re: Help"
    assert call.args[1]["body"]["toAddress"] == "user@example.com"
    assert ("mailFormat" in call.args[1]["body"]) is bool(html)


@pytest.mark.asyncio
async def test_minimal_reply_and_non_object_response(mail):
    mail._account_id = "account-1"
    mail.mcp_adapter.call_tool.return_value = response({"data": []})
    result = await mail.reply_email("123", "Body")
    assert result["status"] == "sent"
    assert result["reply_message_id"] is None
    assert mail.mcp_adapter.call_tool.await_args.args[1]["body"] == {
        "action": "reply",
        "fromAddress": "support@example.com",
        "content": "Body",
    }


@pytest.mark.asyncio
async def test_rfc822_reply_requires_recipient_and_subject(mail):
    mail._account_id = "account-1"
    failed = await mail.reply_email("<mail@example.com>", "Body")
    assert failed["status"] == "failed"
    mail.mcp_adapter.call_tool.assert_not_awaited()
    mail.mcp_adapter.call_tool.return_value = response({"messageId": "456"})
    sent = await mail.reply_email("<mail@example.com>", "Body", to="user@example.com", subject="[HELP-1] Help")
    assert sent["status"] == "sent"
    assert "sent as new email" in sent["note"]
    assert mail.mcp_adapter.call_tool.await_args.args[0].endswith("sendEmail")


@pytest.mark.parametrize("operation", ["send", "reply", "list"])
@pytest.mark.asyncio
async def test_tool_failure_is_reported(mail, operation):
    mail._account_id = "account-1"
    mail.mcp_adapter.call_tool.return_value = response(status="failed")
    if operation == "send":
        result = await mail.send_email("user@example.com", "Help", "Body")
    elif operation == "reply":
        result = await mail.reply_email("123", "Body")
    else:
        result = await mail.get_messages()
    assert result == {"status": "failed", "action_id": "action-1", "error": "tool failed"}


@pytest.mark.parametrize(
    "payload", [[{"subject": "Help"}], {"messages": [{"subject": "Help"}]}, {"data": [{"subject": "Help"}]}]
)
@pytest.mark.parametrize("query,status", [("is:unread", "unread"), ("all", "all")])
@pytest.mark.asyncio
async def test_message_listing_unwraps_payload_and_filters(mail, payload, query, status):
    mail._account_id = "account-1"
    mail.mcp_adapter.call_tool.return_value = response({"data": payload})
    result = await mail.get_messages(query, max_results=3)
    assert result["messages"] == [{"subject": "Help"}]
    assert result["count"] == 1
    assert mail.mcp_adapter.call_tool.await_args.args[1]["query_params"] == {"limit": 3, "status": status}


@pytest.mark.asyncio
async def test_disabled_mail_is_explicitly_mocked():
    flow = ZohoMailFlow(config=MCPConfig())
    assert (await flow.send_email("u@example.com", "Help", "Body"))["status"] == "mocked"
    assert (await flow.reply_email("123", "Body"))["reply_message_id"] == "reply-123"
    assert (await flow.get_messages()) == {"status": "mocked", "messages": [], "count": 0}


def test_zoho_webhook_normalizes_full_payload_without_mutating_input():
    payload = {
        "fromAddress": "user@example.com",
        "sender": "User",
        "subject": "Help",
        "html": "<style>hidden</style><script>ignored</script><p>Login &amp; access</p><div>Failed</div>",
        "summary": "Truncated",
        "messageId": 123,
        "sentDateInGMT": 0,
        "receivedTime": 1000,
        "toAddress": "support@example.com",
        "ccAddress": "cc@example.com",
        "folderId": 2,
        "zuid": 3,
        "size": 4,
        "IntegIdList": "5",
    }
    webhook = EmailWebhook.model_validate(payload)
    assert webhook.sender == "user@example.com"
    assert webhook.from_name == "User"
    assert webhook.body == "Login & access\n\nFailed"
    assert webhook.message_id == "123"
    assert webhook.date == "1970-01-01T00:00:01+00:00"
    assert (
        webhook.to_address,
        webhook.cc_address,
        webhook.folder_id,
        webhook.zuid,
        webhook.size,
        webhook.integ_id_list,
    ) == ("support@example.com", "cc@example.com", 2, 3, 4, "5")
    assert payload["sender"] == "User"


@pytest.mark.parametrize("extra,expected", [({"summary": " Preview "}, "Preview"), ({"body": "Original"}, "Original")])
def test_zoho_webhook_preview_fallback(extra, expected):
    webhook = EmailWebhook.model_validate({"fromAddress": "u@example.com", "subject": "Help", **extra})
    assert webhook.body == expected


def test_canonical_and_automation_webhook_aliases():
    webhook = EmailWebhook.model_validate({"from_email": "u@example.com", "subject": "Help", "body_plain": "Body"})
    assert webhook.sender == "u@example.com"
    assert webhook.body == "Body"
    with pytest.raises(ValidationError):
        EmailWebhook.model_validate([])
    assert _strip_html("<div>one<br>two</div><p>three</p>") == "one two\n\nthree"
