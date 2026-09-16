"""ASGI entrypoint: python -m incidentpilot"""

from incidentpilot.api.app import create_app

app = create_app()
