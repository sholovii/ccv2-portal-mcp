import json

import httpx
import pytest

from ccv2_portal_mcp import server
from ccv2_portal_mcp.app import ConfirmationRequired
from ccv2_portal_mcp.client import PortalError
from tests.conftest import SUB

CERT = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"
KEY = "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----"


def _endpoints(portal, env, items):
    portal.route("GET", f"/v2/subscriptions/{SUB}/environments/{env}/endpoints", {"value": items, "count": len(items)})
    for e in items:
        portal.route("GET", f"/v2/subscriptions/{SUB}/environments/{env}/endpoints/{e['code']}", e)


# ------------------------------------------------------------ maintenance

def test_maintenance_mode_bulk(configured, client):
    client.sub.permissions["d1"].add("endpoints_write")
    _endpoints(configured, "d1", [
        {"code": "web", "maintenanceMode": False, "webProxy": "public"},
        {"code": "api", "maintenanceMode": False, "webProxy": "public"},
        {"code": "bo", "maintenanceMode": True, "webProxy": "public"},
    ])
    seen = {}

    def bulk(request):
        assert request.headers["content-type"] == "application/merge-patch+json"
        seen["items"] = json.loads(request.content)
        return (200, {"value": seen["items"]})

    configured.route("PATCH", f"/v2/subscriptions/{SUB}/environments/d1/endpoints", handler=bulk)
    out = server.ccv2portal_set_maintenance_mode("d1", True)
    assert out.executed and sorted(out.response["changed"]) == ["api", "web"] and out.response["unchanged"] == ["bo"]
    assert seen["items"] == [{"code": "web", "maintenanceMode": True}, {"code": "api", "maintenanceMode": True}]
    assert out.response["method"] == "bulk"


def test_maintenance_mode_fallback_and_noop(configured, client):
    client.sub.permissions["d1"].add("endpoints_write")
    _endpoints(configured, "d1", [{"code": "web", "maintenanceMode": False}])
    configured.route("PATCH", f"/v2/subscriptions/{SUB}/environments/d1/endpoints", {"title": "Not Found"}, status=404)
    configured.route("PATCH", f"/v2/subscriptions/{SUB}/environments/d1/endpoints/web", {"code": "web", "maintenanceMode": True})
    out = server.ccv2portal_set_maintenance_mode("d1", True, endpoints=["web"])
    assert out.executed and out.response["method"] == "per-endpoint" and out.response["changed"] == ["web"]
    # already off -> nothing to do, no confirm needed even on prod
    _endpoints(configured, "p1", [{"code": "web", "maintenanceMode": False}])
    client.sub.permissions["p1"] = {"read", "endpoints_write"}
    assert not server.ccv2portal_set_maintenance_mode("p1", False).executed


def test_maintenance_mode_prod_confirm_and_unknown(configured, client):
    client.sub.permissions["p1"] = {"read", "endpoints_write"}
    _endpoints(configured, "p1", [{"code": "web", "maintenanceMode": False}])
    with pytest.raises(ConfirmationRequired, match="production"):
        server.ccv2portal_set_maintenance_mode("p1", True)
    with pytest.raises(PortalError, match="unknown endpoint"):
        server.ccv2portal_set_maintenance_mode("p1", True, endpoints=["nope"], confirm=True)


# --------------------------------------------------------- backup first

def _deployable(portal):
    portal.route("GET", f"/v2/subscriptions/{SUB}/builds/b1", {"code": "b1", "status": "SUCCESS", "branch": "develop"})
    portal.route("GET", f"/v2/subscriptions/{SUB}/deployments", {"value": [], "count": 0})
    portal.route("GET", f"/v2/subscriptions/{SUB}/deploymentmodes", {"value": []})
    portal.route("POST", f"/v2/subscriptions/{SUB}/deployments", {"code": "dep1"}, status=201)


def test_backup_first_waits_then_deploys(configured, client):
    client.sub.permissions["d1"].add("backup_restore")
    _deployable(configured)
    polls = {"n": 0}
    configured.route("POST", f"/v2/subscriptions/{SUB}/environments/d1/databackups", {"databackupCode": "bk1"}, status=201)

    def backup(request):
        polls["n"] += 1
        return (200, {"databackupCode": "bk1", "status": "IN_PROGRESS" if polls["n"] < 2 else "SUCCESS", "canBeRestored": polls["n"] >= 2})

    configured.route("GET", f"/v2/subscriptions/{SUB}/environments/d1/databackups/bk1", handler=backup)
    out = server.ccv2portal_create_deployment("b1", "d1", backup_first=True)
    assert out.executed and out.code == "dep1" and "bk1 completed" in out.hint


def test_backup_first_still_running_does_not_deploy(configured, client, monkeypatch):
    client.sub.permissions["d1"].add("backup_restore")
    _deployable(configured)
    configured.route("POST", f"/v2/subscriptions/{SUB}/environments/d1/databackups", {"databackupCode": "bk1"}, status=201)
    configured.route("GET", f"/v2/subscriptions/{SUB}/environments/d1/databackups/bk1", {"databackupCode": "bk1", "status": "IN_PROGRESS"})
    posted = []
    configured.route("POST", f"/v2/subscriptions/{SUB}/deployments", handler=lambda r: posted.append(1) or (201, {"code": "dep1"}))
    out = server.ccv2portal_create_deployment("b1", "d1", backup_first=True)
    assert not out.executed and out.response["backup_code"] == "bk1" and "wait_for_backup" in out.hint
    assert posted == []
    wait = server.ccv2portal_wait_for_backup("d1", "bk1", timeout_sec=3)
    assert not wait.done and wait.kind == "backup"


