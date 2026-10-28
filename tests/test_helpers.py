import pytest

from ccv2_portal_mcp import server
from ccv2_portal_mcp.app import ConfirmationRequired
from tests.conftest import SUB, make_zip


def _deployments(portal, items):
    def handler(request):
        env = request.url.params.get("environmentCode")
        status = request.url.params.get("status")
        build = request.url.params.get("buildCode")
        out = [d for d in items if (not env or d.get("environmentCode") == env) and (not status or d.get("status") == status) and (not build or d.get("buildCode") == build)]
        return (200, {"value": out, "count": len(out)})

    portal.route("GET", f"/v2/subscriptions/{SUB}/deployments", handler=handler)


def _builds(portal, items):
    for b in items:
        portal.route("GET", f"/v2/subscriptions/{SUB}/builds/{b['code']}", b)
    portal.route("GET", f"/v2/subscriptions/{SUB}/builds", {"value": items, "count": len(items)})


def test_whats_deployed_and_comparison(configured):
    _builds(configured, [
        {"code": "b2", "branch": "release/2", "status": "SUCCESS", "buildStartTimestamp": "2026-10-27T10:00:00Z"},
        {"code": "b1", "branch": "release/1", "status": "SUCCESS", "buildStartTimestamp": "2026-10-20T10:00:00Z"},
    ])
    _deployments(configured, [
        {"code": "d-s1", "environmentCode": "s1", "buildCode": "b2", "status": "DEPLOYED", "createdTimestamp": "2026-10-27T12:00:00Z"},
        {"code": "d-p1-run", "environmentCode": "p1", "buildCode": "b2", "status": "DEPLOYING", "createdTimestamp": "2026-10-28T09:00:00Z"},
        {"code": "d-p1", "environmentCode": "p1", "buildCode": "b1", "status": "DEPLOYED", "createdTimestamp": "2026-10-21T12:00:00Z"},
    ])
    out = server.ccv2portal_whats_deployed(["s1", "p1"])
    s1, p1 = out.environments
    assert s1.build_code == "b2" and s1.branch == "release/2"
    assert p1.build_code == "b1" and p1.active_deployment.startswith("d-p1-run")
    assert any("different" in c or "runs release/2" in c for c in out.comparison)
    assert any("deployment in progress" in c for c in out.comparison)


def test_rollback_plan_then_execute(configured, client):
    _builds(configured, [
        {"code": "b2", "branch": "release/2", "status": "SUCCESS"},
        {"code": "b1", "branch": "release/1", "status": "SUCCESS"},
    ])
    _deployments(configured, [
        {"code": "dep2", "environmentCode": "d1", "buildCode": "b2", "status": "DEPLOYED", "previousDeploymentCode": "dep1"},
        {"code": "dep1", "environmentCode": "d1", "buildCode": "b1", "status": "DEPLOYED"},
    ])
    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments/dep1", {"code": "dep1", "environmentCode": "d1", "buildCode": "b1", "status": "DEPLOYED"})
    plan = server.ccv2portal_rollback_environment("d1")
    assert not plan.executed and plan.current_build == "b2" and plan.target_build == "b1" and "confirm=true" in plan.hint
    configured.route("POST", f"/v2/subscriptions/{SUB}/deployments", {"code": "dep3"}, status=201)
    done = server.ccv2portal_rollback_environment("d1", confirm=True)
    assert done.executed and done.new_deployment_code == "dep3"


def test_rollback_without_previous(configured):
    _builds(configured, [{"code": "b1", "branch": "x", "status": "SUCCESS"}])
    _deployments(configured, [{"code": "dep1", "environmentCode": "d1", "buildCode": "b1", "status": "DEPLOYED"}])
    plan = server.ccv2portal_rollback_environment("d1", confirm=True)
    assert not plan.executed and "target_build_code" in plan.hint


def test_diagnose_build_failure_categories(configured):
    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b1", {"code": "b1", "branch": "develop", "status": "FAIL"})
    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b1/progress", {"buildStatus": "FAIL", "errorMessage": "Build failed"})
    log = "\n".join(["compiling", "/src/Foo.java:12: error: cannot find symbol", "  symbol: Bar", "BUILD FAILED"])
    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b1/logs", make_zip({"releaselog.txt": log}))
    out = server.ccv2portal_diagnose_build_failure("b1")
    assert out.category == "compile-error" and out.portal_error == "Build failed"
    assert any("cannot find symbol" in e for e in out.evidence)

    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b2", {"code": "b2", "branch": "develop", "status": "SUCCESS"})
    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b2/progress", {"buildStatus": "SUCCESS"})
    assert server.ccv2portal_diagnose_build_failure("b2").category == "not-failed"


