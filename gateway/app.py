"""Vercel entrypoint: exposes the ASGI `app` built from environment variables."""

from solidpilot_gateway.app import create_app
from solidpilot_gateway.config import Settings

app = create_app(Settings.from_env())
