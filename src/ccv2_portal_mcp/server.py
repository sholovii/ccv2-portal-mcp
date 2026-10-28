"""MCP server entry point for the CCv2 Cloud Portal (MCP Python SDK 2.x, MCPServer).

Tool names carry the `ccv2portal_` prefix so they do not clash with other servers' tools. Tools are plain sync functions; the SDK runs them in a worker thread, so one
slow portal call never blocks other sessions.

Phase 1 scope: read-only tools plus bounded waits. Write tools come with the audit log.
"""

from __future__ import annotations

import functools
import logging
import sys
import threading
import time

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import __version__

from . import buildlog
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
from .models import (
    BuildDetail,
    BuildInfo,
    BuildLogResult,
    BuildProgress,
    BuildsResult,
    DeploymentDetail,
    DeploymentInfo,
    DeploymentProgress,
    DeploymentsResult,
    EnvironmentDetail,
    EnvironmentInfo,
    EnvironmentsResult,
    ServiceInfo,
    StageInfo,
    SubscriptionInfo,
    SubscriptionsResult,
    WaitResult,
)

log = logging.getLogger("ccv2_portal_mcp")

mcp = MCPServer("ccv2-portal", version=__version__, instructions=(
    "Read access to SAP Commerce Cloud (CCv2) Cloud Portal subscriptions: environments, builds, "
    "deployments and build logs. Start with ccv2portal_list_subscriptions when unsure which "
    "subscription or environment to use."
))

BUILD_TERMINAL = {"SUCCESS", "FAIL", "DELETED"}
DEPLOYMENT_TERMINAL = {"DEPLOYED", "FAIL", "UNDEPLOYED"}

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


# ------------------------------------------------------------------ registry

