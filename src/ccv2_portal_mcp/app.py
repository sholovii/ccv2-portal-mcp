"""Shared server core: the MCPServer instance, the per-subscription registry, permission
guards, the audit hook and small normalisers used by every tool module."""

from __future__ import annotations

import functools
import inspect
import logging
import threading
import time
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from .audit import Audit
from .auth import AuthError
from .client import PortalClient, PortalError
from .config import (
    PERM_READ,
    AppConfig,
    ConfigError,
    Settings,
    SubscriptionConfig,
    load_config,
)
from .models import BuildInfo, DeploymentInfo, DeploymentProgress, EnvironmentInfo, StageInfo

log = logging.getLogger("ccv2_portal_mcp")

mcp = MCPServer(
    "ccv2-portal",
    version=__version__,
    instructions=(
        "SAP Commerce Cloud (CCv2) Cloud Portal: environments, builds, deployments, build logs, "
        "properties, endpoints, backups, scaling and hibernation. Start with "
        "ccv2portal_list_subscriptions when unsure which subscription or environment to use. "
        "Write tools are gated by per-environment permissions and require confirm=true for "
        "production, INITIALIZE, RECREATE and restores."
    ),
)

BUILD_TERMINAL = {"SUCCESS", "FAIL", "DELETED"}
DEPLOYMENT_TERMINAL = {"DEPLOYED", "FAIL", "UNDEPLOYED"}
DEPLOYMENT_ACTIVE = {"SCHEDULED", "DEPLOYING"}
PRODUCTION_TYPES = {"production", "prod"}

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)


class PermissionDenied(PermissionError):
    pass


class ConfirmationRequired(RuntimeError):
    pass


class Registry:
    """Lazy per-subscription clients plus a short-lived environment cache."""

    ENV_TTL_SEC = 60

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._settings: Settings | None = None
        self._config: AppConfig | None = None
        self._clients: dict[str, PortalClient] = {}
        self._audit: Audit | None = None
        self._envs: dict[str, tuple[float, dict[str, dict]]] = {}

    @property
    def settings(self) -> Settings:
        with self._lock:
            if self._settings is None:
                self._settings = Settings()
            return self._settings

    @property
    def config(self) -> AppConfig:
        with self._lock:
            if self._config is None:
                self._config = load_config(self.settings)
            return self._config

    @property
    def audit(self) -> Audit:
        with self._lock:
            if self._audit is None:
                self._audit = Audit(self.settings.audit_log_path or None)
            return self._audit

    def configure(self, settings: Settings, config: AppConfig, audit: Audit | None = None) -> None:
        with self._lock:
            self._settings = settings
            self._config = config
            self._audit = audit
            self._clients.clear()
            self._envs.clear()

    def client(self, subscription: str | None) -> PortalClient:
        sub = self.config.resolve(subscription)
        with self._lock:
            c = self._clients.get(sub.name)
            if c is None:
                s = self.settings
                c = PortalClient(
                    sub,
                    timeout_sec=s.http_timeout_sec,
                    early_refresh_sec=s.token_early_refresh_sec,
                    retry_429_max=s.retry_429_max,
                )
                self._clients[sub.name] = c
            return c

    def set_client(self, name: str, client: PortalClient) -> None:
        with self._lock:
            self._clients[name] = client
            self._envs.pop(name, None)

    def environments(self, client: PortalClient, *, fresh: bool = False) -> dict[str, dict]:
        """Environment code -> raw environment, cached briefly (type lookups for guards)."""
        with self._lock:
            hit = self._envs.get(client.sub.name)
            if hit and not fresh and time.monotonic() - hit[0] < self.ENV_TTL_SEC:
                return hit[1]
        envs = {e["code"]: e for e in client.environments()}
        with self._lock:
            self._envs[client.sub.name] = (time.monotonic(), envs)
        return envs


registry = Registry()


# ------------------------------------------------------------------- guards

def require(sub: SubscriptionConfig, env: str, perm: str) -> None:
    if not sub.allows(env, perm):
        raise PermissionDenied(
            f"subscription {sub.name!r}: environment {env!r} does not grant {perm!r} "
            f"(granted: {sorted(sub.perms_for(env))}). Adjust the permissions configuration."
        )


