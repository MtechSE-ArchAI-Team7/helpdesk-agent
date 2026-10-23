"""Compatibility exports; implementations live in the canonical package."""

from helpdesk_agent.api.app import _lifespan as _lifespan
from helpdesk_agent.api.app import create_app as create_app
from helpdesk_agent.api.app import main as main

if __name__ == "__main__":
    main()