class Registry:
    """Lazy per-subscription clients, shared across tool calls."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._settings: Settings | None = None
        self._config: AppConfig | None = None
        self._clients: dict[str, PortalClient] = {}

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

    def configure(self, settings: Settings, config: AppConfig) -> None:
        """Used by tests and embedding code to inject configuration."""
        with self._lock:
            self._settings = settings
            self._config = config
            self._clients.clear()

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


registry = Registry()


class PermissionDenied(PermissionError):
    pass


def _require(sub: SubscriptionConfig, env: str, perm: str) -> None:
    if not sub.allows(env, perm):
        raise PermissionDenied(
            f"subscription {sub.name!r}: environment {env!r} does not grant {perm!r} "
            f"(granted: {sorted(sub.perms_for(env))}). Adjust the permissions configuration."
        )


def _tool(**kwargs):
    """Register a sync function as a tool; domain errors become readable tool errors."""

    def decorate(fn):
        @functools.wraps(fn)
        def run(*args, **kw):
            try:
                return fn(*args, **kw)
            except (PortalError, AuthError, ConfigError, PermissionDenied) as exc:
                # The SDK turns the exception into an isError tool result carrying this text.
                raise RuntimeError(str(exc)) from exc

        mcp.tool(**kwargs)(run)
        return fn

    return decorate


# --------------------------------------------------------------- normalisers

def _env_info(sub: SubscriptionConfig, raw: dict) -> EnvironmentInfo:
    info = EnvironmentInfo(**raw)
    info.permissions = sorted(sub.perms_for(info.code))
    return info


def _build_info(raw: dict) -> BuildInfo:
    return BuildInfo(**raw)


def _deployment_info(raw: dict) -> DeploymentInfo:
    return DeploymentInfo(**raw)


def _deployment_progress(raw: dict) -> DeploymentProgress:
    stages = []
    for st in raw.get("stages") or []:
        failed = []
        for step in st.get("steps") or []:
            if (step.get("status") or "").upper() == "FAIL":
                failed.append(f"{step.get('name') or step.get('code')}: {step.get('message') or ''}".strip())
        stages.append(StageInfo(**{k: v for k, v in st.items() if k != "steps"}, failed_steps=failed))
    return DeploymentProgress(**{k: v for k, v in raw.items() if k != "stages"}, stages=stages)


# --------------------------------------------------------------------- tools

@_tool(
    name="ccv2portal_list_subscriptions",
    description="List the configured CCv2 subscriptions and the per-environment permissions this server grants to agents. Call first when more than one subscription is configured.",
    annotations=READ_ONLY,
)
def ccv2portal_list_subscriptions() -> SubscriptionsResult:
    cfg = registry.config
    return SubscriptionsResult(
        subscriptions=[
            SubscriptionInfo(
                name=s.name,
                code=s.code,
                base_url=s.base_url,
                default_permissions=sorted(s.default_permissions),
                permissions={k: sorted(v) for k, v in s.permissions.items()},
                is_default=(s.name == cfg.default_subscription or len(cfg.subscriptions) == 1),
            )
            for s in cfg.subscriptions.values()
        ]
    )


@_tool(
    name="ccv2portal_list_environments",
    description="List environments of a subscription (code, type dev/staging/production, status, deployment status, hibernation status) with the permissions granted on each.",
    annotations=READ_ONLY,
)
def ccv2portal_list_environments(subscription: str | None = None) -> EnvironmentsResult:
    client = registry.client(subscription)
    envs = [_env_info(client.sub, e) for e in client.environments()]
    envs = [e for e in envs if client.sub.allows(e.code, PERM_READ)]
    return EnvironmentsResult(subscription=client.sub.name, environments=envs)


@_tool(
    name="ccv2portal_get_environment",
    description="One-call picture of an environment: status, services with desired/available replicas, the latest deployment and the build it carries, and scaling details. Partial failures (e.g. scaling not readable) are reported in *_error fields instead of failing the call.",
    annotations=READ_ONLY,
)
def ccv2portal_get_environment(environment: str, subscription: str | None = None) -> EnvironmentDetail:
    client = registry.client(subscription)
    _require(client.sub, environment, PERM_READ)
    envs = {e["code"]: e for e in client.environments()}
    if environment not in envs:
        raise PortalError(f"environment {environment!r} not found; known: {sorted(envs)}")
    detail = EnvironmentDetail(subscription=client.sub.name, environment=_env_info(client.sub, envs[environment]))
    try:
        detail.services = [ServiceInfo(**s) for s in client.services(environment)]
    except (PortalError, AuthError) as exc:
        detail.services_error = str(exc)
    deps = client.deployments(environment=environment, limit=1)
    if deps:
        detail.latest_deployment = _deployment_info(deps[0])
        if detail.latest_deployment.buildCode:
            try:
                detail.latest_build = _build_info(client.build(detail.latest_deployment.buildCode))
            except PortalError as exc:
                log.info("build %s lookup failed: %s", detail.latest_deployment.buildCode, exc)
    try:
        detail.scaling = client.scaling(environment)
    except (PortalError, AuthError) as exc:
        detail.scaling_error = str(exc)
    return detail


@_tool(
    name="ccv2portal_list_builds",
    description="List recent builds, newest first. Filter client-side by branch substring and/or status (BUILDING, SUCCESS, FAIL, SCHEDULED, DELETED, UNKNOWN).",
    annotations=READ_ONLY,
)
def ccv2portal_list_builds(
    subscription: str | None = None,
    branch: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> BuildsResult:
    client = registry.client(subscription)
    limit = max(1, min(limit, 200))
    fetch = limit if not (branch or status) else min(200, limit * 5)
    builds = [_build_info(b) for b in client.builds(limit=fetch)]
    if branch:
        builds = [b for b in builds if b.branch and branch.lower() in b.branch.lower()]
    if status:
        builds = [b for b in builds if (b.status or "").upper() == status.upper()]
    return BuildsResult(subscription=client.sub.name, builds=builds[:limit])


@_tool(
    name="ccv2portal_get_build",
    description="Details of one build plus its progress (percentage, tasks, error message) and the deployments made from it.",
    annotations=READ_ONLY,
)
def ccv2portal_get_build(build_code: str, subscription: str | None = None) -> BuildDetail:
    client = registry.client(subscription)
    build = _build_info(client.build(build_code))
    progress = None
    try:
        progress = BuildProgress(**client.build_progress(build_code))
    except PortalError as exc:
        log.info("progress for %s unavailable: %s", build_code, exc)
    status = (progress.buildStatus if progress and progress.buildStatus else build.status) or ""
    deployments = [_deployment_info(d) for d in client.deployments(build=build_code, limit=20)]
    return BuildDetail(
        subscription=client.sub.name,
        build=build,
        progress=progress,
        terminal=status.upper() in BUILD_TERMINAL,
        deployments=deployments,
    )


@_tool(
    name="ccv2portal_get_build_log",
    description=(
        "Read a build's log. The portal returns a zip; this tool unpacks it and returns lines. "
        "mode='errors' (default) returns a window around the first error-looking line; "
        "mode='grep' needs `pattern` (regex, case-insensitive) and returns matches with `context` lines; "
        "mode='tail' / 'head' return the last / first `lines` lines. Line numbers are prefixed."
    ),
    annotations=READ_ONLY,
)
def ccv2portal_get_build_log(
    build_code: str,
    mode: str = "errors",
    pattern: str | None = None,
    lines: int = 200,
    context: int = 2,
    entry: str | None = None,
    subscription: str | None = None,
) -> BuildLogResult:
    client = registry.client(subscription)
    max_lines = registry.settings.build_log_max_lines
    entries = buildlog.extract_entries(client.build_logs_zip(build_code))
    if not entries:
        raise PortalError(f"build {build_code}: log archive is empty")
    names = [n for n, _ in entries]
    if entry:
        match = next(((n, t) for n, t in entries if n == entry or n.endswith(entry)), None)
        if match is None:
            raise PortalError(f"log entry {entry!r} not in archive; entries: {names}")
        name, text = match
    else:
        name, text = entries[0]
    total = len(text.splitlines())
    mode = mode.lower()
    if mode == "errors":
        chosen = buildlog.first_error(text)
        if chosen is None:
            chosen, _, _ = buildlog.select_lines(text, tail=min(lines, max_lines), max_lines=max_lines)
            mode = "tail (no error pattern found)"
        truncated = False
    elif mode == "grep":
        if not pattern:
            raise ConfigError("mode='grep' requires `pattern`")
        chosen, _, truncated = buildlog.select_lines(text, grep=pattern, context=context, max_lines=max_lines)
    elif mode == "tail":
        chosen, _, truncated = buildlog.select_lines(text, tail=min(lines, max_lines), max_lines=max_lines)
    elif mode == "head":
        chosen, _, truncated = buildlog.select_lines(text, head=min(lines, max_lines), max_lines=max_lines)
    else:
        raise ConfigError("mode must be one of errors, grep, tail, head")
    return BuildLogResult(
        subscription=client.sub.name,
        build_code=build_code,
        entry=name,
        entries=names,
        total_lines=total,
        returned_lines=len(chosen),
        truncated=truncated,
        mode=mode,
        lines=chosen,
    )


@_tool(
    name="ccv2portal_list_deployments",
    description="List deployments, newest first, optionally filtered by environment, build code or status (SCHEDULED, DEPLOYING, DEPLOYED, UNDEPLOYED, FAIL).",
    annotations=READ_ONLY,
)
def ccv2portal_list_deployments(
    subscription: str | None = None,
    environment: str | None = None,
    build_code: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> DeploymentsResult:
    client = registry.client(subscription)
    if environment:
        _require(client.sub, environment, PERM_READ)
    deps = client.deployments(environment=environment, build=build_code, status=status, limit=max(1, min(limit, 200)))
    deps = [_deployment_info(d) for d in deps]
    deps = [d for d in deps if not d.environmentCode or client.sub.allows(d.environmentCode, PERM_READ)]
    return DeploymentsResult(subscription=client.sub.name, deployments=deps)


@_tool(
    name="ccv2portal_get_deployment",
    description="Details of one deployment: mode, strategy, timestamps, cancellation/canary info, the build it deploys, and progress by stage with any failed steps.",
    annotations=READ_ONLY,
)
def ccv2portal_get_deployment(deployment_code: str, subscription: str | None = None) -> DeploymentDetail:
    client = registry.client(subscription)
    dep = _deployment_info(client.deployment(deployment_code))
    if dep.environmentCode:
        _require(client.sub, dep.environmentCode, PERM_READ)
    build = None
    if dep.buildCode:
        try:
            build = _build_info(client.build(dep.buildCode))
        except PortalError as exc:
            log.info("build %s lookup failed: %s", dep.buildCode, exc)
    progress = None
    try:
        progress = _deployment_progress(client.deployment_progress(deployment_code))
    except PortalError as exc:
        log.info("progress for %s unavailable: %s", deployment_code, exc)
    status = (progress.deploymentStatus if progress and progress.deploymentStatus else dep.status) or ""
    return DeploymentDetail(
        subscription=client.sub.name,
        deployment=dep,
        build=build,
        progress=progress,
        terminal=status.upper() in DEPLOYMENT_TERMINAL,
    )


@_tool(
    name="ccv2portal_get_deployment_modes",
    description="Which database update modes (NONE/UPDATE/INITIALIZE) and strategies (ROLLING_UPDATE/RECREATE/GREEN) each environment allows. Check before creating a deployment.",
    annotations=READ_ONLY,
)
def ccv2portal_get_deployment_modes(subscription: str | None = None) -> dict:
    client = registry.client(subscription)
    data = client.deployment_modes()
    return {"subscription": client.sub.name, "modes": data}


def _wait(kind: str, code: str, fetch, terminal: set[str], timeout_sec: int | None, poll_sec: int | None, subscription: str | None) -> WaitResult:
    client = registry.client(subscription)
    s = registry.settings
    timeout = min(max(1, timeout_sec or s.wait_max_sec), s.wait_max_sec)
    poll = max(2, poll_sec or s.wait_poll_sec)
    start = time.monotonic()
    while True:
        raw = fetch(client, code)
        status = (raw.get("buildStatus") or raw.get("deploymentStatus") or raw.get("status") or "").upper() or None
        pct = raw.get("percentage")
        done = status in terminal
        elapsed = time.monotonic() - start
        if done or elapsed + poll > timeout:
            if done:
                hint = f"{kind} finished with {status}."
                succeeded = status in {"SUCCESS", "DEPLOYED"}
            else:
                hint = f"Still {status or 'running'} after {elapsed:.0f}s; call again to keep waiting (each call waits at most {s.wait_max_sec}s)."
                succeeded = None
            return WaitResult(
                subscription=client.sub.name, kind=kind, code=code, status=status, percentage=pct,
                done=done, succeeded=succeeded, waited_sec=round(elapsed, 1), hint=hint,
            )
        time.sleep(poll)


@_tool(
    name="ccv2portal_wait_for_build",
    description="Poll a build until it reaches a terminal state or the bounded timeout elapses (never longer than WAIT_MAX_SEC, default 60s). Returns done=false with a hint to call again while the build is still running.",
    annotations=READ_ONLY,
)
def ccv2portal_wait_for_build(
    build_code: str, timeout_sec: int | None = None, poll_sec: int | None = None, subscription: str | None = None
) -> WaitResult:
    return _wait("build", build_code, lambda c, code: c.build_progress(code), BUILD_TERMINAL, timeout_sec, poll_sec, subscription)


@_tool(
    name="ccv2portal_wait_for_deployment",
    description="Poll a deployment until it reaches a terminal state or the bounded timeout elapses (never longer than WAIT_MAX_SEC, default 60s). Returns done=false with a hint to call again while the deployment is still running.",
    annotations=READ_ONLY,
)
def ccv2portal_wait_for_deployment(
    deployment_code: str, timeout_sec: int | None = None, poll_sec: int | None = None, subscription: str | None = None
) -> WaitResult:
    return _wait("deployment", deployment_code, lambda c, code: c.deployment_progress(code), DEPLOYMENT_TERMINAL, timeout_sec, poll_sec, subscription)


# ---------------------------------------------------------------------- main

def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = registry.settings
    try:
        cfg = registry.config
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        sys.exit(2)
    log.info(
        "ccv2-portal-mcp starting: transport=%s subscriptions=%s",
        s.mcp_transport, {n: sub.redacted()["permissions"] for n, sub in cfg.subscriptions.items()},
    )
    if s.mcp_transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host=s.mcp_host, port=s.mcp_port)


if __name__ == "__main__":
    main()
