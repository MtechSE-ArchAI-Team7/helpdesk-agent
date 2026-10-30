from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


def test_email_webhook_creates_case_and_ticket() -> None:
    # Mock the LLM client
    mock_patcher = patch("helpdesk_agent.api.dependencies.get_llm_client")
    mock_get_llm = mock_patcher.start()

    from helpdesk_agent.domain.models.triage import TriageResult

    mock_llm = MagicMock()
    mock_llm.triage_email = AsyncMock(
        return_value=TriageResult(
            service="identity",
            issue_type="access",
            priority="high",
            confidence=0.9,
            missing_info=[],
            clarifying_questions=[],
            summary="Unable to access portal",
            source="email",
            sender="alice@example.com",
            raw_text="Subject: Unable to access portal\n\nBody: I cannot log into the portal after the latest "
            "password reset.",
        )
    )
    mock_get_llm.return_value = mock_llm

    from helpdesk_agent.api.app import create_app

    client = TestClient(create_app())
    response = client.post(
        "/webhooks/email",
        json={
            "sender": "alice@example.com",
            "subject": "Unable to access portal",
            "body": "I cannot log into the portal after the latest password reset.",
            "message_id": "<12345@example.com>",
        },
    )

    print(f"Status: {response.status_code}")
    print(f"Response: {response.text}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"]
    assert payload["events"][0]["action"] == "run.started"
    assert payload["events"][-1]["action"] == "run.finished"
    assert "alice@example.com" not in str(payload["events"])
    assert "12345@example.com" not in str(payload["events"])
    assert len({event["case_id"] for event in payload["events"]}) == 1
    assert payload["ticket"]["status"] == "mocked"
    assert payload["ticket"]["ticket_id"] == "JIRA-<12345@example.com>"
    # No FAQ match in mocked mode -> ticket marked diagnosis required
    assert payload["current_step"] == "diagnosis_required"

    mock_patcher.stop()


def test_email_webhook_accepts_tool_field_names() -> None:
    """Accept Gmail automation tool field names (from_email, body_plain, message_url)."""
    mock_patcher = patch("helpdesk_agent.api.dependencies.get_llm_client")
    mock_get_llm = mock_patcher.start()

    from helpdesk_agent.domain.models.triage import TriageResult

    mock_llm = MagicMock()
    mock_llm.triage_email = AsyncMock(
        return_value=TriageResult(
            service="identity",
            issue_type="access",
            priority="high",
            confidence=0.9,
            missing_info=[],
            clarifying_questions=[],
            summary="Security alert",
            source="email",
            sender="no-reply@accounts.google.com",
            raw_text="Subject: Security alert\n\nBody: Sign-in attempt",
        )
    )
    mock_get_llm.return_value = mock_llm

    from helpdesk_agent.api.app import create_app

    client = TestClient(create_app())
    response = client.post(
        "/webhooks/email",
        json={
            "subject": "Security alert",
            "from_email": "no-reply@accounts.google.com",
            "from_name": "Google",
            "date": "Sat, 15 Aug 2026 06:20:56 GMT",
            "body_plain": "Sign-in attempt was blocked",
            "message_url": "https://mail.google.com/mail/u/0/#inbox/abc123",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ticket"]["status"] == "mocked"
    # New ticket created; case_id now carries the ticket ID
    assert payload["case_id"] == "JIRA-https://mail.google.com/mail/u/0/#inbox/abc123"

    mock_patcher.stop()


def test_email_webhook_accepts_zoho_payload() -> None:
    """Accept a Zoho Mail webhook payload (fromAddress, summary, messageId, ...)."""
    mock_patcher = patch("helpdesk_agent.api.dependencies.get_llm_client")
    mock_get_llm = mock_patcher.start()

    from helpdesk_agent.domain.models.triage import TriageResult

    mock_llm = MagicMock()
    mock_llm.triage_email = AsyncMock(
        return_value=TriageResult(
            service="marketing",
            issue_type="request",
            priority="medium",
            confidence=0.9,
            missing_info=[],
            clarifying_questions=[],
            summary="Product pitch slide deck shared",
            source="email",
            sender="paula@zylker.com",
            raw_text="Marketing - Product pitch",
        )
    )
    mock_get_llm.return_value = mock_llm

    from helpdesk_agent.api.app import create_app

    client = TestClient(create_app())
    response = client.post(
        "/webhooks/email",
        json={
            "summary": "Hi Rebecca, I have shared the slide deck for our product pitch meeting "
            "on Friday. Please take a look and do let me know if you have any suggestions. "
            "Regards, Paula",
            "sentDateInGMT": 1560866021000,
            "subject": "Marketing - Product pitch",
            "messageId": 1560840837125110000,
            "toAddress": '"Rebecca A"<rebecca@zylker.com>',
            "folderId": 3881227000000013000,
            "zuid": 647772765,
            "ccAddress": "",
            "size": 55503,
            "sender": "Paula",
            "receivedTime": 1560840837126,
            "fromAddress": "paula@zylker.com",
            "html": (
                '<meta /><div><div style="font-family:&quot;Trebuchet ms&quot;, Arial, '
                'Helvetica, sans-serif;font-size:12pt;"><div>Hi Rebecca,<br /></div>'
                "<div><br /></div><div>I have shared the slide deck for our product pitch "
                "meeting on Friday. Please take a look and do let me know if you have any "
                'suggestions.<br /></div><div id=""><div><img src="/zm/ImageDisplay?f=1.png'
                "&amp;mode=inline&amp;cid=0.28869215260.3894179596053002321.16b695cdb49__"
                'inline__img__src&amp;" width="145" height="145" style="float:left;" /><br />'
                "</div><div><br /></div><div><br /></div><div>Regards,<br /></div><div>Paula"
                '<br /><br /></div></div><br /><br /><div style="clear:both;"></div></div>'
                "<br /></div>"
            ),
            "IntegIdList": "34000000580271,",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ticket"]["status"] == "mocked"
    # messageId (int) coerced to str; new ticket created, case_id carries ticket ID
    assert payload["case_id"] == "JIRA-1560840837125110000"
    # subject and summary-based body should reach triage
    subject, body = mock_llm.triage_email.call_args.args
    assert subject == "Marketing - Product pitch"
    assert "slide deck" in body

    mock_patcher.stop()
