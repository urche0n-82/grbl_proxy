"""FastAPI application factory for grbl-proxy web server."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from grbl_proxy.config import Config
    from grbl_proxy.web.console_log import ConsoleLog
    from grbl_proxy.web.status import ProxyControl, ProxyStatus

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    status: "ProxyStatus",
    control: "ProxyControl",
    console: "ConsoleLog",
    config: "Config",
) -> FastAPI:
    """Create and configure the FastAPI application.

    Uses a factory function (rather than a module-level app instance) so that
    tests can inject mock facades without touching global state.
    """
    from grbl_proxy.web.routes import create_router, WebSocketManager

    ws_manager = WebSocketManager(status)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import asyncio
        task = asyncio.create_task(ws_manager.broadcast_loop(), name="ws-broadcast")
        try:
            yield
        finally:
            task.cancel()
            import contextlib
            with contextlib.suppress(Exception):
                await task

    app = FastAPI(
        title="grbl-proxy",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    # Store facades in app.state for route dependency injection
    app.state.proxy_status = status
    app.state.proxy_control = control
    app.state.console_log = console
    app.state.config = config
    app.state.ws_manager = ws_manager

    app.include_router(create_router())

    # Serve the static dashboard — must come last so /api routes take priority
    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app
