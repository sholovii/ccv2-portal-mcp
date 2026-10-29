import json

import pytest

from ccv2_portal_mcp import server
from ccv2_portal_mcp.app import ConfirmationRequired, PermissionDenied
from ccv2_portal_mcp.audit import Audit
from ccv2_portal_mcp.client import PortalError
from tests.conftest import SUB


def _ok_build(portal, code="b1", status="SUCCESS", branch="develop"):
    portal.route("GET", f"/v2/subscriptions/{SUB}/builds/{code}", {"code": code, "status": status, "branch": branch})


def _no_running(portal):
    portal.route("GET", f"/v2/subscriptions/{SUB}/deployments", {"value": [], "count": 0})


def _modes(portal, env="d1", db=("NONE", "UPDATE", "INITIALIZE"), st=("ROLLING_UPDATE", "RECREATE", "GREEN")):
    portal.route("GET", f"/v2/subscriptions/{SUB}/deploymentmodes", {"value": [{"environmentCode": env, "dataMigrationMode": list(db), "deploymentMode": list(st)}]})


def test_create_build_posts_and_names(configured):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return (201, {"subscriptionCode": SUB, "code": "20260101.1"})

    configured.route("POST", f"/v2/subscriptions/{SUB}/builds", handler=handler)
    out = server.ccv2portal_create_build("feature/x y")
    assert out.code == "20260101.1" and out.executed
    assert seen["branch"] == "feature/x y" and seen["applicationCode"] == "commerce-cloud"
    assert seen["name"].startswith("feature-x-y-")


def test_create_build_needs_build_permission(configured, client):
    client.sub.permissions = {"p1": {"read"}}
    client.sub.default_permissions = {"read"}
    with pytest.raises(PermissionDenied):
        server.ccv2portal_create_build("develop")


def test_create_deployment_happy_path_dev(configured):
    _ok_build(configured)
    _no_running(configured)
    _modes(configured)
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return (201, {"code": "dep9"})

    configured.route("POST", f"/v2/subscriptions/{SUB}/deployments", handler=handler)
    out = server.ccv2portal_create_deployment("b1", "d1")
    assert out.code == "dep9" and seen == {"buildCode": "b1", "environmentCode": "d1", "databaseUpdateMode": "NONE", "strategy": "ROLLING_UPDATE"}


def test_create_deployment_refuses_failed_build(configured):
    _ok_build(configured, status="FAIL")
    with pytest.raises(PortalError, match="only SUCCESS builds"):
        server.ccv2portal_create_deployment("b1", "d1")


def test_create_deployment_refuses_running(configured):
    _ok_build(configured)
    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments", {"value": [{"code": "depX", "status": "DEPLOYING", "environmentCode": "d1"}], "count": 1})
    with pytest.raises(PortalError, match="already has deployment depX"):
        server.ccv2portal_create_deployment("b1", "d1")


def test_create_deployment_validates_modes(configured):
    _ok_build(configured)
    _no_running(configured)
    _modes(configured, db=("NONE", "UPDATE"))
    # hard validation comes before the confirmation prompt
    with pytest.raises(PortalError, match="does not allow database_update_mode INITIALIZE"):
        server.ccv2portal_create_deployment("b1", "d1", database_update_mode="INITIALIZE", strategy="RECREATE")
    with pytest.raises(ConfirmationRequired, match="RECREATE causes downtime"):
        server.ccv2portal_create_deployment("b1", "d1", database_update_mode="UPDATE", strategy="RECREATE")


def test_create_deployment_prod_requires_permission_and_confirm(configured, client):
    _ok_build(configured)
    _no_running(configured)
    _modes(configured, env="p1")
    with pytest.raises(PermissionDenied, match="deploy"):
        server.ccv2portal_create_deployment("b1", "p1")
    client.sub.permissions["p1"] = {"read", "deploy"}
    with pytest.raises(PermissionDenied, match="deploy_prod"):
        server.ccv2portal_create_deployment("b1", "p1")
    client.sub.permissions["p1"] = {"read", "deploy", "deploy_prod"}
    with pytest.raises(ConfirmationRequired, match="production"):
        server.ccv2portal_create_deployment("b1", "p1")
    configured.route("POST", f"/v2/subscriptions/{SUB}/deployments", {"code": "depP"}, status=201)
    assert server.ccv2portal_create_deployment("b1", "p1", confirm=True).code == "depP"


def test_audit_records_writes_and_failures(configured, tmp_path, client):
    path = tmp_path / "audit.jsonl"
    server.registry.configure(server.registry.settings, server.registry.config, Audit(str(path)))
    server.registry.set_client("acme", client)
    _ok_build(configured, status="FAIL")
    with pytest.raises(PortalError):
        server.ccv2portal_create_deployment("b1", "d1")
    configured.route("PUT", f"/v2/subscriptions/{SUB}/environments/d1/services/hcs_common/properties/db.password", {"key": "db.password", "value": "hunter2"})
    configured.route("GET", f"/v2/subscriptions/{SUB}/environments/d1/services/hcs_common/properties/db.password", {}, status=404)
    client.sub.permissions["d1"].add("properties_write")
    server.ccv2portal_set_property("d1", "db.password", "hunter2")
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert lines[0]["tool"] == "ccv2portal_create_deployment" and lines[0]["error"]
    assert lines[0]["environment"] == "d1" and lines[0]["args"]["build_code"] == "b1"  # positional args captured
    assert lines[1]["tool"] == "ccv2portal_set_property" and lines[1]["error"] is None
    assert "hunter2" not in path.read_text()  # value masked because the property key looks secret


