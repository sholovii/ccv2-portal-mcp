"""Status page: the data collector and the HTTP routes, against the fake portal."""

from __future__ import annotations

from starlette.testclient import TestClient

from ccv2_portal_mcp import server, ui
from ccv2_portal_mcp.status import StatusCollector, audit_tail, tools_summary
from tests.conftest import SUB


def _routes(configured):
    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments", handler=lambda r: (200, {"value": [
        {"code": "dep1", "buildCode": "b1", "status": "DEPLOYED", "environmentCode": r.url.params.get("environmentCode"),
         "strategy": "ROLLING_UPDATE", "databaseUpdateMode": "UPDATE", "deployedTimestamp": "2026-01-01T10:00:00Z", "createdBy": "tech"},
    ], "count": 1}))
    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b1", {"code": "b1", "branch": "develop", "name": "DEV", "status": "SUCCESS"})


def test_collect_config_and_live(configured, client):
    _routes(configured)
    client.sub.permissions["zz"] = {"read"}  # configured but not in the portal
    c = StatusCollector(server.registry, served_by="ui")
    doc = c.collect(live=True)

    assert doc["server"]["served_by"] == "ui" and doc["server"]["running"] is False
    assert doc["config_error"] is None
    sub = doc["subscriptions"][0]
    assert sub["name"] == "acme" and sub["is_default"]
    assert sub["client_id"] == "***"  # short ids are fully masked
    assert "client_secret" not in sub and "resource" not in sub  # only *_set flags
    assert sub["client_secret_set"] and sub["resource_set"]
    live = sub["live"]
    assert live["auth"]["ok"] and live["auth"]["token_expires_in_sec"] > 3000
    assert live["unknown_environments"] == ["zz"]
    by_code = {e["code"]: e for e in live["environments"]}
    assert by_code["p1"]["production"] and not by_code["d1"]["production"]
    assert by_code["d1"]["permissions"] == ["build", "deploy", "read"] and not by_code["d1"]["permissions_from_default"]
    assert by_code["s1"]["permissions_from_default"]
    assert by_code["d1"]["deployed"]["build_code"] == "b1" and by_code["d1"]["deployed"]["branch"] == "develop"
    assert live["cached"] is False


def test_collect_hides_unreadable_and_caches(configured, client):
    _routes(configured)
    client.sub.default_permissions.clear()
    c = StatusCollector(server.registry, served_by="mcp")
    first = c.collect()
    s1 = next(e for e in first["subscriptions"][0]["live"]["environments"] if e["code"] == "s1")
    assert s1["readable"] is False and s1["permissions"] == [] and s1["deployed"] is None
    calls = len(configured.requests)
    second = c.collect()
    assert second["subscriptions"][0]["live"]["cached"] is True
    assert len(configured.requests) == calls  # nothing refetched
    third = c.collect(refresh=True)
    assert third["subscriptions"][0]["live"]["cached"] is False
    assert len(configured.requests) > calls


def test_collect_reports_auth_failure(configured, client):
    configured.token_status = 401
    client._tokens.invalidate()
    doc = StatusCollector(server.registry, served_by="ui").collect()
    live = doc["subscriptions"][0]["live"]
    assert live["auth"]["ok"] is False and "401" in live["auth"]["error"]
    assert live["environments"] == [] and "authentication failed" in live["error"]


def test_audit_tail_and_tools_summary(tmp_path):
    p = tmp_path / "audit.jsonl"
    p.write_text('{"ts": "t1", "tool": "a"}\n{"ts": "t2", "tool": "b"}\nnot json\n')
    tail = audit_tail(str(p), 2)
    assert tail["total_lines"] == 3 and tail["entries"][0] == {"raw": "not json"} and tail["entries"][1]["tool"] == "b"
    assert audit_tail("", 5)["entries"] == [] and audit_tail(str(tmp_path / "missing"), 5)["note"]

    class T:
        def __init__(self, name, ro):
            self.name = name
            self.annotations = type("A", (), {"read_only_hint": ro, "destructive_hint": False})()

    summary = tools_summary([T("ccv2portal_x", False), T("ccv2portal_a", True)])
    assert summary["count"] == 2 and summary["read_only"] == 1 and summary["items"][0]["name"] == "ccv2portal_a"


def test_http_routes(configured):
    _routes(configured)
    app = ui.build_app()
    with TestClient(app) as tc:
        assert tc.get("/healthz").json() == {"ok": True, "served_by": "ui"}
        page = tc.get("/")
        assert page.status_code == 200 and "ccv2-portal-mcp" in page.text and "api/status" in page.text
        assert tc.get("/status").status_code == 200
        data = tc.get("/api/status").json()
        assert data["tools"]["count"] > 40 and data["tools"]["read_only"] >= 17
        assert data["subscriptions"][0]["live"]["environments"][0]["code"] == "d1"
        no_live = tc.get("/api/status?live=0").json()
        assert no_live["subscriptions"][0]["live"] is None


def test_register_on_mcp_adds_routes(configured):
    routes = ui.register_on_mcp()
    assert [r.path for r in routes.routes()] == ["/", "/status", "/api/status", "/healthz"]
    app = server.mcp.streamable_http_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert {"/", "/api/status", "/healthz"} <= paths


def test_concurrent_misses_share_one_fetch(configured):
    import threading

    _routes(configured)
    c = StatusCollector(server.registry, served_by="ui")
    results = []
    threads = [threading.Thread(target=lambda: results.append(c.collect())) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    env_calls = [r for r in configured.requests if r.url.path.endswith("/environments")]
    assert len(env_calls) == 1
    assert sum(1 for r in results if r["subscriptions"][0]["live"]["cached"] is False) == 1