def require_any(sub: SubscriptionConfig, perm: str) -> None:
    """For subscription-level operations (builds): any environment granting `perm` suffices."""
    envs = set(sub.permissions) | {"*"}
    if not any(perm in sub.perms_for(e) for e in envs):
        raise PermissionDenied(
            f"subscription {sub.name!r}: no environment grants {perm!r}; nothing to do."
        )


def is_production(env_raw: dict | None, env_code: str) -> bool:
    if env_raw and (env_raw.get("type") or "").lower() in PRODUCTION_TYPES:
        return True
    return env_code.lower().startswith("p")  # p1/p2 naming when type is missing


def env_or_fail(client: PortalClient, env: str) -> dict:
    envs = registry.environments(client)
    if env not in envs:
        raise PortalError(f"environment {env!r} not found; known: {sorted(envs)}")
    return envs[env]


# --------------------------------------------------------------------- tool

def tool(**kwargs) -> Callable:
    """Register a sync function as a tool; domain errors become readable tool errors."""

    def decorate(fn):
        @functools.wraps(fn)
        def run(*args, **kw):
            try:
                return fn(*args, **kw)
            except (PortalError, AuthError, ConfigError, PermissionDenied, ConfirmationRequired) as exc:
                raise RuntimeError(str(exc)) from exc

        mcp.tool(**kwargs)(run)
        return fn

    return decorate


def audited(tool_name: str, *, env_arg: str | None = "environment"):
    """Record every call of a write tool, including failures."""

    def decorate(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def run(*args, **kw):
            bound = sig.bind_partial(*args, **kw)
            bound.apply_defaults()
            call_args = {k: v for k, v in bound.arguments.items() if k != "subscription"}
            client = registry.client(bound.arguments.get("subscription"))
            env = call_args.get(env_arg) if env_arg else None
            try:
                result = fn(*args, **kw)
            except Exception as exc:
                registry.audit.record(subscription=client.sub.name, tool=tool_name, environment=env, args=call_args, error=str(exc))
                raise
            payload = result.model_dump() if hasattr(result, "model_dump") else result
            registry.audit.record(subscription=client.sub.name, tool=tool_name, environment=env, args=call_args, result=payload)
            return result

        return run

    return decorate


# --------------------------------------------------------------- normalisers

def env_info(sub: SubscriptionConfig, raw: dict) -> EnvironmentInfo:
    info = EnvironmentInfo(**raw)
    info.permissions = sorted(sub.perms_for(info.code))
    return info


def build_info(raw: dict) -> BuildInfo:
    return BuildInfo(**raw)


def deployment_info(raw: dict) -> DeploymentInfo:
    return DeploymentInfo(**raw)


def deployment_progress(raw: dict) -> DeploymentProgress:
    stages = []
    for st in raw.get("stages") or []:
        failed = []
        for step in st.get("steps") or []:
            if (step.get("status") or "").upper() == "FAIL":
                failed.append(f"{step.get('name') or step.get('code')}: {step.get('message') or ''}".strip())
        stages.append(StageInfo(**{k: v for k, v in st.items() if k != "steps"}, failed_steps=failed))
    return DeploymentProgress(**{k: v for k, v in raw.items() if k != "stages"}, stages=stages)


def status_of(raw: dict) -> str | None:
    s = raw.get("buildStatus") or raw.get("deploymentStatus") or raw.get("status") or ""
    return s.upper() or None


def latest_deployed(client: PortalClient, env: str, *, limit: int = 20) -> dict | None:
    for d in client.deployments(environment=env, status="DEPLOYED", limit=limit):
        return d
    return None


def active_deployment(client: PortalClient, env: str) -> dict | None:
    for d in client.deployments(environment=env, limit=5):
        if status_of(d) in DEPLOYMENT_ACTIVE:
            return d
    return None


def safe(fn: Callable[[], Any]) -> tuple[Any, str | None]:
    """Run a portal read; return (value, None) or (None, error text)."""
    try:
        return fn(), None
    except (PortalError, AuthError) as exc:
        return None, str(exc)
