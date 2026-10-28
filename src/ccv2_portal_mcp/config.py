"""Runtime configuration.

Three ways to configure, all producing the same `AppConfig`:

1. Flat environment variables for a single subscription (the Docker default):
   CCV2_SUBSCRIPTION_CODE, CCV2_CLIENT_ID, CCV2_CLIENT_SECRET, CCV2_TOKEN_URL, CCV2_RESOURCE,
   CCV2_PERMISSIONS ("d1:read,build,deploy;s1:read,build;p1:read"),
   CCV2_DEFAULT_PERMISSIONS ("read").
2. Several subscriptions from environment variables: CCV2_SUBSCRIPTIONS="acme,globex" and,
   per name, ACME_SUBSCRIPTION_CODE, ACME_CLIENT_ID, ACME_CLIENT_SECRET, ACME_RESOURCE,
   ACME_PERMISSIONS, optionally ACME_TOKEN_URL / ACME_DEFAULT_PERMISSIONS / ACME_BASE_URL
   (falling back to the CCV2_* values). CCV2_DEFAULT_SUBSCRIPTION names the default.
3. A JSON file named by CCV2_CONFIG for several subscriptions. String values may contain
   `${ENV_VAR}` placeholders so secrets stay in the environment, not in the file.

Permissions are per environment code and default to read-only. An environment that is not
listed gets `default_permissions`. Production isolation is therefore a configuration act:
leave p1 at `read` unless a deploy from an agent is really wanted.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PERM_READ = "read"
PERM_BUILD = "build"
PERM_DEPLOY = "deploy"
PERM_DEPLOY_PROD = "deploy_prod"
PERM_PROPERTIES_WRITE = "properties_write"
PERM_ENDPOINTS_WRITE = "endpoints_write"
PERM_SCALING = "scaling"
PERM_BACKUP_RESTORE = "backup_restore"

ALL_PERMISSIONS = frozenset(
    {
        PERM_READ,
        PERM_BUILD,
        PERM_DEPLOY,
        PERM_DEPLOY_PROD,
        PERM_PROPERTIES_WRITE,
        PERM_ENDPOINTS_WRITE,
        PERM_SCALING,
        PERM_BACKUP_RESTORE,
    }
)

DEFAULT_BASE_URL = "https://portalapi.commerce.ondemand.com/v2"
LEGACY_HOST = "portalrotapi.hana.ondemand.com"

_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """Configuration is missing or inconsistent."""


def _validate_perms(perms: set[str], where: str) -> set[str]:
    unknown = perms - ALL_PERMISSIONS
    if unknown:
        raise ConfigError(
            f"{where}: unknown permission(s) {sorted(unknown)}; "
            f"allowed: {sorted(ALL_PERMISSIONS)}"
        )
    return perms


def parse_permissions(spec: str) -> dict[str, set[str]]:
    """Parse "d1:read,build;p1:read" into {"d1": {...}, "p1": {...}}."""
    result: dict[str, set[str]] = {}
    for chunk in filter(None, (c.strip() for c in spec.split(";"))):
        if ":" not in chunk:
            raise ConfigError(f"permission entry {chunk!r} must look like 'env:perm,perm'")
        env, _, perms = chunk.partition(":")
        env = env.strip()
        perm_set = {p.strip() for p in perms.split(",") if p.strip()}
        result[env] = _validate_perms(perm_set, f"permissions for {env}")
    return result


class SubscriptionConfig(BaseModel):
    name: str
    code: str
    client_id: str
    client_secret: str
    token_url: str
    resource: str
    base_url: str = DEFAULT_BASE_URL
    permissions: dict[str, set[str]] = Field(default_factory=dict)
    default_permissions: set[str] = Field(default_factory=lambda: {PERM_READ})

    @field_validator("base_url")
    @classmethod
    def _no_legacy_host(cls, v: str) -> str:
        if LEGACY_HOST in v:
            raise ConfigError(
                f"{LEGACY_HOST} is the retired host (it answers 302); use {DEFAULT_BASE_URL}"
            )
        return v.rstrip("/")

    @field_validator("permissions")
    @classmethod
    def _check_perms(cls, v: dict[str, set[str]]) -> dict[str, set[str]]:
        for env, perms in v.items():
            _validate_perms(set(perms), f"permissions for {env}")
        return v

    @field_validator("default_permissions")
    @classmethod
    def _check_default(cls, v: set[str]) -> set[str]:
        return _validate_perms(set(v), "default_permissions")

    def perms_for(self, env_code: str) -> set[str]:
        return set(self.permissions.get(env_code, self.default_permissions))

    def allows(self, env_code: str, perm: str) -> bool:
        return perm in self.perms_for(env_code)

    def redacted(self) -> dict:
        return {
            "name": self.name,
            "code": self.code,
            "client_id": self.client_id,
            "token_url": self.token_url,
            "base_url": self.base_url,
            "default_permissions": sorted(self.default_permissions),
            "permissions": {k: sorted(v) for k, v in self.permissions.items()},
        }


class AppConfig(BaseModel):
    subscriptions: dict[str, SubscriptionConfig]
    default_subscription: str | None = None

    def resolve(self, name: str | None) -> SubscriptionConfig:
        if name:
            try:
                return self.subscriptions[name]
            except KeyError:
                raise ConfigError(
                    f"unknown subscription {name!r}; configured: {sorted(self.subscriptions)}"
                ) from None
        if self.default_subscription:
            return self.subscriptions[self.default_subscription]
        if len(self.subscriptions) == 1:
            return next(iter(self.subscriptions.values()))
        raise ConfigError(
            "several subscriptions are configured and none is the default; "
            f"pass subscription= one of {sorted(self.subscriptions)}"
        )


class Settings(BaseSettings):
    """Process-level settings read from the environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Multi-subscription config file (JSON). When set, the other CCV2_* variables are ignored.
    ccv2_config: str | None = None

    # Multi-subscription from environment variables: comma-separated names, each read from
    # <NAME>_SUBSCRIPTION_CODE, <NAME>_CLIENT_ID, ... (see module docstring).
    ccv2_subscriptions: str = ""
    ccv2_default_subscription: str = ""

    # Single-subscription flat configuration.
    ccv2_subscription_name: str = "default"
    ccv2_subscription_code: str = ""
    ccv2_client_id: str = ""
    ccv2_client_secret: str = ""
    ccv2_token_url: str = ""
    ccv2_resource: str = ""
    ccv2_base_url: str = DEFAULT_BASE_URL
    ccv2_permissions: str = ""
    ccv2_default_permissions: str = PERM_READ

    # Transport: stdio for local runs, streamable-http for the container.
    mcp_transport: str = "stdio"
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8849

    # HTTP behaviour.
    http_timeout_sec: float = 30.0
    token_early_refresh_sec: int = 30
    retry_429_max: int = 3

    # Bounded waits: a wait_* tool never blocks longer than this per call.
    wait_max_sec: int = 60
    wait_poll_sec: int = 15

    # Build log handling: the zip is read fully but the returned text is capped.
    build_log_max_lines: int = 400

    # Where write operations are audited (JSONL). Empty = stderr only.
    audit_log_path: str = ""


