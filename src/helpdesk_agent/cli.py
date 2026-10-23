from __future__ import annotations

import argparse
import asyncio

from helpdesk_agent.api.factory import HelpdeskWorkflow


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AIOS helpdesk workflow scaffold")
    parser.add_argument("--case-id", default="case-001")
    parser.add_argument("--summary", default="Password reset request")
    parser.add_argument("--source", default="email", choices=["email", "slack"])
    parser.add_argument("--sender", default="ops.user")
    parser.add_argument("--service", default="identity")
    args = parser.parse_args()

    workflow = HelpdeskWorkflow()
    state = asyncio.run(
        workflow.run(
            {
                "case_id": args.case_id,
                "summary": args.summary,
                "source": args.source,
                "sender": args.sender,
                "service": args.service,
            }
        )
    )
    print(state.to_dict())


if __name__ == "__main__":
    main()
