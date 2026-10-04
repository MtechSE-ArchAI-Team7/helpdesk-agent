from __future__ import annotations

import argparse
import json
import os
import time
from copy import deepcopy
from typing import Any

import boto3
from botocore.config import Config

PRESERVED_FIELDS = (
    "roleArn",
    "networkConfiguration",
    "description",
    "authorizerConfiguration",
    "requestHeaderConfiguration",
    "protocolConfiguration",
    "lifecycleConfiguration",
    "metadataConfiguration",
    "environmentVariables",
    "filesystemConfigurations",
    "capacityProviderConfiguration",
    "platformVersion",
)


def build_update_request(current: dict[str, Any], runtime_id: str, image_uri: str) -> dict[str, Any]:
    if "containerConfiguration" not in current.get("agentRuntimeArtifact", {}):
        raise ValueError("Target runtime is not an existing container runtime")
    protocol = current.get("protocolConfiguration", {}).get("serverProtocol", "HTTP")
    if protocol != "HTTP":
        raise ValueError("Target runtime must use the HTTP protocol")
    if current.get("status") != "READY":
        raise ValueError("Target runtime must be READY before updating")
    if not current.get("roleArn"):
        raise ValueError("Target runtime has no execution role")
    request = {field: deepcopy(current[field]) for field in PRESERVED_FIELDS if current.get(field) is not None}
    request["agentRuntimeId"] = runtime_id
    request["agentRuntimeArtifact"] = {"containerConfiguration": {"containerUri": image_uri}}
    return request


def update_runtime(client: Any, runtime_id: str, image_uri: str, timeout_seconds: float = 900) -> dict[str, str]:
    current = client.get_agent_runtime(agentRuntimeId=runtime_id)
    response = client.update_agent_runtime(**build_update_request(current, runtime_id, image_uri))
    version = response["agentRuntimeVersion"]
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        runtime = client.get_agent_runtime(agentRuntimeId=runtime_id, agentRuntimeVersion=version)
        status = runtime["status"]
        if status == "READY":
            actual_uri = runtime.get("agentRuntimeArtifact", {}).get("containerConfiguration", {}).get("containerUri")
            if runtime.get("agentRuntimeVersion") != version or actual_uri != image_uri:
                raise RuntimeError("READY runtime does not match the requested version and image")
            return {
                "runtime_id": runtime_id,
                "runtime_version": version,
                "runtime_arn": runtime["agentRuntimeArn"],
                "status": status,
                "image_uri": image_uri,
            }
        if status in {"CREATE_FAILED", "UPDATE_FAILED", "DELETING", "DELETED", "DELETE_FAILED"}:
            raise RuntimeError(f"AgentCore runtime entered {status}; inspect failureReason in the AWS console")
        time.sleep(min(10, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"AgentCore runtime version {version} did not become READY within {timeout_seconds:g}s")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--image-uri", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    client = boto3.client(
        "bedrock-agentcore-control",
        region_name=args.region,
        config=Config(connect_timeout=10, read_timeout=30, retries={"mode": "standard", "max_attempts": 3}),
    )
    result = update_runtime(client, args.runtime_id, args.image_uri)
    print(json.dumps(result))
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as output:
            for name, value in result.items():
                output.write(f"{name}={value}\n")


if __name__ == "__main__":
    main()