def test_cancel_deployment_guards(configured):
    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments/dep1", {"code": "dep1", "environmentCode": "d1", "status": "DEPLOYING"})
    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments/dep1/cancellationoptions", {}, status=501)
    with pytest.raises(PortalError, match="switched on"):
        server.ccv2portal_cancel_deployment("dep1", "wrong build", confirm=True)
    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments/dep1/cancellationoptions", {"databaseRollbackRestoreTimestamp": "2026-10-28T10:00:00Z"})
    with pytest.raises(ConfirmationRequired):
        server.ccv2portal_cancel_deployment("dep1", "wrong build")
    configured.route("POST", f"/v2/subscriptions/{SUB}/deployments/dep1/cancellation", {}, status=202)
    out = server.ccv2portal_cancel_deployment("dep1", "wrong build", rollback_database=True, confirm=True)
    assert out.executed and out.response["options"]["databaseRollbackRestoreTimestamp"]


def test_blue_green_decision_only_green(configured):
    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments/dep1", {"code": "dep1", "environmentCode": "d1", "strategy": "ROLLING_UPDATE"})
    with pytest.raises(PortalError, match="GREEN deployments only"):
        server.ccv2portal_blue_green_decision("dep1", "accept")
    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments/dep1", {"code": "dep1", "environmentCode": "d1", "strategy": "GREEN"})
    configured.route("POST", f"/v2/subscriptions/{SUB}/deployments/dep1/decisions", {"decision": "ACCEPT", "newDeploymentCode": "dep2"})
    out = server.ccv2portal_blue_green_decision("dep1", "accept", reason="looks good")
    assert out.action == "blue_green_accept" and "dep2" in out.hint


def test_set_property_records_previous(configured, client):
    client.sub.permissions["d1"].add("properties_write")
    configured.route("GET", f"/v2/subscriptions/{SUB}/environments/d1/services/hcs_common/properties/foo", {"key": "foo", "value": "old"})
    configured.route("PUT", f"/v2/subscriptions/{SUB}/environments/d1/services/hcs_common/properties/foo", {"key": "foo", "value": "new"})
    out = server.ccv2portal_set_property("d1", "foo", "new")
    assert out.response["previous"] == "old" and "apply" in out.hint


def test_backup_create_and_restore_cross_env(configured, client):
    client.sub.permissions["s1"] = {"read", "backup_restore"}
    client.sub.permissions["p1"] = {"read", "backup_restore"}
    configured.route("POST", f"/v2/subscriptions/{SUB}/environments/p1/databackups", {"databackupCode": "bk1"}, status=201)
    out = server.ccv2portal_create_backup("p1", "before release")
    assert out.code == "bk1"
    with pytest.raises(Exception, match="QUICK backups cover the database only"):
        server.ccv2portal_create_backup("p1", "x", include_storages=True)
    configured.route("GET", f"/v2/subscriptions/{SUB}/environments/p1/databackups", {"value": [{"databackupCode": "bk1", "canBeRestored": True, "status": "SUCCESS"}], "count": 1})
    with pytest.raises(ConfirmationRequired, match="replaces the target"):
        server.ccv2portal_restore_backup("s1", "bk1", source_environment="p1")
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return (201, {"datarestoreCode": "rs1"})

    configured.route("POST", f"/v2/subscriptions/{SUB}/environments/s1/datarestores", handler=handler)
    out = server.ccv2portal_restore_backup("s1", "bk1", source_environment="p1", confirm=True)
    assert out.code == "rs1" and seen == {"databackupCode": "bk1", "sourceEnvironmentCode": "p1"}


def test_restore_into_prod_needs_deploy_prod(configured, client):
    client.sub.permissions["p1"] = {"read", "backup_restore"}
    with pytest.raises(PermissionDenied, match="deploy_prod"):
        server.ccv2portal_restore_backup("p1", "bk1", confirm=True)


def test_update_scaling_validates_and_confirms(configured, client):
    client.sub.permissions["d1"].add("scaling")
    with pytest.raises(Exception, match="changes must use only"):
        server.ccv2portal_update_scaling("d1", {"replicas": 3}, confirm=True)
    with pytest.raises(ConfirmationRequired):
        server.ccv2portal_update_scaling("d1", {"services": [{"code": "hcs_platform_api", "replicas": 3}]})
    configured.route("PATCH", f"/v2/subscriptions/{SUB}/environments/d1/scaling", {"services": [{"code": "hcs_platform_api", "replicas": 3}]})
    assert server.ccv2portal_update_scaling("d1", {"services": [{"code": "hcs_platform_api", "replicas": 3}]}, confirm=True).executed


def test_schedule_activity(configured, client):
    client.sub.permissions["d1"].add("scaling")
    with pytest.raises(Exception, match="activity_type must be"):
        server.ccv2portal_schedule_activity("d1", "SLEEP", "2026-11-01T22:00:00Z")
    with pytest.raises(Exception, match="ISO 8601"):
        server.ccv2portal_schedule_activity("d1", "HIBERNATE_COMMERCE_ENVIRONMENT", "tomorrow")
    configured.route("POST", f"/v2/subscriptions/{SUB}/environments/d1/scheduledactivities", {"activityCode": "act1"}, status=201)
    out = server.ccv2portal_schedule_activity("d1", "hibernate_commerce_environment", "2026-11-01T22:00:00Z")
    assert out.code == "act1"


@pytest.mark.anyio
async def test_write_tools_annotated(configured):
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert not tools["ccv2portal_create_deployment"].annotations.read_only_hint
    assert tools["ccv2portal_create_deployment"].annotations.destructive_hint
    assert tools["ccv2portal_restore_backup"].annotations.destructive_hint
    assert tools["ccv2portal_get_property"].annotations.read_only_hint
