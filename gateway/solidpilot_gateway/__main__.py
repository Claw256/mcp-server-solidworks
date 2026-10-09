"""Local run for development: `python -m solidpilot_gateway` (needs a Redis; see .env.example).

Production runs on Vercel, which imports `app.py` at the gateway root instead.
"""

import logging
import os
from urllib.parse import urlsplit

import uvicorn

from .app import create_app
from .config import Settings

if __name__ == "__main__":
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", urlsplit(settings.public_url).port or 8080)),
        proxy_headers=True,
    )
