"""Shared fixtures: a fake portal served through httpx.MockTransport."""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest

from ccv2_portal_mcp import server
from ccv2_portal_mcp.client import PortalClient
from ccv2_portal_mcp.config import AppConfig, Settings, SubscriptionConfig

TOKEN_URL = "https://ias.example/oauth2/token"
BASE = "https://portalapi.commerce.ondemand.com/v2"
SUB = "sub123"


def make_sub(**over) -> SubscriptionConfig:
    base = dict(
        name="acme",
        code=SUB,
        client_id="cid",
        client_secret="secret",
        token_url=TOKEN_URL,
        resource="res",
        permissions={"d1": {"read", "build", "deploy"}, "p1": {"read"}},
        default_permissions={"read"},
    )
    base.update(over)
    return SubscriptionConfig(**base)


class FakePortal:
    """Route table + request log. Handlers get (request) and return (status, body) or Response."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], object] = {}
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.token_status = 200
        self.token_body = {"access_token": "tok-1", "token_type": "Bearer", "expires_in": 3600}

    def route(self, method: str, path: str, body=None, status: int = 200, *, handler=None):
        self.routes[(method.upper(), path)] = handler or (status, body)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == TOKEN_URL:
            self.token_calls += 1
            form = dict(p.split("=", 1) for p in request.content.decode().split("&"))
            assert form.get("resource") == "res", "resource param must be sent"
            assert form.get("grant_type") == "client_credentials"
            return httpx.Response(self.token_status, json=self.token_body if self.token_status == 200 else {"error": "x"})
        key = (request.method, request.url.path)
        entry = self.routes.get(key)
        if entry is None:
            return httpx.Response(404, json={"title": "Not Found", "detail": f"no route {key}"})
        if callable(entry):
            out = entry(request)
            if isinstance(out, httpx.Response):
                return out
            status, body = out
        else:
            status, body = entry
        if isinstance(body, bytes):
            return httpx.Response(status, content=body, headers={"content-type": "application/octet-stream"})
        return httpx.Response(status, json=body)


def make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def portal() -> FakePortal:
    return FakePortal()


@pytest.fixture
def client(portal) -> PortalClient:
    http = httpx.Client(transport=httpx.MockTransport(portal.handle), follow_redirects=False)
    return PortalClient(make_sub(), http=http, sleep=lambda s: None)


@pytest.fixture
def configured(portal, client, monkeypatch):
    """Wire the server registry to the fake portal."""
    settings = Settings(_env_file=None, wait_max_sec=5, wait_poll_sec=2, build_log_max_lines=50)
    cfg = AppConfig(subscriptions={"acme": client.sub}, default_subscription="acme")
    server.registry.configure(settings, cfg)
    server.registry.set_client("acme", client)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    return portal
