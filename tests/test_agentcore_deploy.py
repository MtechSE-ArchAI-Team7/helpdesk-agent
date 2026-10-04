from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from scripts import update_agentcore_runtime as deploy

IMAGE_URI = "123456789012.dkr.ecr.ap-southeast-1.amazonaws.com/aios/helpdesk-agent:sha-run-attempt"


@pytest.fixture
def current_runtime() -> dict:
    return {
        "agentRuntimeId": "helpdesk-123",
        "agentRuntimeVersion": "1",
        "agentRuntimeArn": "arn:aws:bedrock-agentcore:ap-southeast-1:123456789012:runtime/helpdesk-123",
        "status": "READY",
        "roleArn": "arn:aws:iam::123456789012:role/runtime",
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": "old-image"}},
        "networkConfiguration": {"networkMode": "VPC", "networkModeConfig": {
            "subnets": ["subnet-123"], "securityGroups": ["sg-123"], "requireServiceS3Endpoint": False,
        }},
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "environmentVariables": {"KEEP": "existing-value"},
        "authorizerConfiguration": {"customJWTAuthorizer": {"discoveryUrl": "https://identity.example/.well-known"}},
        "requestHeaderConfiguration": {"requestHeaderAllowlist": ["X-Request-Id"]},
        "lifecycleConfiguration": {"maxLifetime": 3600},
        "metadataConfiguration": {"requireMMDSV2": True},
        "filesystemConfigurations": [{"sessionStorage": {"mountPath": "/mnt/data"}}],
        "capacityProviderConfiguration": {"capacityProviderArn": "capacity-arn"},
        "platformVersion": "1",
        "description": "Existing helpdesk runtime",
        "workloadIdentityDetails": {"workloadIdentityArn": "identity-arn"},
    }


def test_preserves_configuration_and_only_replaces_image(current_runtime: dict) -> None:
    original = deepcopy(current_runtime)
    request = deploy.build_update_request(current_runtime, "helpdesk-123", IMAGE_URI)
    assert request["agentRuntimeArtifact"] == {"containerConfiguration": {"containerUri": IMAGE_URI}}
    for field in deploy.PRESERVED_FIELDS:
        assert request[field] == current_runtime[field]
    assert set(request) == {*deploy.PRESERVED_FIELDS, "agentRuntimeId", "agentRuntimeArtifact"}
    request["environmentVariables"]["KEEP"] = "changed"
    assert current_runtime == original


@pytest.mark.parametrize("field,value", [
    ("agentRuntimeArtifact", {"codeConfiguration": {}}),
    ("protocolConfiguration", {"serverProtocol": "MCP"}),
    ("status", "UPDATING"),
    ("roleArn", None),
])
def test_rejects_incompatible_runtime(current_runtime: dict, field: str, value: object) -> None:
    current_runtime[field] = value
    with pytest.raises(ValueError):
        deploy.build_update_request(current_runtime, "helpdesk-123", IMAGE_URI)


def test_update_waits_for_specific_new_version(current_runtime: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    ready = deepcopy(current_runtime)
    ready["agentRuntimeVersion"] = "2"
    ready["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] = IMAGE_URI
    client = MagicMock()
    client.update_agent_runtime.return_value = {"agentRuntimeVersion": "2"}
    client.get_agent_runtime.side_effect = [current_runtime, {"status": "UPDATING"}, ready]
    monkeypatch.setattr(deploy.time, "sleep", lambda delay: None)
    result = deploy.update_runtime(client, "helpdesk-123", IMAGE_URI)
    assert result["status"] == "READY"
    assert result["runtime_version"] == "2"
    client.get_agent_runtime.assert_called_with(agentRuntimeId="helpdesk-123", agentRuntimeVersion="2")


def test_failed_update_is_not_success(current_runtime: dict) -> None:
    client = MagicMock()
    client.update_agent_runtime.return_value = {"agentRuntimeVersion": "2"}
    client.get_agent_runtime.side_effect = [current_runtime, {"status": "UPDATE_FAILED"}]
    with pytest.raises(RuntimeError, match="UPDATE_FAILED"):
        deploy.update_runtime(client, "helpdesk-123", IMAGE_URI)


def test_timeout_is_not_success(current_runtime: dict) -> None:
    client = MagicMock()
    client.get_agent_runtime.return_value = current_runtime
    client.update_agent_runtime.return_value = {"agentRuntimeVersion": "2"}
    with pytest.raises(TimeoutError):
        deploy.update_runtime(client, "helpdesk-123", IMAGE_URI, timeout_seconds=0)


@pytest.mark.parametrize("field,value", [("agentRuntimeVersion", "1"), ("agentRuntimeArtifact", {
    "containerConfiguration": {"containerUri": "wrong-image"},
})])
def test_ready_must_match_version_and_image(current_runtime: dict, field: str, value: object) -> None:
    ready = deepcopy(current_runtime)
    ready["agentRuntimeVersion"] = "2"
    ready["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"] = IMAGE_URI
    ready[field] = value
    client = MagicMock()
    client.update_agent_runtime.return_value = {"agentRuntimeVersion": "2"}
    client.get_agent_runtime.side_effect = [current_runtime, ready]
    with pytest.raises(RuntimeError, match="does not match"):
        deploy.update_runtime(client, "helpdesk-123", IMAGE_URI)