def _expand(value):
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            name = m.group(1)
            if name not in os.environ:
                raise ConfigError(f"config placeholder ${{{name}}} is not set in the environment")
            return os.environ[name]

        return _PLACEHOLDER.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_config_file(path: str | Path) -> AppConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = _expand(raw)
    subs_raw = raw.get("subscriptions") or {}
    if not subs_raw:
        raise ConfigError(f"{path}: 'subscriptions' is empty")
    subs: dict[str, SubscriptionConfig] = {}
    for name, body in subs_raw.items():
        body = dict(body)
        body.setdefault("name", name)
        if isinstance(body.get("permissions"), dict):
            body["permissions"] = {k: set(v) for k, v in body["permissions"].items()}
        if isinstance(body.get("default_permissions"), list):
            body["default_permissions"] = set(body["default_permissions"])
        subs[name] = SubscriptionConfig(**body)
    default = raw.get("default_subscription")
    if default and default not in subs:
        raise ConfigError(f"{path}: default_subscription {default!r} is not a configured subscription")
    return AppConfig(subscriptions=subs, default_subscription=default)


def _env_prefix(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).upper()


def _perm_set(spec: str) -> set[str]:
    return _validate_perms({p.strip() for p in spec.split(",") if p.strip()}, "default_permissions")


