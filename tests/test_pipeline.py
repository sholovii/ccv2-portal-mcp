"""The build -> backup -> deploy pipeline against the fake portal."""

from __future__ import annotations

import json

import pytest

from ccv2_portal_mcp import pipeline, server
from ccv2_portal_mcp.app import ConfirmationRequired, PermissionDenied
from tests.conftest import SUB


@pytest.fixture(autouse=True)
def fresh_store():
    """The pipeline store is process-global; start every test with an empty one."""
    pipeline.store._items.clear()
    pipeline.store._threads.clear()
    yield


def _progression(statuses):
    """Handler whose answer advances on every call and sticks at the last one."""
    calls = {"n": 0}

    def handler(request):
        i = min(calls["n"], len(statuses) - 1)
        calls["n"] += 1
        return 200, statuses[i]

    return handler


def _happy_portal(portal, *, build_ok=True, with_backup=False):
    posted = {"builds": [], "deployments": [], "backups": []}
    portal.route("POST", f"/v2/subscriptions/{SUB}/builds", handler=lambda r: posted["builds"].append(json.loads(r.content)) or (201, {"code": "b9"}))
    final = {"buildStatus": "SUCCESS" if build_ok else "FAIL", "percentage": 100, "errorMessage": None if build_ok else "Build failed"}
    portal.route("GET", f"/v2/subscriptions/{SUB}/builds/b9/progress", handler=_progression([{"buildStatus": "UNKNOWN"}, {"buildStatus": "BUILDING", "percentage": 40}, final]))
    portal.route("GET", f"/v2/subscriptions/{SUB}/builds/b9", {"code": "b9", "status": "SUCCESS" if build_ok else "FAIL", "branch": "develop"})
    portal.route("GET", f"/v2/subscriptions/{SUB}/builds/b9/logs", handler=lambda r: (404, {"title": "no logs"}))
    portal.route("GET", f"/v2/subscriptions/{SUB}/deployments", {"value": [], "count": 0})
    portal.route("POST", f"/v2/subscriptions/{SUB}/deployments", handler=lambda r: posted["deployments"].append(json.loads(r.content)) or (201, {"code": "dep9"}))
    portal.route("GET", f"/v2/subscriptions/{SUB}/deployments/dep9/progress", handler=_progression([{"deploymentStatus": "DEPLOYING", "percentage": 50}, {"deploymentStatus": "DEPLOYED", "percentage": 100}]))
    if with_backup:
        portal.route("POST", f"/v2/subscriptions/{SUB}/environments/d1/databackups", handler=lambda r: posted["backups"].append(json.loads(r.content)) or (201, {"databackupCode": "bk9"}))
        portal.route("GET", f"/v2/subscriptions/{SUB}/environments/d1/databackups/bk9", handler=_progression([{"status": "IN_PROGRESS", "canBeRestored": False}, {"status": "CREATED", "canBeRestored": True}]))
    return posted


def _finished(pid):
    pipeline.store.join(pid, timeout=10)
    return server.ccv2portal_get_pipeline(pid)


def test_pipeline_builds_backs_up_and_deploys(configured, client):
    client.sub.permissions["d1"].add("backup_restore")
    posted = _happy_portal(configured, with_backup=True)
    started = server.ccv2portal_build_and_deploy("develop", "d1", database_update_mode="UPDATE", backup_first=True, build_name="nightly")
    assert started.status == "running" and started.id.startswith("pl-")
    state = _finished(started.id)
    assert state.status == "succeeded" and state.progress == 100
    assert (state.build_code, state.backup_code, state.deployment_code) == ("b9", "bk9", "dep9")
    assert {s.name: s.status for s in state.steps} == {
        "build": "done", "wait_build": "done", "backup": "done", "wait_backup": "done",
        "wait_environment": "skipped", "deploy": "done", "wait_deployment": "done",
    }
    assert posted["builds"][0]["name"] == "nightly" and posted["deployments"][0]["databaseUpdateMode"] == "UPDATE"
    assert posted["backups"][0]["databackupType"] == "QUICK"
    assert "deployed to d1" in state.hint


def test_pipeline_without_backup_skips_those_steps(configured):
    _happy_portal(configured)
    state = _finished(server.ccv2portal_build_and_deploy("develop", "d1").id)
    assert state.status == "succeeded"
    assert {s.name: s.status for s in state.steps}["backup"] == "skipped"
    listed = server.ccv2portal_list_pipelines()
    assert listed.pipelines[0].id == state.id and listed.running == 0


