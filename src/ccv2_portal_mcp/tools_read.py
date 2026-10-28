"""Read-only tools."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import buildlog
from .app import (
    BUILD_TERMINAL,
    DEPLOYMENT_TERMINAL,
    READ_ONLY,
    build_info,
    deployment_info,
    deployment_progress,
    env_info,
    env_or_fail,
    registry,
    require,
    safe,
    tool,
)
from .audit import SECRET_KEY
from .client import PortalError
from .config import PERM_READ, ConfigError
from .models import (
    ActivitiesResult,
    ActivityInfo,
    BackupInfo,
    BackupsResult,
    BuildDetail,
    BuildLogResult,
    BuildProgress,
    BuildsResult,
    CertificateInfo,
    CertificatesResult,
    DeploymentDetail,
    DeploymentsResult,
    EndpointInfo,
    EndpointsResult,
    EnvironmentDetail,
    EnvironmentsResult,
    PropertyResult,
    RestoreInfo,
    RestoresResult,
    ServiceInfo,
    SubscriptionInfo,
    SubscriptionsResult,
)

log = logging.getLogger(__name__)


@tool(
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


@tool(
    name="ccv2portal_list_environments",
    description="List environments of a subscription (code, type dev/staging/production, status, deployment status, hibernation status) with the permissions granted on each.",
    annotations=READ_ONLY,
)
def ccv2portal_list_environments(subscription: str | None = None) -> EnvironmentsResult:
    client = registry.client(subscription)
    envs = [env_info(client.sub, e) for e in registry.environments(client, fresh=True).values()]
    envs = [e for e in envs if client.sub.allows(e.code, PERM_READ)]
    return EnvironmentsResult(subscription=client.sub.name, environments=envs)


@tool(
    name="ccv2portal_get_environment",
    description="One-call picture of an environment: status, services with desired/available replicas, the latest deployment and the build it carries, and scaling details. Partial failures (e.g. scaling not readable) are reported in *_error fields instead of failing the call.",
    annotations=READ_ONLY,
)
def ccv2portal_get_environment(environment: str, subscription: str | None = None) -> EnvironmentDetail:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_READ)
    raw = env_or_fail(client, environment)
    detail = EnvironmentDetail(subscription=client.sub.name, environment=env_info(client.sub, raw))
    services, err = safe(lambda: client.services(environment))
    if err:
        detail.services_error = err
    else:
        detail.services = [ServiceInfo(**s) for s in services]
    deps = client.deployments(environment=environment, limit=1)
    if deps:
        detail.latest_deployment = deployment_info(deps[0])
        if detail.latest_deployment.buildCode:
            b, err = safe(lambda: client.build(detail.latest_deployment.buildCode))
            if b:
                detail.latest_build = build_info(b)
    scaling, err = safe(lambda: client.scaling(environment))
    detail.scaling, detail.scaling_error = scaling, err
    return detail


@tool(
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
    builds = [build_info(b) for b in client.builds(limit=fetch)]
    if branch:
        builds = [b for b in builds if b.branch and branch.lower() in b.branch.lower()]
    if status:
        builds = [b for b in builds if (b.status or "").upper() == status.upper()]
    return BuildsResult(subscription=client.sub.name, builds=builds[:limit])


@tool(
    name="ccv2portal_get_build",
    description="Details of one build plus its progress (percentage, tasks, error message) and the deployments made from it.",
    annotations=READ_ONLY,
)
def ccv2portal_get_build(build_code: str, subscription: str | None = None) -> BuildDetail:
    client = registry.client(subscription)
    build = build_info(client.build(build_code))
    progress_raw, _ = safe(lambda: client.build_progress(build_code))
    progress = BuildProgress(**progress_raw) if progress_raw else None
    status = (progress.buildStatus if progress and progress.buildStatus else build.status) or ""
    deployments = [deployment_info(d) for d in client.deployments(build=build_code, limit=20)]
    return BuildDetail(
        subscription=client.sub.name,
        build=build,
        progress=progress,
        terminal=status.upper() in BUILD_TERMINAL,
        deployments=deployments,
    )


@tool(
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
    truncated = False
    if mode == "errors":
        chosen = buildlog.first_error(text)
        if chosen is None:
            chosen, _, _ = buildlog.select_lines(text, tail=min(lines, max_lines), max_lines=max_lines)
            mode = "tail (no error pattern found)"
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
        subscription=client.sub.name, build_code=build_code, entry=name, entries=names,
        total_lines=total, returned_lines=len(chosen), truncated=truncated, mode=mode, lines=chosen,
    )


@tool(
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
        require(client.sub, environment, PERM_READ)
    deps = client.deployments(environment=environment, build=build_code, status=status, limit=max(1, min(limit, 200)))
    deps = [deployment_info(d) for d in deps]
    deps = [d for d in deps if not d.environmentCode or client.sub.allows(d.environmentCode, PERM_READ)]
    return DeploymentsResult(subscription=client.sub.name, deployments=deps)


@tool(
    name="ccv2portal_get_deployment",
    description="Details of one deployment: mode, strategy, timestamps, cancellation/canary info, the build it deploys, and progress by stage with any failed steps.",
    annotations=READ_ONLY,
)
def ccv2portal_get_deployment(deployment_code: str, subscription: str | None = None) -> DeploymentDetail:
    client = registry.client(subscription)
    dep = deployment_info(client.deployment(deployment_code))
    if dep.environmentCode:
        require(client.sub, dep.environmentCode, PERM_READ)
    build = None
    if dep.buildCode:
        b, _ = safe(lambda: client.build(dep.buildCode))
        build = build_info(b) if b else None
    progress_raw, _ = safe(lambda: client.deployment_progress(deployment_code))
    progress = deployment_progress(progress_raw) if progress_raw else None
    status = (progress.deploymentStatus if progress and progress.deploymentStatus else dep.status) or ""
    return DeploymentDetail(
        subscription=client.sub.name, deployment=dep, build=build, progress=progress,
        terminal=status.upper() in DEPLOYMENT_TERMINAL,
    )


@tool(
    name="ccv2portal_get_deployment_modes",
    description="Which database update modes (NONE/UPDATE/INITIALIZE) and strategies (ROLLING_UPDATE/RECREATE/GREEN) each environment allows. Check before creating a deployment.",
    annotations=READ_ONLY,
)
def ccv2portal_get_deployment_modes(subscription: str | None = None) -> dict:
    client = registry.client(subscription)
    return {"subscription": client.sub.name, "modes": client.deployment_modes()}


@tool(
    name="ccv2portal_get_deployment_decisions",
    description="Blue-green decisions (ACCEPT / REJECT / PREPARE_CANARY) recorded for a GREEN deployment, plus its current traffic split and cancellation options if any.",
    annotations=READ_ONLY,
)
def ccv2portal_get_deployment_decisions(deployment_code: str, subscription: str | None = None) -> dict:
    client = registry.client(subscription)
    dep = client.deployment(deployment_code)
    if dep.get("environmentCode"):
        require(client.sub, dep["environmentCode"], PERM_READ)
    decisions, d_err = safe(lambda: client.deployment_decisions(deployment_code))
    split, s_err = safe(lambda: client.traffic_split(deployment_code))
    cancel, c_err = safe(lambda: client.cancellation_options(deployment_code))
    return {
        "subscription": client.sub.name,
        "deployment": {k: dep.get(k) for k in ("code", "environmentCode", "buildCode", "status", "strategy", "canaryStatus", "isCanaryAvailable", "expireAtTimestamp")},
        "decisions": (decisions or {}).get("deploymentDecisions", decisions) if decisions is not None else None,
        "decisions_error": d_err,
        "traffic_split": split,
        "traffic_split_error": s_err,
        "cancellation_options": cancel,
        "cancellation_options_error": c_err,
    }


@tool(
    name="ccv2portal_get_property",
    description="Read one customer property of a service aspect (e.g. service 'hcs_common' or 'hcs_platform_api'). Secret-looking values are masked unless reveal=true.",
    annotations=READ_ONLY,
)
def ccv2portal_get_property(
    environment: str, key: str, service: str = "hcs_common", reveal: bool = False, subscription: str | None = None
) -> PropertyResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_READ)
    raw = client.property(environment, service, key)
    value = raw.get("value")
    masked = bool(value) and bool(SECRET_KEY.search(key)) and not reveal
    return PropertyResult(
        subscription=client.sub.name, environment=environment, service=service, key=raw.get("key", key),
        value="***" if masked else value, masked=masked, flags=list(raw.get("flags") or []),
    )


@tool(
    name="ccv2portal_list_endpoints",
    description="Public/private endpoints of an environment with their URL, service, web proxy and maintenance-mode state. detail=true also returns the full endpoint configuration (IP filter sets, certificate, caching, WAF).",
    annotations=READ_ONLY,
)
def ccv2portal_list_endpoints(environment: str, detail: bool = False, subscription: str | None = None) -> EndpointsResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_READ)
    items = client.endpoints(environment)
    if detail:
        items = [client.endpoint(environment, e["code"]) for e in items]
    return EndpointsResult(subscription=client.sub.name, environment=environment, endpoints=[EndpointInfo(**e) for e in items])


@tool(
    name="ccv2portal_list_certificates",
    description="SSL certificates of the subscription with expiry, issuer, domains and the endpoints using them; days_to_expiry is computed.",
    annotations=READ_ONLY,
)
def ccv2portal_list_certificates(subscription: str | None = None) -> CertificatesResult:
    client = registry.client(subscription)
    out = []
    now = datetime.now(timezone.utc)
    for c in client.certificates():
        info = CertificateInfo(**c)
        if info.expirationDate:
            try:
                exp = datetime.fromisoformat(info.expirationDate.replace("Z", "+00:00"))
                info.days_to_expiry = (exp - now).days
            except ValueError:
                pass
        out.append(info)
    return CertificatesResult(subscription=client.sub.name, certificates=out)


@tool(
    name="ccv2portal_list_backups",
    description="Data backups of an environment (QUICK = DB point-in-time, 35-day expiry; STANDARD = DB + media) with status and whether each can be restored, deleted or cancelled.",
    annotations=READ_ONLY,
)
def ccv2portal_list_backups(environment: str, subscription: str | None = None) -> BackupsResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_READ)
    return BackupsResult(subscription=client.sub.name, environment=environment, backups=[BackupInfo(**b) for b in client.backups(environment)])


@tool(
    name="ccv2portal_list_restores",
    description="Data restores that targeted an environment, with status.",
    annotations=READ_ONLY,
)
def ccv2portal_list_restores(environment: str, subscription: str | None = None) -> RestoresResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_READ)
    return RestoresResult(subscription=client.sub.name, environment=environment, restores=[RestoreInfo(**r) for r in client.restores(environment)])


@tool(
    name="ccv2portal_get_scaling_options",
    description="Allowed scaling values for an environment (worker VM types and counts, database sizes and performance tiers, memory scale factors) next to the current scaling. Changing scaling needs a Consumption Credits contract.",
    annotations=READ_ONLY,
)
def ccv2portal_get_scaling_options(environment: str, subscription: str | None = None) -> dict:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_READ)
    current, c_err = safe(lambda: client.scaling(environment))
    options, o_err = safe(lambda: client.scaling_options(environment))
    return {"subscription": client.sub.name, "environment": environment, "current": current, "current_error": c_err, "options": options, "options_error": o_err}


@tool(
    name="ccv2portal_list_scheduled_activities",
    description="Scheduled activities of an environment: hibernation / wake-up and DR package changes, with status and timestamps.",
    annotations=READ_ONLY,
)
def ccv2portal_list_scheduled_activities(environment: str, status: list[str] | None = None, subscription: str | None = None) -> ActivitiesResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_READ)
    items = client.scheduled_activities(environment, status=status)
    return ActivitiesResult(subscription=client.sub.name, environment=environment, activities=[ActivityInfo(**a) for a in items])

__all__ = [n for n in dir() if n.startswith("ccv2portal_")]