def load_config_env_multi(settings: Settings) -> AppConfig:
    names = [n.strip() for n in settings.ccv2_subscriptions.split(",") if n.strip()]
    subs: dict[str, SubscriptionConfig] = {}
    for name in names:
        pre = _env_prefix(name)

        def get(key: str, fallback: str = "") -> str:
            return os.environ.get(f"{pre}_{key}", fallback)

        required = {k: get(k) for k in ("SUBSCRIPTION_CODE", "CLIENT_ID", "CLIENT_SECRET", "RESOURCE")}
        token_url = get("TOKEN_URL", settings.ccv2_token_url)
        missing = [f"{pre}_{k}" for k, v in required.items() if not v] + ([f"{pre}_TOKEN_URL (or CCV2_TOKEN_URL)"] if not token_url else [])
        if missing:
            raise ConfigError(f"subscription {name!r}: missing " + ", ".join(missing))
        subs[name] = SubscriptionConfig(
            name=name,
            code=required["SUBSCRIPTION_CODE"],
            client_id=required["CLIENT_ID"],
            client_secret=required["CLIENT_SECRET"],
            token_url=token_url,
            resource=required["RESOURCE"],
            base_url=get("BASE_URL", settings.ccv2_base_url),
            permissions=parse_permissions(get("PERMISSIONS", "")),
            default_permissions=_perm_set(get("DEFAULT_PERMISSIONS", settings.ccv2_default_permissions)),
        )
    default = settings.ccv2_default_subscription or (names[0] if len(names) == 1 else None)
    if default and default not in subs:
        raise ConfigError(f"CCV2_DEFAULT_SUBSCRIPTION {default!r} is not in CCV2_SUBSCRIPTIONS {names}")
    return AppConfig(subscriptions=subs, default_subscription=default)


def load_config(settings: Settings | None = None) -> AppConfig:
    settings = settings or Settings()
    if settings.ccv2_config:
        return load_config_file(settings.ccv2_config)
    if settings.ccv2_subscriptions.strip():
        return load_config_env_multi(settings)
    missing = [
        n
        for n, v in (
            ("CCV2_SUBSCRIPTION_CODE", settings.ccv2_subscription_code),
            ("CCV2_CLIENT_ID", settings.ccv2_client_id),
            ("CCV2_CLIENT_SECRET", settings.ccv2_client_secret),
            ("CCV2_TOKEN_URL", settings.ccv2_token_url),
            ("CCV2_RESOURCE", settings.ccv2_resource),
        )
        if not v
    ]
    if missing:
        raise ConfigError(
            "no configuration: set CCV2_CONFIG to a JSON file, CCV2_SUBSCRIPTIONS for several "
            "subscriptions from the environment, or the flat variables " + ", ".join(missing)
        )
    sub = SubscriptionConfig(
        name=settings.ccv2_subscription_name,
        code=settings.ccv2_subscription_code,
        client_id=settings.ccv2_client_id,
        client_secret=settings.ccv2_client_secret,
        token_url=settings.ccv2_token_url,
        resource=settings.ccv2_resource,
        base_url=settings.ccv2_base_url,
        permissions=parse_permissions(settings.ccv2_permissions),
        default_permissions={
            p.strip() for p in settings.ccv2_default_permissions.split(",") if p.strip()
        },
    )
    return AppConfig(subscriptions={sub.name: sub}, default_subscription=sub.name)
