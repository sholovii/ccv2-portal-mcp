"""Duration estimates from the subscription's own builds and deployments."""

from __future__ import annotations

from ccv2_portal_mcp import server
from ccv2_portal_mcp.history import describe, typical_build_duration, typical_deployment_duration
from tests.conftest import SUB


def _builds(portal, items):
    portal.route("GET", f"/v2/subscriptions/{SUB}/builds", {"value": items, "count": len(items)})


def test_build_estimate_uses_successful_builds_only(configured, client):
    _builds(configured, [
        {"code": "b3", "status": "FAIL", "buildStartTimestamp": "2026-01-03T10:00:00Z", "buildEndTimestamp": "2026-01-03T12:00:00Z"},
        {"code": "b2", "status": "SUCCESS", "buildStartTimestamp": "2026-01-02T10:00:00Z", "buildEndTimestamp": "2026-01-02T10:12:00Z"},
        {"code": "b1", "status": "SUCCESS", "buildStartTimestamp": "2026-01-01T10:00:00Z", "buildEndTimestamp": "2026-01-01T10:09:30Z"},
        {"code": "b0", "status": "SUCCESS", "buildStartTimestamp": None, "buildEndTimestamp": None},
    ])
    t = typical_build_duration(client, 3600)
    assert t["samples"] == 2 and t["min_sec"] == 570 and t["max_sec"] == 720
    assert t["suggested_timeout_sec"] == 900  # 720 * 1.25 rounded up to a minute
    text = describe(t, kind="build", wait_tool="ccv2portal_wait_for_build", wait_max_sec=3600)
    assert text == "Recent builds here took 10 min to 12 min (2 samples); one ccv2portal_wait_for_build call with timeout_sec=900 covers it."


def test_estimate_is_capped_and_absent_without_history(configured, client):
    _builds(configured, [{"code": "b1", "status": "SUCCESS", "buildStartTimestamp": "2026-01-01T10:00:00Z", "buildEndTimestamp": "2026-01-01T11:30:00Z"}])
    assert typical_build_duration(client, 3600)["suggested_timeout_sec"] == 3600
    _builds(configured, [])
    assert typical_build_duration(client, 3600) is None
    assert "No build history" in describe(None, kind="build", wait_tool="ccv2portal_wait_for_build", wait_max_sec=3600)
    configured.routes.pop(("GET", f"/v2/subscriptions/{SUB}/builds"))
    assert typical_build_duration(client, 3600) is None  # a portal error is not fatal


def test_deployment_estimate_filters_by_environment(configured, client):
    def handler(request):
        env = request.url.params.get("environmentCode")
        items = [
            {"code": "d1a", "environmentCode": "d1", "status": "DEPLOYED", "createdTimestamp": "2026-01-01T10:00:00Z", "deployedTimestamp": "2026-01-01T10:20:00Z"},
            {"code": "p1a", "environmentCode": "p1", "status": "DEPLOYED", "createdTimestamp": "2026-01-01T10:00:00Z", "deployedTimestamp": "2026-01-01T11:00:00Z"},
        ]
        out = [i for i in items if not env or i["environmentCode"] == env]
        return 200, {"value": out, "count": len(out)}

    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments", handler=handler)
    assert typical_deployment_duration(client, 3600, "d1")["max_sec"] == 1200
    assert typical_deployment_duration(client, 3600)["max_sec"] == 3600


def test_create_build_hint_comes_from_history(configured):
    server.registry.settings.wait_max_sec = 3600  # the fixture caps waits at 5 s
    configured.route("POST", f"/v2/subscriptions/{SUB}/builds", {"code": "b9"}, status=201)
    _builds(configured, [{"code": "b1", "status": "SUCCESS", "buildStartTimestamp": "2026-01-01T10:00:00Z", "buildEndTimestamp": "2026-01-01T10:30:00Z"}])
    out = server.ccv2portal_create_build("develop")
    assert out.typical_duration["samples"] == 1 and "timeout_sec=2280" in out.hint
    _builds(configured, [])
    out = server.ccv2portal_create_build("develop")
    assert out.typical_duration is None and "No build history" in out.hint


def test_wait_timeout_hint_includes_estimate(configured):
    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b1/progress", {"buildStatus": "BUILDING", "percentage": 10})
    _builds(configured, [{"code": "b1", "status": "SUCCESS", "buildStartTimestamp": "2026-01-01T10:00:00Z", "buildEndTimestamp": "2026-01-01T10:08:00Z"}])
    out = server.ccv2portal_wait_for_build("b1", timeout_sec=3, poll_sec=2)
    assert not out.done and out.typical_duration["max_sec"] == 480 and "Recent builds here took 8 min" in out.hint
