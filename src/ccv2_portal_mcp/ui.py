"""Read-only status page: configuration, environment state, permissions and server facts.

Under the streamable-http transport the routes are added to the MCP server itself, so the
page lives next to `/mcp` on the same port. For stdio setups `ccv2-portal-mcp ui` serves
the same page from a separate process.
"""

from __future__ import annotations

import functools
import logging

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .app import mcp, registry
from .status import StatusCollector
from .ui_page import PAGE

log = logging.getLogger("ccv2_portal_mcp.ui")


def _flag(request: Request, name: str, default: bool) -> bool:
    v = request.query_params.get(name)
    if v is None:
        return default
    return v.lower() not in {"0", "false", "no", "off"}


class StatusRoutes:
    def __init__(self, collector: StatusCollector) -> None:
        self.collector = collector

    async def page(self, request: Request) -> Response:
        return HTMLResponse(PAGE)

    async def api_status(self, request: Request) -> Response:
        tools = await mcp.list_tools()
        live = _flag(request, "live", True)
        refresh = _flag(request, "refresh", False)
        fn = functools.partial(self.collector.collect, tools=tools, live=live, refresh=refresh)
        data = await anyio.to_thread.run_sync(fn)
        return JSONResponse(data)

    async def healthz(self, request: Request) -> Response:
        return JSONResponse({"ok": True, "served_by": self.collector.served_by})

    def routes(self) -> list[Route]:
        return [
            Route("/", self.page, methods=["GET"]),
            Route("/status", self.page, methods=["GET"]),
            Route("/api/status", self.api_status, methods=["GET"]),
            Route("/healthz", self.healthz, methods=["GET"]),
        ]


def register_on_mcp() -> StatusRoutes:
    """Attach the status routes to the MCP server (call before mcp.run under streamable-http)."""
    routes = StatusRoutes(StatusCollector(registry, served_by="mcp"))
    for r in routes.routes():
        mcp.custom_route(r.path, methods=["GET"], include_in_schema=False)(r.endpoint)
    return routes


def build_app(served_by: str = "ui") -> Starlette:
    """A standalone Starlette app serving only the status page."""
    routes = StatusRoutes(StatusCollector(registry, served_by=served_by))
    return Starlette(routes=routes.routes())


def run_standalone(host: str, port: int) -> None:
    import uvicorn

    log.info("status page on http://%s:%d/", host, port)
    uvicorn.run(build_app(), host=host, port=port, log_level="warning")
