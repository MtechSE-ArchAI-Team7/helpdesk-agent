"""Vercel serverless entrypoint.

Adds src/ to sys.path (src layout isn't installed as a package on Vercel)
and exposes the FastAPI app object.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from helpdesk_agent.api.app import create_app  # noqa: E402

app = create_app()