def test_failed_build_stops_the_pipeline_with_a_diagnosis(configured):
    posted = _happy_portal(configured, build_ok=False)
    state = _finished(server.ccv2portal_build_and_deploy("develop", "d1").id)
    assert state.status == "failed" and "b9 finished with FAIL" in state.error
    by = {s.name: s for s in state.steps}
    assert by["wait_build"].status == "failed" and by["deploy"].status == "skipped"
    assert state.diagnosis and state.diagnosis["build_code"] == "b9"
    assert posted["deployments"] == []


def test_validation_happens_before_anything_starts(configured, client):
    posted = _happy_portal(configured)
    client.sub.permissions["p1"] = {"read", "deploy"}
    with pytest.raises(ConfirmationRequired, match="production"):
        server.ccv2portal_build_and_deploy("release/1", "p1")
    with pytest.raises(PermissionDenied, match="deploy"):
        server.ccv2portal_build_and_deploy("develop", "s1")  # s1 only has the default read permission
    with pytest.raises(PermissionDenied, match="backup_restore"):
        server.ccv2portal_build_and_deploy("develop", "d1", backup_first=True)
    with pytest.raises(Exception, match="strategy must be one of"):
        server.ccv2portal_build_and_deploy("develop", "d1", strategy="YOLO")
    assert posted["builds"] == [] and server.ccv2portal_list_pipelines().pipelines == []


def test_cancel_stops_before_the_next_step(configured):
    posted = _happy_portal(configured)
    holder = {}

    def building(request):
        if "id" in holder:
            server.ccv2portal_cancel_pipeline(holder["id"])
        return 200, {"buildStatus": "BUILDING", "percentage": 10}

    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b9/progress", handler=building)
    started = server.ccv2portal_build_and_deploy("develop", "d1")
    holder["id"] = started.id
    state = _finished(started.id)
    assert state.status == "cancelled"
    by = {s.name: s for s in state.steps}
    assert by["build"].status == "done" and by["wait_build"].status == "cancelled" and by["deploy"].status == "skipped"
    assert posted["deployments"] == []
    with pytest.raises(Exception, match="already cancelled"):
        server.ccv2portal_cancel_pipeline(started.id)


def test_wait_for_pipeline_reports_progress_and_returns_final_state(configured):
    from ccv2_portal_mcp import app

    _happy_portal(configured)
    started = server.ccv2portal_build_and_deploy("develop", "d1")
    seen = []
    token = app.PROGRESS.set(lambda p, t, m: seen.append((p, m)))
    try:
        state = server.ccv2portal_wait_for_pipeline(started.id, timeout_sec=5)
    finally:
        app.PROGRESS.reset(token)
    assert state.status == "succeeded"
    assert seen and seen[-1][0] == 100 and started.id in seen[-1][1]


def test_unknown_pipeline_explains_restarts():
    with pytest.raises(Exception, match="do not survive a restart"):
        server.ccv2portal_get_pipeline("pl-nope")


def test_status_page_lists_pipelines(configured):
    from ccv2_portal_mcp.status import StatusCollector

    _happy_portal(configured)
    pid = server.ccv2portal_build_and_deploy("develop", "d1").id
    _finished(pid)
    doc = StatusCollector(server.registry, served_by="ui").collect(live=False)
    assert doc["pipelines"][0]["id"] == pid and doc["pipelines"][0]["status"] == "succeeded"


def test_pipeline_waits_for_a_deployment_already_running_on_the_target(configured):
    posted = _happy_portal(configured)
    # the target is busy with dep0 for the first three checks, then free
    checks = {"n": 0}

    def deployments(request):
        checks["n"] += 1
        busy = checks["n"] <= 3
        items = [{"code": "dep0", "environmentCode": "d1", "buildCode": "b0", "status": "DEPLOYING" if busy else "DEPLOYED"}]
        if request.url.params.get("status") == "DEPLOYED" and busy:
            items = []
        return 200, {"value": items, "count": len(items)}

    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments", handler=deployments)
    configured.route("GET", f"/v2/subscriptions/{SUB}/deployments/dep0/progress", handler=_progression([{"deploymentStatus": "DEPLOYING", "percentage": 80}, {"deploymentStatus": "DEPLOYED", "percentage": 100}]))
    started = server.ccv2portal_build_and_deploy("develop", "d1")  # not refused although d1 is busy
    state = _finished(started.id)
    assert state.status == "succeeded", state.error
    by = {s.name: s for s in state.steps}
    assert by["wait_environment"].status == "done" and by["wait_environment"].code == "dep0"
    assert posted["deployments"][0]["buildCode"] == "b9"
