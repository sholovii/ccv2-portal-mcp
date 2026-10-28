import json

import pytest

from ccv2_portal_mcp.config import (
    AppConfig,
    ConfigError,
    Settings,
    SubscriptionConfig,
    load_config,
    load_config_file,
    parse_permissions,
)


def test_parse_permissions():
    perms = parse_permissions("d1:read,build;p1:read")
    assert perms == {"d1": {"read", "build"}, "p1": {"read"}}


def test_parse_permissions_rejects_unknown():
    with pytest.raises(ConfigError, match="unknown permission"):
        parse_permissions("d1:read,fly")


def test_legacy_host_refused():
    with pytest.raises((ConfigError, ValueError), match="retired host"):
        SubscriptionConfig(
            name="x", code="c", client_id="i", client_secret="s", token_url="t", resource="r",
            base_url="https://portalrotapi.hana.ondemand.com/v2",
        )


def test_flat_env_config(monkeypatch):
    for k, v in {
        "CCV2_SUBSCRIPTION_CODE": "abc",
        "CCV2_CLIENT_ID": "id",
        "CCV2_CLIENT_SECRET": "sec",
        "CCV2_TOKEN_URL": "https://ias/token",
        "CCV2_RESOURCE": "res",
        "CCV2_PERMISSIONS": "d1:read,build;p1:read",
    }.items():
        monkeypatch.setenv(k, v)
    cfg = load_config(Settings(_env_file=None))
    sub = cfg.resolve(None)
    assert sub.code == "abc"
    assert sub.allows("d1", "build") and not sub.allows("p1", "build")
    assert sub.perms_for("s1") == {"read"}  # default


def test_flat_env_missing(monkeypatch):
    for k in ("CCV2_SUBSCRIPTION_CODE", "CCV2_CLIENT_ID", "CCV2_CLIENT_SECRET", "CCV2_TOKEN_URL", "CCV2_RESOURCE", "CCV2_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ConfigError, match="CCV2_CLIENT_ID"):
        load_config(Settings(_env_file=None))


def test_json_config_with_placeholders(tmp_path, monkeypatch):
    monkeypatch.setenv("ACME_SECRET", "s3cr3t")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "default_subscription": "acme",
        "subscriptions": {
            "acme": {"code": "c1", "client_id": "i", "client_secret": "${ACME_SECRET}", "token_url": "t", "resource": "r",
                      "permissions": {"d1": ["read", "deploy"]}},
            "other": {"code": "c2", "client_id": "i", "client_secret": "x", "token_url": "t", "resource": "r"},
        },
    }))
    cfg = load_config_file(path)
    assert cfg.resolve(None).client_secret == "s3cr3t"
    assert cfg.resolve("other").code == "c2"
    assert "s3cr3t" not in json.dumps(cfg.resolve("acme").redacted())


def test_json_config_unset_placeholder(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"subscriptions": {"a": {"code": "c", "client_id": "i", "client_secret": "${NOPE}", "token_url": "t", "resource": "r"}}}))
    with pytest.raises(ConfigError, match="NOPE"):
        load_config_file(path)


def test_resolve_ambiguous():
    subs = {n: SubscriptionConfig(name=n, code=n, client_id="i", client_secret="s", token_url="t", resource="r") for n in ("a", "b")}
    cfg = AppConfig(subscriptions=subs)
    with pytest.raises(ConfigError, match="none is the default"):
        cfg.resolve(None)
    assert cfg.resolve("b").code == "b"


def _clear(monkeypatch):
    for k in ("CCV2_CONFIG", "CCV2_SUBSCRIPTIONS", "CCV2_DEFAULT_SUBSCRIPTION", "CCV2_TOKEN_URL", "CCV2_SUBSCRIPTION_CODE"):
        monkeypatch.delenv(k, raising=False)


def test_env_multi_subscriptions(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CCV2_SUBSCRIPTIONS", "acme, globex-eu")
    monkeypatch.setenv("CCV2_DEFAULT_SUBSCRIPTION", "globex-eu")
    monkeypatch.setenv("CCV2_TOKEN_URL", "https://ias/token")
    for pre in ("ACME", "GLOBEX_EU"):
        monkeypatch.setenv(f"{pre}_SUBSCRIPTION_CODE", f"code-{pre}")
        monkeypatch.setenv(f"{pre}_CLIENT_ID", "id")
        monkeypatch.setenv(f"{pre}_CLIENT_SECRET", "sec")
        monkeypatch.setenv(f"{pre}_RESOURCE", "res")
    monkeypatch.setenv("ACME_PERMISSIONS", "d1:read,deploy")
    monkeypatch.setenv("GLOBEX_EU_TOKEN_URL", "https://other-ias/token")
    monkeypatch.setenv("GLOBEX_EU_DEFAULT_PERMISSIONS", "")
    cfg = load_config(Settings(_env_file=None))
    assert sorted(cfg.subscriptions) == ["acme", "globex-eu"]
    assert cfg.resolve(None).code == "code-GLOBEX_EU"
    assert cfg.resolve("acme").allows("d1", "deploy") and cfg.resolve("acme").perms_for("p1") == {"read"}
    assert cfg.resolve("globex-eu").token_url == "https://other-ias/token"
    assert cfg.resolve("globex-eu").perms_for("d1") == set()  # hidden unless listed


def test_env_multi_missing_and_bad_default(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CCV2_SUBSCRIPTIONS", "acme")
    monkeypatch.setenv("ACME_SUBSCRIPTION_CODE", "c")
    with pytest.raises(ConfigError, match="ACME_CLIENT_ID"):
        load_config(Settings(_env_file=None))
    for k in ("CLIENT_ID", "CLIENT_SECRET", "RESOURCE", "TOKEN_URL"):
        monkeypatch.setenv(f"ACME_{k}", "x")
    assert load_config(Settings(_env_file=None)).default_subscription == "acme"  # single name is the default
    monkeypatch.setenv("CCV2_DEFAULT_SUBSCRIPTION", "nope")
    with pytest.raises(ConfigError, match="CCV2_DEFAULT_SUBSCRIPTION"):
        load_config(Settings(_env_file=None))
