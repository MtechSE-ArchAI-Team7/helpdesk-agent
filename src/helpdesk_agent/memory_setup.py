"""Explicit provisioning command; the application never creates AWS resources on startup."""

from __future__ import annotations

import argparse
import json
import time

import boto3
from botocore.config import Config

from .modules.memory import FACTS_NAMESPACE, SUMMARY_NAMESPACE


def main() -> None:
    parser = argparse.ArgumentParser(description="Create ticket-scoped AgentCore Memory with cross-ticket extraction")
    parser.add_argument("--name", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--retention-days", type=int, default=90)
    parser.add_argument("--kms-key-arn")
    args = parser.parse_args()
    if not 3 <= args.retention_days <= 365:
        parser.error("--retention-days must be between 3 and 365")
    client = boto3.client(
        "bedrock-agentcore-control",
        region_name=args.region,
        config=Config(connect_timeout=5, read_timeout=10, retries={"total_max_attempts": 2}),
    )
    arguments = {
        "name": args.name,
        "description": "Ticket history and organization-scoped reports/troubleshooting",
        "eventExpiryDuration": args.retention_days,
        "memoryStrategies": [
            {"summaryMemoryStrategy": {"name": "TicketSummary", "namespaceTemplates": [SUMMARY_NAMESPACE]}},
            {"semanticMemoryStrategy": {"name": "TicketFacts", "namespaceTemplates": [FACTS_NAMESPACE]}},
        ],
    }
    if args.kms_key_arn:
        arguments["encryptionKeyArn"] = args.kms_key_arn
    memory = client.create_memory(**arguments)["memory"]
    print(json.dumps({"memory_id": memory["id"], "region": args.region, "status": memory["status"]}), flush=True)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        memory = client.get_memory(memoryId=memory["id"])["memory"]
        if memory["status"] == "FAILED":
            raise RuntimeError(f"Memory creation failed: {memory.get('failureReason', 'unknown')}")
        if memory["status"] == "ACTIVE" and all(s["status"] == "ACTIVE" for s in memory.get("strategies", [])):
            print(f"HELPDESK_AGENT_MEMORY_ID={memory['id']}")
            return
        time.sleep(5)
    raise RuntimeError(f"Memory {memory['id']} is not active yet; check its status before enabling it")