def test_pre_deploy_checklist(configured, client):
    client.sub.permissions["p1"] = {"read", "deploy"}  # missing deploy_prod
    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b1", {"code": "b1", "branch": "feature/wip", "status": "SUCCESS"})
    _deployments(configured, [{"code": "g1", "environmentCode": "p1", "buildCode": "b0", "status": "DEPLOYED", "strategy": "GREEN", "expireAtTimestamp": "2026-10-29T00:00:00Z"}])
    configured.route("GET", f"/v2/subscriptions/{SUB}/deploymentmodes", {"value": [{"environmentCode": "p1", "dataMigrationMode": ["NONE", "UPDATE"], "deploymentMode": ["ROLLING_UPDATE", "GREEN"]}]})
    configured.route("GET", f"/v2/subscriptions/{SUB}/environments/p1/databackups", {"value": [], "count": 0})
    configured.route("GET", f"/v2/subscriptions/{SUB}/environments/p1/endpoints", {"value": [{"code": "web", "maintenanceMode": True}], "count": 1})
    configured.route("GET", f"/v2/subscriptions/{SUB}/certificates", {"value": [], "count": 0})
    out = server.ccv2portal_pre_deploy_checklist("p1", "b1", strategy="RECREATE")
    by = {c.name: c for c in out.checks}
    assert not out.ready
    assert by["agent permissions"].level == "fail" and "deploy_prod" in by["agent permissions"].detail
    assert by["build"].level == "ok" and by["branch naming"].level == "warn"
    assert by["allowed modes"].level == "fail" and "RECREATE" in by["allowed modes"].detail
    assert by["last backup"].level == "warn" and by["maintenance mode"].level == "warn"
    assert by["blue-green"].level == "warn"


def test_pre_deploy_checklist_ready(configured, client):
    client.sub.permissions["d1"] = {"read", "deploy"}
    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b1", {"code": "b1", "branch": "develop", "status": "SUCCESS"})
    _deployments(configured, [])
    configured.route("GET", f"/v2/subscriptions/{SUB}/deploymentmodes", {"value": []})
    configured.route("GET", f"/v2/subscriptions/{SUB}/environments/d1/databackups", {"value": [], "count": 0})
    configured.route("GET", f"/v2/subscriptions/{SUB}/environments/d1/endpoints", {"value": [], "count": 0})
    configured.route("GET", f"/v2/subscriptions/{SUB}/certificates", {"value": [], "count": 0})
    out = server.ccv2portal_pre_deploy_checklist("d1", "b1")
    assert out.ready


def test_compare_properties_masks_secrets(configured):
    base = f"/v2/subscriptions/{SUB}/environments"
    configured.route("GET", f"{base}/d1/services/hcs_common/properties/a", {"key": "a", "value": "1"})
    configured.route("GET", f"{base}/s1/services/hcs_common/properties/a", {"key": "a", "value": "2"})
    configured.route("GET", f"{base}/d1/services/hcs_common/properties/api.secret", {"key": "api.secret", "value": "x"})
    configured.route("GET", f"{base}/s1/services/hcs_common/properties/api.secret", {"key": "api.secret", "value": "x"})
    out = server.ccv2portal_compare_properties("d1", "s1", ["a", "api.secret", "missing"])
    by = {d.key: d for d in out.diffs}
    assert not by["a"].same and by["api.secret"].same and by["api.secret"].a == "***"
    assert by["missing"].error_a and out.different == 2


def test_release_timeline(configured):
    _builds(configured, [
        {"code": "b1", "branch": "develop", "status": "SUCCESS", "buildStartTimestamp": "2026-10-27T10:00:00Z"},
        {"code": "b0", "branch": "develop", "status": "SUCCESS", "buildStartTimestamp": "2020-01-01T10:00:00Z"},
    ])
    _deployments(configured, [{"code": "dep1", "environmentCode": "d1", "buildCode": "b1", "status": "DEPLOYED", "createdTimestamp": "2026-10-27T12:00:00Z", "databaseUpdateMode": "NONE", "strategy": "ROLLING_UPDATE"}])
    out = server.ccv2portal_release_timeline(days=3650)
    kinds = [(e.kind, e.code) for e in out.events]
    assert kinds[0] == ("deployment", "dep1") and ("build", "b0") in kinds
    assert server.ccv2portal_release_timeline(days=2, branch="nope").events == []
