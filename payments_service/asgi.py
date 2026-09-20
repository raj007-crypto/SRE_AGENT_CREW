"""ASGI entry point:  uvicorn payments_service.asgi:app --port 8001"""

from .app import create_app
from .settings import Settings

app = create_app(Settings.from_env())
