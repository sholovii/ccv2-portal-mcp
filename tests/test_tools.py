import pytest

from ccv2_portal_mcp import server
from tests.conftest import SUB, make_zip


def test_list_environments_hides_unreadable(configured, client):
    client.sub.default_permissions.clear()  # unlisted envs become invisible
    configured.route("GET", f"/v2/subscriptions/{SUB}/environments", {"value": [
        {"code": "d1", "type": "development", "status": "AVAILABLE"},
        {"code": "s1", "type": "staging"},
        {"code": "p1", "type": "production"},
    ], "count": 3})
    out = server.ccv2portal_list_environments()
    assert [e.code for e in out.environments] == ["d1", "p1"]
    assert out.environments[0].permissions == ["build", "deploy", "read"]


def test_get_environment_partial_failures(configured, client):
    configured.route("GET", f"/v2/subscriptions/{SUB}/environments", {"value": [{"code": "d1", "name": "Dev"}], "count": 1})
    configured.route("GET", f"/v2/subscriptions/{SUB}/environments/d1/services", {"value": [{"code": "hcs_platform_api", "desiredReplicas": 2, "availableReplicas": 2}]})
    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments", {"value": [{"code": "dep1", "buildCode": "b1", "status": "DEPLOYED", "environmentCode": "d1"}], "count": 1})
    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b1", {"code": "b1", "branch": "develop", "status": "SUCCESS"})
    configured.route("GET", f"/v2/subscriptions/{SUB}/environments/d1/scaling", {"title": "Forbidden"}, status=403)
    out = server.ccv2portal_get_environment("d1")
    assert out.services[0].desiredReplicas == 2
    assert out.latest_build.branch == "develop"
    assert out.scaling is None and "403" in out.scaling_error


def test_get_environment_requires_read(configured, client):
    client.sub.default_permissions.clear()
    with pytest.raises(server.PermissionDenied):
        server.ccv2portal_get_environment("s1")


def test_list_builds_filters(configured):
    configured.route("GET", f"/v2/subscriptions/{SUB}/builds", {"value": [
        {"code": "b1", "branch": "release/1.2", "status": "SUCCESS"},
        {"code": "b2", "branch": "develop", "status": "FAIL"},
        {"code": "b3", "branch": "develop", "status": "SUCCESS"},
    ], "count": 3})
    out = server.ccv2portal_list_builds(branch="develop", status="success")
    assert [b.code for b in out.builds] == ["b3"]


def test_get_build_terminal_and_deployments(configured):
    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b1", {"code": "b1", "status": "SUCCESS"})
    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b1/progress", {"buildStatus": "SUCCESS", "percentage": 100})
    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments", {"value": [{"code": "dep1", "buildCode": "b1"}], "count": 1})
    out = server.ccv2portal_get_build("b1")
    assert out.terminal and out.deployments[0].code == "dep1"


def test_get_build_log_modes(configured):
    log = "\n".join(["start", "[ERROR] boom", "trace", "end"] + [f"l{i}" for i in range(100)])
    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b1/logs", make_zip({"releaselog.txt": log, "other.txt": "x"}))
    errors = server.ccv2portal_get_build_log("b1")
    assert errors.entry == "releaselog.txt" and errors.lines[0].startswith("1: start")
    grep = server.ccv2portal_get_build_log("b1", mode="grep", pattern="BOOM")
    assert grep.returned_lines == 4  # match on line 2 + 2 context each side, clamped at line 1
    tail = server.ccv2portal_get_build_log("b1", mode="tail", lines=3)
    assert tail.returned_lines == 3 and tail.lines[-1].endswith("l99")
    other = server.ccv2portal_get_build_log("b1", mode="head", entry="other.txt")
    assert other.lines == ["1: x"]


def test_get_deployment_progress_failed_steps(configured):
    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments/dep1", {"code": "dep1", "buildCode": "b1", "environmentCode": "d1", "status": "FAIL"})
    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b1", {"code": "b1", "branch": "develop"})
    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments/dep1/progress", {
        "deploymentStatus": "FAIL", "percentage": 60,
        "stages": [{"name": "Deploy application", "status": "FAIL", "steps": [
            {"name": "update db", "status": "DONE"},
            {"name": "start api", "status": "FAIL", "message": "readiness probe failed"},
        ]}],
    })
    out = server.ccv2portal_get_deployment("dep1")
    assert out.terminal and out.build.branch == "develop"
    assert out.progress.stages[0].failed_steps == ["start api: readiness probe failed"]


def test_wait_for_build_bounded(configured):
    calls = {"n": 0}

    def progress(request):
        calls["n"] += 1
        return (200, {"buildStatus": "BUILDING", "percentage": 40})

    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b1/progress", handler=progress)
    out = server.ccv2portal_wait_for_build("b1", timeout_sec=999)
    assert not out.done and out.succeeded is None and "call again" in out.hint
    assert calls["n"] >= 1


def test_wait_for_deployment_done(configured):
    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments/dep1/progress", {"deploymentStatus": "DEPLOYED", "percentage": 100})
    out = server.ccv2portal_wait_for_deployment("dep1")
    assert out.done and out.succeeded and out.status == "DEPLOYED"


@pytest.mark.anyio
async def test_tools_registered_with_prefix(configured):
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert names >= {"ccv2portal_list_environments", "ccv2portal_get_build_log", "ccv2portal_wait_for_deployment"}
    assert all(n.startswith("ccv2portal_") for n in names)
    assert all(t.annotations.read_only_hint for t in tools if t.name.startswith(("ccv2portal_list_", "ccv2portal_get_", "ccv2portal_wait_")))


def test_list_certificates_tolerates_null_lists(configured):
    # the live portal sends "endpoints": null for unused certificates
    configured.route("GET", f"/v2/subscriptions/{SUB}/certificates", {"value": [
        {"code": "c1", "name": "old", "domains": None, "endpoints": None, "expirationDate": "2020-01-01T00:00:00Z"},
    ], "count": 1})
    out = server.ccv2portal_list_certificates()
    assert out.certificates[0].endpoints == [] and out.certificates[0].domains == []
    assert out.certificates[0].days_to_expiry < 0
