"""ASGI entry point for uvicorn.

Use this module as the uvicorn target — NOT cymatix_context.server — to
avoid the module-level side effect that fired when server.py was imported
directly during pytest collection:

    uvicorn cymatix_context._asgi:app

Keeping this in a separate module means server.py can be safely imported
anywhere (tests, tools, submodules) without triggering a database open.
"""
from cymatix_context.server import create_app

app = create_app()
