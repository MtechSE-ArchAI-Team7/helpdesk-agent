"""CLI entrypoint for running the helpdesk API server."""

import os

import uvicorn


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("helpdesk_agent.api.app:create_app", host="0.0.0.0", port=port, factory=True)  # nosec B104


if __name__ == "__main__":
    main()
