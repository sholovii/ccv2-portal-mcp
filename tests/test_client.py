import httpx
import pytest

from ccv2_portal_mcp.auth import AUTH_HEADER, AuthError
from ccv2_portal_mcp.client import PortalError
from tests.conftest import SUB


def test_token_fetched_once_and_sent_in_approuter_header(portal, client):
    portal.route("GET", f"/v2/subscriptions/{SUB}/environments", {"value": [{"code": "d1"}], "count": 1})
    client.environments()
    client.environments()
    assert portal.token_calls == 1
    api = [r for r in portal.requests if "environments" in r.url.path]
    assert api[0].headers[AUTH_HEADER] == "Bearer tok-1"
    assert "authorization" not in api[0].headers


def test_401_triggers_single_refresh(portal, client):
    seen = {"n": 0}

    def handler(request):
        seen["n"] += 1
        return (401, {"title": "Unauthorized"}) if seen["n"] == 1 else (200, {"value": [], "count": 0})

    portal.route("GET", f"/v2/subscriptions/{SUB}/environments", handler=handler)
    client.environments()
    assert portal.token_calls == 2 and seen["n"] == 2


def test_html_login_page_is_auth_error(portal, client):
    portal.route(
        "GET", f"/v2/subscriptions/{SUB}/environments",
        handler=lambda r: httpx.Response(200, text="<html>login</html>", headers={"content-type": "text/html"}),
    )
    with pytest.raises(AuthError, match="HTML login page"):
        client.environments()


def test_redirect_points_at_host_problem(portal, client):
    portal.route(
        "GET", f"/v2/subscriptions/{SUB}/environments",
        handler=lambda r: httpx.Response(302, headers={"location": "https://portalapi.commerce.ondemand.com/"}),
    )
    with pytest.raises(PortalError, match="retired host"):
        client.environments()


def test_429_retried_then_succeeds(portal, client):
    seen = {"n": 0}

    def handler(request):
        seen["n"] += 1
        if seen["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return (200, {"value": [], "count": 0})

    portal.route("GET", f"/v2/subscriptions/{SUB}/environments", handler=handler)
    client.environments()
    assert seen["n"] == 3


def test_problem_json_error_surfaces_detail(portal, client):
    portal.route("POST", f"/v2/subscriptions/{SUB}/deployments", {"title": "Conflict", "detail": "deployment running"}, status=409)
    with pytest.raises(PortalError) as ei:
        client.create_deployment(build_code="b", environment="d1", database_update_mode="NONE", strategy="ROLLING_UPDATE")
    assert ei.value.status == 409
    assert "busy" in str(ei.value) and "deployment running" in str(ei.value)


def test_501_explains_feature_flag(portal, client):
    portal.route("GET", f"/v2/subscriptions/{SUB}/builds/x", {}, status=501)
    with pytest.raises(PortalError, match="switched on in the Cloud Portal"):
        client.build("x")


def test_pagination_walks_pages(portal, client):
    def handler(request):
        skip = int(request.url.params.get("$skip", 0))
        top = int(request.url.params.get("$top"))
        assert top <= 100
        items = [{"code": f"b{i}"} for i in range(skip, min(skip + top, 150))]
        return (200, {"value": items, "count": 150})

    portal.route("GET", f"/v2/subscriptions/{SUB}/builds", handler=handler)
    assert len(client.builds(limit=150)) == 150
    assert len(client.builds(limit=7)) == 7
    assert len(client.get_paged(f"/subscriptions/{SUB}/builds")) == 150


def test_services_falls_back_to_undocumented_path(portal, client):
    portal.route("GET", f"/v2/{SUB}/environments/d1/services", [{"code": "hcs_platform_api", "desiredReplicas": 2}])
    assert client.services("d1")[0]["code"] == "hcs_platform_api"


def test_token_failure_message(portal, client):
    portal.token_status = 401
    with pytest.raises(AuthError, match="secret has not expired"):
        client.environments()