def test_backup_first_failure_aborts(configured, client):
    client.sub.permissions["d1"].add("backup_restore")
    _deployable(configured)
    configured.route("POST", f"/v2/subscriptions/{SUB}/environments/d1/databackups", {"databackupCode": "bk1"}, status=201)
    configured.route("GET", f"/v2/subscriptions/{SUB}/environments/d1/databackups/bk1", {"databackupCode": "bk1", "status": "FAILED"})
    with pytest.raises(PortalError, match="backup bk1 failed"):
        server.ccv2portal_create_deployment("b1", "d1", backup_first=True)


def test_backup_first_needs_permission(configured, client):
    _deployable(configured)
    with pytest.raises(Exception, match="backup_restore"):
        server.ccv2portal_create_deployment("b1", "d1", backup_first=True)


# ---------------------------------------------------------- certificates

def test_upload_certificate_validates_pem_and_masks_key(configured, client, tmp_path):
    from ccv2_portal_mcp.audit import Audit
    path = tmp_path / "audit.jsonl"
    server.registry.configure(server.registry.settings, server.registry.config, Audit(str(path)))
    server.registry.set_client("acme", client)
    client.sub.permissions["d1"].add("endpoints_write")
    with pytest.raises(Exception, match="PEM"):
        server.ccv2portal_upload_certificate("x", "not pem", KEY)
    seen = {}

    def create(request):
        seen.update(json.loads(request.content))
        return (201, {"code": "cert2", "name": "x", "domains": ["*.example.com"], "expirationDate": "2027-10-01T00:00:00Z"})

    configured.route("POST", f"/v2/subscriptions/{SUB}/certificates", handler=create)
    out = server.ccv2portal_upload_certificate("x", CERT, KEY, ca_certificate_pem=CERT)
    assert out.code == "cert2" and seen["certificateKey"] == KEY and "caCertificate" in seen
    assert "MIIE" not in path.read_text()  # private key masked in the audit


def test_rotate_certificate_domain_check_and_delete_old(configured, client):
    client.sub.permissions["d1"].add("endpoints_write")
    configured.route("GET", f"/v2/subscriptions/{SUB}/certificates/cert2", {"code": "cert2", "domains": ["*.example.com"], "expirationDate": "2027-10-01T00:00:00Z"})
    _endpoints(configured, "d1", [
        {"code": "web", "protocol": "HTTPS", "domainName": "www.example.com", "sslCertificate": {"code": "cert1"}},
        {"code": "api", "protocol": "HTTPS", "domainName": "api.example.com", "sslCertificate": "cert1"},
        {"code": "old", "protocol": "HTTPS", "domainName": "old.other.com", "sslCertificate": "cert0"},
        {"code": "plain", "protocol": "HTTP", "domainName": "plain.example.com"},
    ])
    # default selection would include 'old' whose domain does not match -> refuse
    with pytest.raises(PortalError, match="does not match endpoint domain"):
        server.ccv2portal_rotate_certificate("d1", "cert2")
    patched = {}

    def patch(code):
        def h(request):
            patched[code] = json.loads(request.content)
            return (200, {"code": code})
        return h

    for c in ("web", "api"):
        configured.route("PATCH", f"/v2/subscriptions/{SUB}/environments/d1/endpoints/{c}", handler=patch(c))
    configured.route("GET", f"/v2/subscriptions/{SUB}/certificates/cert1", {"code": "cert1", "endpoints": []})
    deleted = []
    configured.route("DELETE", f"/v2/subscriptions/{SUB}/certificates/cert1", handler=lambda r: deleted.append(1) or httpx.Response(204))
    out = server.ccv2portal_rotate_certificate("d1", "cert2", old_certificate_code="cert1", delete_old=True)
    assert sorted(out.response["changed"]) == ["api", "web"] and patched["web"] == {"sslCertificate": "cert2"}
    assert out.response["deleted_certificates"] == ["cert1"] and deleted == [1]


def test_rotate_certificate_prod_confirm_and_noop(configured, client):
    client.sub.permissions["p1"] = {"read", "endpoints_write"}
    configured.route("GET", f"/v2/subscriptions/{SUB}/certificates/cert2", {"code": "cert2", "domains": ["www.example.com"]})
    _endpoints(configured, "p1", [{"code": "web", "protocol": "HTTPS", "domainName": "www.example.com", "sslCertificate": "cert1"}])
    with pytest.raises(ConfirmationRequired, match="production"):
        server.ccv2portal_rotate_certificate("p1", "cert2")
    _endpoints(configured, "p1", [{"code": "web", "protocol": "HTTPS", "domainName": "www.example.com", "sslCertificate": "cert2"}])
    assert not server.ccv2portal_rotate_certificate("p1", "cert2", confirm=True).executed


def test_delete_certificate_refuses_in_use(configured, client):
    client.sub.permissions["d1"].add("endpoints_write")
    configured.route("GET", f"/v2/subscriptions/{SUB}/certificates/cert1", {"code": "cert1", "endpoints": [{"code": "web"}]})
    with pytest.raises(PortalError, match="still used"):
        server.ccv2portal_delete_certificate("cert1", confirm=True)
    configured.route("GET", f"/v2/subscriptions/{SUB}/certificates/cert1", {"code": "cert1", "endpoints": []})
    with pytest.raises(ConfirmationRequired):
        server.ccv2portal_delete_certificate("cert1")
    configured.route("DELETE", f"/v2/subscriptions/{SUB}/certificates/cert1", handler=lambda r: httpx.Response(204))
    assert server.ccv2portal_delete_certificate("cert1", confirm=True).executed
