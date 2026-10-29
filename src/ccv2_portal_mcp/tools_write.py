"""Write tools. Every one is gated by per-environment permissions, audited, and the
dangerous ones (production, INITIALIZE, RECREATE, cancel, restore, scaling, delete)
require confirm=true, mirroring the portal's own CONFIRM prompt."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

from .app import (
    DESTRUCTIVE,
    WRITE,
    ConfirmationRequired,
    active_deployment,
    audited,
    backup_state,
    build_info,
    cert_code_of,
    env_or_fail,
    is_production,
    registry,
    require,
    require_any,
    safe,
    status_of,
    tool,
)
from .client import PortalClient, PortalError
from .config import (
    PERM_BACKUP_RESTORE,
    PERM_BUILD,
    PERM_DEPLOY,
    PERM_ENDPOINTS_WRITE,
    PERM_PROPERTIES_WRITE,
    PERM_SCALING,
    ConfigError,
)
from .models import WriteResult

log = logging.getLogger(__name__)

DB_MODES = {"NONE", "UPDATE", "INITIALIZE"}
STRATEGIES = {"ROLLING_UPDATE", "RECREATE", "GREEN"}
DECISIONS = {"ACCEPT", "REJECT", "PREPARE_CANARY"}
ACTIVITY_TYPES = {"HIBERNATE_COMMERCE_ENVIRONMENT", "WAKE_UP_COMMERCE_ENVIRONMENT", "UPGRADE_PREMIUM_DR", "DOWNGRADE_TO_STANDARD_DR"}


def _confirm(confirm: bool, why: str) -> None:
    if not confirm:
        raise ConfirmationRequired(f"{why} Re-run with confirm=true to proceed.")


def _deploy_perms(client: PortalClient, env: str) -> bool:
    """Require deploy on the environment. Returns whether env is production (confirm guard)."""
    require(client.sub, env, PERM_DEPLOY)
    return is_production(env_or_fail(client, env), env)


def allowed_modes(client: PortalClient, env: str) -> tuple[set[str] | None, set[str] | None]:
    """(db modes, strategies) the portal allows for env, or (None, None) when the shape is unknown."""
    try:
        data = client.deployment_modes()
    except PortalError as exc:
        log.info("deployment modes unavailable: %s", exc)
        return None, None
    items = data.get("value", data) if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = [dict(v, environmentCode=k) for k, v in items.items() if isinstance(v, dict)]
    if not isinstance(items, list):
        return None, None
    for it in items:
        if not isinstance(it, dict):
            continue
        code = it.get("environmentCode") or it.get("code") or it.get("environment")
        if code != env:
            continue
        db = it.get("dataMigrationMode") or it.get("databaseUpdateModes") or it.get("databaseUpdateMode")
        st = it.get("deploymentMode") or it.get("strategies") or it.get("strategy")
        return (set(db) if isinstance(db, list) else None, set(st) if isinstance(st, list) else None)
    return None, None


# -------------------------------------------------------------------- builds

@tool(
    name="ccv2portal_create_build",
    description="Trigger a build from a branch (or tag). `name` defaults to '<branch>-<UTC timestamp>'. Returns the build code; follow with ccv2portal_wait_for_build. Needs the 'build' permission on at least one environment.",
    annotations=WRITE,
)
@audited("ccv2portal_create_build", env_arg=None)
def ccv2portal_create_build(branch: str, name: str | None = None, subscription: str | None = None) -> WriteResult:
    client = registry.client(subscription)
    require_any(client.sub, PERM_BUILD)
    if not branch.strip():
        raise ConfigError("branch is required")
    name = name or f"{re.sub(r'[^A-Za-z0-9._-]+', '-', branch)}-{datetime.now(timezone.utc):%Y%m%d-%H%M}"
    resp = client.create_build(branch=branch, name=name)
    code = resp.get("code")
    return WriteResult(
        subscription=client.sub.name, action="create_build", code=code, executed=True, response=resp,
        hint=f"Build {code} scheduled from {branch!r}. Poll with ccv2portal_wait_for_build (builds take 20-60 min).",
    )


# --------------------------------------------------------------- deployments

@tool(
    name="ccv2portal_create_deployment",
    description=(
        "Deploy a build to an environment. database_update_mode: NONE | UPDATE | INITIALIZE; "
        "strategy: ROLLING_UPDATE | RECREATE | GREEN. Checks that the build succeeded, that no deployment "
        "is already running on the environment and that the portal allows the modes. confirm=true is "
        "required for production, INITIALIZE (wipes data) and RECREATE (downtime). backup_first=true takes a "
        "QUICK database backup and waits for it (bounded) before deploying; if the backup is still running "
        "the deployment is NOT started and the backup code is returned so you can wait and retry."
    ),
    annotations=DESTRUCTIVE,
)
@audited("ccv2portal_create_deployment")
def ccv2portal_create_deployment(
    build_code: str,
    environment: str,
    database_update_mode: str = "NONE",
    strategy: str = "ROLLING_UPDATE",
    confirm: bool = False,
    backup_first: bool = False,
    subscription: str | None = None,
) -> WriteResult:
    client = registry.client(subscription)
    db_mode, strat = database_update_mode.upper(), strategy.upper()
    if db_mode not in DB_MODES:
        raise ConfigError(f"database_update_mode must be one of {sorted(DB_MODES)}")
    if strat not in STRATEGIES:
        raise ConfigError(f"strategy must be one of {sorted(STRATEGIES)}")
    prod = _deploy_perms(client, environment)

    build = build_info(client.build(build_code))
    if (build.status or "").upper() != "SUCCESS":
        raise PortalError(f"build {build_code} has status {build.status}; only SUCCESS builds can be deployed")
    running = active_deployment(client, environment)
    if running:
        raise PortalError(
            f"environment {environment} already has deployment {running.get('code')} in status "
            f"{status_of(running)}; wait for it or cancel it first"
        )
    db_allowed, st_allowed = allowed_modes(client, environment)
    if db_allowed is not None and db_mode not in db_allowed:
        raise PortalError(f"portal does not allow database_update_mode {db_mode} on {environment}; allowed: {sorted(db_allowed)}")
    if st_allowed is not None and strat not in st_allowed:
        raise PortalError(f"portal does not allow strategy {strat} on {environment}; allowed: {sorted(st_allowed)}")

    reasons = []
    if prod:
        reasons.append(f"{environment} is a production environment")
    if db_mode == "INITIALIZE":
        reasons.append("INITIALIZE re-initialises the database (data loss)")
    if strat == "RECREATE":
        reasons.append("RECREATE causes downtime")
    if reasons:
        _confirm(confirm, f"Deploying build {build_code} ({build.branch}) to {environment} with {db_mode}/{strat}: " + "; ".join(reasons) + ".")

    backup_note = ""
    if backup_first:
        require(client.sub, environment, PERM_BACKUP_RESTORE)
        created = client.create_backup(environment, description=f"pre-deploy {build_code} ({build.branch})", backup_type="QUICK", databases=True, storages=False)
        backup_code = created.get("databackupCode")
        state = _wait_backup(client, environment, backup_code)
        if state == "failed":
            raise PortalError(f"pre-deploy backup {backup_code} failed; deployment not started")
        if state != "done":
            return WriteResult(
                subscription=client.sub.name, action="create_deployment", environment=environment, code=None, executed=False,
                response={"backup_code": backup_code, "backup_state": state},
                hint=f"Backup {backup_code} is still running; deployment NOT started. Wait with ccv2portal_wait_for_backup, then call again without backup_first.",
            )
        backup_note = f" Pre-deploy backup {backup_code} completed."

    resp = client.create_deployment(build_code=build_code, environment=environment, database_update_mode=db_mode, strategy=strat)
    code = resp.get("code")
    return WriteResult(
        subscription=client.sub.name, action="create_deployment", environment=environment, code=code, executed=True,
        response=resp,
        hint=f"Deployment {code} of build {build_code} ({build.branch}) to {environment} started ({db_mode}/{strat}).{backup_note} Poll with ccv2portal_wait_for_deployment."
        + (" GREEN: accept or reject afterwards with ccv2portal_blue_green_decision." if strat == "GREEN" else ""),
    )


def _wait_backup(client: PortalClient, env: str, code: str) -> str:
    """Poll a backup within the bounded wait window; returns backup_state()."""
    s = registry.settings
    start = time.monotonic()
    while True:
        state = backup_state(client.backup(env, code))
        if state != "running" or time.monotonic() - start + s.wait_poll_sec > s.wait_max_sec:
            return state
        time.sleep(max(2, s.wait_poll_sec))


@tool(
    name="ccv2portal_cancel_deployment",
    description="Cancel a running deployment (only possible once the 'Deploy application' stage is reached; causes downtime). rollback_database=true also reverts the DB to the pre-deployment restore point when the portal offers one. Always needs confirm=true. HTTP 501 means cancellation is not enabled for the subscription.",
    annotations=DESTRUCTIVE,
)
@audited("ccv2portal_cancel_deployment", env_arg=None)
def ccv2portal_cancel_deployment(
    deployment_code: str, reason: str, rollback_database: bool = False, confirm: bool = False, subscription: str | None = None
) -> WriteResult:
    client = registry.client(subscription)
    dep = client.deployment(deployment_code)
    env = dep.get("environmentCode") or ""
    _deploy_perms(client, env)
    if not 4 <= len(reason) <= 255:
        raise ConfigError("reason must be 4-255 characters")
    if status_of(dep) not in {"SCHEDULED", "DEPLOYING"}:
        raise PortalError(f"deployment {deployment_code} is {status_of(dep)}; only running deployments can be cancelled")
    options, _ = (None, None)
    try:
        options = client.cancellation_options(deployment_code)
    except PortalError as exc:
        if exc.status == 501:
            raise
        log.info("cancellation options unavailable: %s", exc)
    _confirm(confirm, f"Cancelling deployment {deployment_code} on {env} causes downtime" + (" and will roll back the database" if rollback_database else "") + ".")
    resp = client.cancel_deployment(deployment_code, rollback_database=rollback_database, reason=reason)
    return WriteResult(
        subscription=client.sub.name, action="cancel_deployment", environment=env, code=deployment_code, executed=True,
        response={"cancellation": resp, "options": options},
        hint="Cancellation accepted; poll with ccv2portal_get_deployment. Blob storage and Solr are not rolled back.",
    )


@tool(
    name="ccv2portal_blue_green_decision",
    description="Decide on a GREEN (blue-green) deployment: ACCEPT switches traffic to green, REJECT discards it, PREPARE_CANARY enables a traffic split. On production, confirm=true is required.",
    annotations=DESTRUCTIVE,
)
@audited("ccv2portal_blue_green_decision", env_arg=None)
def ccv2portal_blue_green_decision(
    deployment_code: str, decision: str, reason: str | None = None, confirm: bool = False, subscription: str | None = None
) -> WriteResult:
    client = registry.client(subscription)
    decision = decision.upper()
    if decision not in DECISIONS:
        raise ConfigError(f"decision must be one of {sorted(DECISIONS)}")
    dep = client.deployment(deployment_code)
    env = dep.get("environmentCode") or ""
    prod = _deploy_perms(client, env)
    if (dep.get("strategy") or "").upper() != "GREEN":
        raise PortalError(f"deployment {deployment_code} uses strategy {dep.get('strategy')}; decisions apply to GREEN deployments only")
    if prod:
        _confirm(confirm, f"{decision} on production deployment {deployment_code} ({env}) changes live traffic.")
    resp = client.create_deployment_decision(deployment_code, decision=decision, reason=reason)
    return WriteResult(
        subscription=client.sub.name, action=f"blue_green_{decision.lower()}", environment=env, code=deployment_code,
        executed=True, response=resp,
        hint=f"{decision} recorded. New deployment code (if any): {resp.get('newDeploymentCode')}.",
    )


@tool(
    name="ccv2portal_set_traffic_split",
    description="Canary traffic split for a GREEN deployment after PREPARE_CANARY: lists of {code, percentage} for services and endpoints. Production needs confirm=true.",
    annotations=DESTRUCTIVE,
)
@audited("ccv2portal_set_traffic_split", env_arg=None)
def ccv2portal_set_traffic_split(
    deployment_code: str, services: list[dict] | None = None, endpoints: list[dict] | None = None,
    confirm: bool = False, subscription: str | None = None,
) -> WriteResult:
    client = registry.client(subscription)
    dep = client.deployment(deployment_code)
    env = dep.get("environmentCode") or ""
    prod = _deploy_perms(client, env)
    for item in (services or []) + (endpoints or []):
        pct = item.get("percentage")
        if "code" not in item or not isinstance(pct, (int, float)) or not 0 <= pct <= 100:
            raise ConfigError("each split item needs 'code' and 'percentage' 0-100")
    if prod:
        _confirm(confirm, f"Changing the traffic split of production deployment {deployment_code} ({env}).")
    resp = client.update_traffic_split(deployment_code, services=services or [], endpoints=endpoints or [])
    return WriteResult(subscription=client.sub.name, action="set_traffic_split", environment=env, code=deployment_code, executed=True, response=resp, hint="Traffic split updated.")


# ---------------------------------------------------------------- properties

@tool(
    name="ccv2portal_set_property",
    description="Create or update one customer property of a service aspect (default service 'hcs_common'). Production needs confirm=true. Note: the portal may still require 'apply configuration' / a service restart before the value is live.",
    annotations=WRITE,
)
@audited("ccv2portal_set_property")
def ccv2portal_set_property(
    environment: str, key: str, value: str, service: str = "hcs_common", flags: list[str] | None = None,
    confirm: bool = False, subscription: str | None = None,
) -> WriteResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_PROPERTIES_WRITE)
    prod = is_production(env_or_fail(client, environment), environment)
    if prod:
        _confirm(confirm, f"Setting {key} on production environment {environment}.")
    previous, _ = (None, None)
    try:
        previous = client.property(environment, service, key).get("value")
    except PortalError as exc:
        if exc.status != 404:
            log.info("previous value unreadable: %s", exc)
    resp = client.put_property(environment, service, key, value, flags)
    return WriteResult(
        subscription=client.sub.name, action="set_property", environment=environment, code=key, executed=True,
        response={"service": service, "key": key, "previous": previous, "response": resp},
        hint="Stored. Verify it is applied (KBA 3285271: some aspects needed 'apply configuration' in the portal) and expect a service restart for most properties.",
    )


# ------------------------------------------------------------------- backups

@tool(
    name="ccv2portal_create_backup",
    description="Create a data backup. backup_type QUICK (default: DB only, point-in-time, 35-day expiry, max 4) or STANDARD (DB and optionally media, no expiry, max 3 prod / 2 non-prod).",
    annotations=WRITE,
)
@audited("ccv2portal_create_backup")
def ccv2portal_create_backup(
    environment: str, description: str, backup_type: str = "QUICK", include_databases: bool = True,
    include_storages: bool = False, subscription: str | None = None,
) -> WriteResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_BACKUP_RESTORE)
    env_or_fail(client, environment)
    backup_type = backup_type.upper()
    if backup_type not in {"QUICK", "STANDARD"}:
        raise ConfigError("backup_type must be QUICK or STANDARD")
    if backup_type == "QUICK" and include_storages:
        raise ConfigError("QUICK backups cover the database only; use STANDARD for media/storage")
    if not include_databases and not include_storages:
        raise ConfigError("nothing selected: include_databases and/or include_storages")
    resp = client.create_backup(environment, description=description, backup_type=backup_type, databases=include_databases, storages=include_storages)
    return WriteResult(
        subscription=client.sub.name, action="create_backup", environment=environment, code=resp.get("databackupCode"),
        executed=True, response=resp, hint="Backup started; check status with ccv2portal_list_backups.",
    )


@tool(
    name="ccv2portal_cancel_backup",
    description="Cancel a running data backup.",
    annotations=WRITE,
)
@audited("ccv2portal_cancel_backup")
def ccv2portal_cancel_backup(environment: str, backup_code: str, subscription: str | None = None) -> WriteResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_BACKUP_RESTORE)
    resp = client.cancel_backup(environment, backup_code)
    return WriteResult(subscription=client.sub.name, action="cancel_backup", environment=environment, code=backup_code, executed=True, response=resp, hint="Cancel requested.")


@tool(
    name="ccv2portal_delete_backup",
    description="Delete a data backup (asynchronous, irreversible). Needs confirm=true.",
    annotations=DESTRUCTIVE,
)
@audited("ccv2portal_delete_backup")
def ccv2portal_delete_backup(environment: str, backup_code: str, confirm: bool = False, subscription: str | None = None) -> WriteResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_BACKUP_RESTORE)
    _confirm(confirm, f"Deleting backup {backup_code} on {environment} is irreversible.")
    client.delete_backup(environment, backup_code)
    return WriteResult(subscription=client.sub.name, action="delete_backup", environment=environment, code=backup_code, executed=True, hint="Deletion started.")


@tool(
    name="ccv2portal_restore_backup",
    description=(
        "Restore a backup into target_environment. source_environment defaults to the target; set it to another "
        "environment (e.g. p1 -> s1) for a data refresh. restore_point_time (ISO 8601) picks a point in time for "
        "QUICK backups. The target's data is replaced and a RECREATE deployment is usually needed afterwards. "
        "Always needs confirm=true."
    ),
    annotations=DESTRUCTIVE,
)
@audited("ccv2portal_restore_backup", env_arg="target_environment")
def ccv2portal_restore_backup(
    target_environment: str, backup_code: str, source_environment: str | None = None,
    restore_point_time: str | None = None, confirm: bool = False, subscription: str | None = None,
) -> WriteResult:
    client = registry.client(subscription)
    require(client.sub, target_environment, PERM_BACKUP_RESTORE)
    source = source_environment or target_environment
    env_or_fail(client, target_environment)
    backups = {b.get("databackupCode"): b for b in client.backups(source)}
    if backup_code not in backups:
        raise PortalError(f"backup {backup_code} not found on {source}; known: {sorted(backups)}")
    if backups[backup_code].get("canBeRestored") is False:
        raise PortalError(f"backup {backup_code} reports canBeRestored=false (status {backups[backup_code].get('status')})")
    _confirm(confirm, f"Restoring backup {backup_code} from {source} into {target_environment} replaces the target's data.")
    resp = client.create_restore(target_environment, backup_code=backup_code, source_env=source, restore_point_time=restore_point_time)
    return WriteResult(
        subscription=client.sub.name, action="restore_backup", environment=target_environment, code=resp.get("datarestoreCode"),
        executed=True, response=resp,
        hint="Restore started; follow with ccv2portal_list_restores. Plan a RECREATE deployment and a maintenance page.",
    )


@tool(
    name="ccv2portal_cancel_restore",
    description="Cancel a running data restore. Warning: a cancelled restore can leave services down.",
    annotations=DESTRUCTIVE,
)
@audited("ccv2portal_cancel_restore")
def ccv2portal_cancel_restore(environment: str, restore_code: str, confirm: bool = False, subscription: str | None = None) -> WriteResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_BACKUP_RESTORE)
    _confirm(confirm, f"Cancelling restore {restore_code} on {environment} can leave services down.")
    resp = client.cancel_restore(environment, restore_code)
    return WriteResult(subscription=client.sub.name, action="cancel_restore", environment=environment, code=restore_code, executed=True, response=resp, hint="Cancel requested.")


# ------------------------------------------------------------------- scaling

@tool(
    name="ccv2portal_update_scaling",
    description=(
        "Patch environment scaling (Consumption Credits contracts only). `changes` is the PATCH body of the scaling "
        "API: {kubernetesClusters:[{code,maxWorkerCount,workerVmType:{code}}], databaseSchemas:[{code,maxSizeInMb,"
        "haReplicaCount,performance:{code}}], services:[{code,replicas,requestsCpu,requestsMemory,memoryScaleFactor,"
        "storageSize}]}. Read ccv2portal_get_scaling_options first. Needs confirm=true."
    ),
    annotations=DESTRUCTIVE,
)
@audited("ccv2portal_update_scaling")
def ccv2portal_update_scaling(environment: str, changes: dict, confirm: bool = False, subscription: str | None = None) -> WriteResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_SCALING)
    env_or_fail(client, environment)
    allowed_keys = {"kubernetesClusters", "databaseSchemas", "services", "hibernated", "autoscaled"}
    unknown = set(changes) - allowed_keys
    if unknown or not changes:
        raise ConfigError(f"changes must use only {sorted(allowed_keys)}; got {sorted(changes)}")
    _confirm(confirm, f"Changing scaling of {environment} affects running services and cost.")
    resp = client.patch_scaling(environment, changes)
    return WriteResult(subscription=client.sub.name, action="update_scaling", environment=environment, executed=True, response=resp, hint="Scaling patch accepted; re-read with ccv2portal_get_scaling_options.")


# ------------------------------------------------------- scheduled activities

@tool(
    name="ccv2portal_schedule_activity",
    description="Schedule an environment activity: HIBERNATE_COMMERCE_ENVIRONMENT, WAKE_UP_COMMERCE_ENVIRONMENT, UPGRADE_PREMIUM_DR or DOWNGRADE_TO_STANDARD_DR at scheduled_timestamp (ISO 8601 UTC). Hibernating production needs confirm=true.",
    annotations=WRITE,
)
@audited("ccv2portal_schedule_activity")
def ccv2portal_schedule_activity(
    environment: str, activity_type: str, scheduled_timestamp: str, confirm: bool = False, subscription: str | None = None
) -> WriteResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_SCALING)
    activity_type = activity_type.upper()
    if activity_type not in ACTIVITY_TYPES:
        raise ConfigError(f"activity_type must be one of {sorted(ACTIVITY_TYPES)}")
    try:
        datetime.fromisoformat(scheduled_timestamp.replace("Z", "+00:00"))
    except ValueError:
        raise ConfigError("scheduled_timestamp must be ISO 8601, e.g. 2026-11-01T22:00:00Z") from None
    if activity_type == "HIBERNATE_COMMERCE_ENVIRONMENT" and is_production(env_or_fail(client, environment), environment):
        _confirm(confirm, f"Hibernating production environment {environment} takes it offline.")
    resp = client.create_scheduled_activity(environment, activity_type=activity_type, scheduled_timestamp=scheduled_timestamp)
    return WriteResult(subscription=client.sub.name, action="schedule_activity", environment=environment, code=resp.get("activityCode") or resp.get("code"), executed=True, response=resp, hint=f"{activity_type} scheduled for {scheduled_timestamp}.")


@tool(
    name="ccv2portal_reschedule_activity",
    description="Move a scheduled activity to a new ISO 8601 timestamp.",
    annotations=WRITE,
)
@audited("ccv2portal_reschedule_activity")
def ccv2portal_reschedule_activity(environment: str, activity_code: str, scheduled_timestamp: str, subscription: str | None = None) -> WriteResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_SCALING)
    resp = client.update_scheduled_activity(environment, activity_code, scheduled_timestamp=scheduled_timestamp)
    return WriteResult(subscription=client.sub.name, action="reschedule_activity", environment=environment, code=activity_code, executed=True, response=resp, hint="Rescheduled.")


@tool(
    name="ccv2portal_cancel_activity",
    description="Cancel a scheduled activity that has not run yet.",
    annotations=WRITE,
)
@audited("ccv2portal_cancel_activity")
def ccv2portal_cancel_activity(environment: str, activity_code: str, subscription: str | None = None) -> WriteResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_SCALING)
    resp = client.cancel_scheduled_activity(environment, activity_code)
    return WriteResult(subscription=client.sub.name, action="cancel_activity", environment=environment, code=activity_code, executed=True, response=resp, hint="Cancelled.")





# ------------------------------------------------------------- endpoints

@tool(
    name="ccv2portal_set_maintenance_mode",
    description=(
        "Switch maintenance mode on or off for endpoints of an environment in one call. Defaults to every "
        "endpoint; pass `endpoints` (codes) to limit it, or web_proxy ('public'/'private') to filter. Uses the "
        "bulk endpoint patch and falls back to per-endpoint patches. Production needs confirm=true."
    ),
    annotations=DESTRUCTIVE,
)
@audited("ccv2portal_set_maintenance_mode")
def ccv2portal_set_maintenance_mode(
    environment: str, enabled: bool, endpoints: list[str] | None = None, web_proxy: str | None = None,
    confirm: bool = False, subscription: str | None = None,
) -> WriteResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_ENDPOINTS_WRITE)
    prod = is_production(env_or_fail(client, environment), environment)
    current = {e["code"]: e for e in client.endpoints(environment, web_proxy=web_proxy)}
    if endpoints:
        unknown = [c for c in endpoints if c not in current]
        if unknown:
            raise PortalError(f"unknown endpoint code(s) {unknown}; known: {sorted(current)}")
        targets = [current[c] for c in endpoints]
    else:
        targets = list(current.values())
    if not targets:
        raise PortalError(f"no endpoints found on {environment}" + (f" for web_proxy={web_proxy}" if web_proxy else ""))
    to_change = [e for e in targets if bool(e.get("maintenanceMode")) != enabled]
    unchanged = [e["code"] for e in targets if e not in to_change]
    if not to_change:
        return WriteResult(subscription=client.sub.name, action="set_maintenance_mode", environment=environment, executed=False,
                           response={"unchanged": unchanged}, hint=f"Maintenance mode already {'on' if enabled else 'off'} for {unchanged}.")
    if prod:
        _confirm(confirm, f"Turning maintenance mode {'ON' if enabled else 'OFF'} for {[e['code'] for e in to_change]} on production environment {environment}.")
    items = [{"code": e["code"], "maintenanceMode": enabled} for e in to_change]
    changed, errors, method = [], {}, "bulk"
    try:
        client.update_endpoints_bulk(environment, items)
        changed = [e["code"] for e in to_change]
    except PortalError as exc:
        if exc.status not in {404, 405, 415, 501}:
            raise
        method = "per-endpoint"
        for e in to_change:
            _, err = safe(lambda e=e: client.update_endpoint(environment, e["code"], {"maintenanceMode": enabled}))
            (errors.__setitem__(e["code"], err) if err else changed.append(e["code"]))
    return WriteResult(
        subscription=client.sub.name, action="set_maintenance_mode", environment=environment, executed=bool(changed),
        response={"enabled": enabled, "changed": changed, "unchanged": unchanged, "errors": errors, "method": method},
        hint=f"Maintenance mode {'on' if enabled else 'off'} for {changed}." + (f" Failed: {errors}." if errors else ""),
    )


# ---------------------------------------------------------- certificates

def _check_pem(label: str, pem: str, kind: str) -> None:
    if f"-----BEGIN {kind}" not in pem and not (kind == "PRIVATE KEY" and "PRIVATE KEY-----" in pem):
        raise ConfigError(f"{label} must be PEM text including the BEGIN/END {kind} markers")


@tool(
    name="ccv2portal_upload_certificate",
    description="Upload an SSL certificate (PEM certificate, matching PEM private key, optional PEM CA chain) to the subscription. Returns the certificate code to assign with ccv2portal_rotate_certificate. Needs endpoints_write on any environment.",
    annotations=WRITE,
)
@audited("ccv2portal_upload_certificate", env_arg=None)
def ccv2portal_upload_certificate(
    name: str, certificate_pem: str, private_key_pem: str, ca_certificate_pem: str | None = None,
    description: str | None = None, subscription: str | None = None,
) -> WriteResult:
    client = registry.client(subscription)
    require_any(client.sub, PERM_ENDPOINTS_WRITE)
    _check_pem("certificate_pem", certificate_pem, "CERTIFICATE")
    _check_pem("private_key_pem", private_key_pem, "PRIVATE KEY")
    if ca_certificate_pem:
        _check_pem("ca_certificate_pem", ca_certificate_pem, "CERTIFICATE")
    resp = client.create_certificate(name=name, certificate=certificate_pem, certificate_key=private_key_pem, ca_certificate=ca_certificate_pem, description=description)
    code = resp.get("code")
    return WriteResult(
        subscription=client.sub.name, action="upload_certificate", code=code, executed=True,
        response={k: resp.get(k) for k in ("code", "name", "domains", "issuer", "expirationDate")},
        hint=f"Certificate {code} uploaded (domains {resp.get('domains')}, expires {resp.get('expirationDate')}). Assign it with ccv2portal_rotate_certificate.",
    )


@tool(
    name="ccv2portal_rotate_certificate",
    description=(
        "Point endpoints of an environment at a new certificate. Defaults to every HTTPS endpoint that currently "
        "uses `old_certificate_code` (or any certificate when it is omitted); `endpoints` limits the set. "
        "delete_old=true removes the old certificate afterwards if no endpoint anywhere still uses it. "
        "Production needs confirm=true."
    ),
    annotations=DESTRUCTIVE,
)
@audited("ccv2portal_rotate_certificate")
def ccv2portal_rotate_certificate(
    environment: str, new_certificate_code: str, old_certificate_code: str | None = None, endpoints: list[str] | None = None,
    delete_old: bool = False, confirm: bool = False, subscription: str | None = None,
) -> WriteResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_ENDPOINTS_WRITE)
    prod = is_production(env_or_fail(client, environment), environment)
    new_cert = client.certificate(new_certificate_code)
    details = [client.endpoint(environment, e["code"]) for e in client.endpoints(environment)]
    if endpoints:
        unknown = [c for c in endpoints if c not in {d["code"] for d in details}]
        if unknown:
            raise PortalError(f"unknown endpoint code(s) {unknown}")
        details = [d for d in details if d["code"] in endpoints]
    targets = []
    for d in details:
        cur = cert_code_of(d)
        if cur == new_certificate_code:
            continue
        if old_certificate_code and cur != old_certificate_code:
            continue
        if not old_certificate_code and not endpoints and (d.get("protocol") or "HTTPS").upper() != "HTTPS":
            continue
        targets.append(d)
    if not targets:
        return WriteResult(subscription=client.sub.name, action="rotate_certificate", environment=environment, code=new_certificate_code,
                           executed=False, hint="No endpoint needs changing (already on the new certificate or no match).")
    domains = [str(x).lower() for x in new_cert.get("domains") or []]
    mismatched = [d["code"] for d in targets if d.get("domainName") and domains and not any(
        d["domainName"].lower() == dom or (dom.startswith("*.") and d["domainName"].lower().endswith(dom[1:])) for dom in domains)]
    if mismatched:
        raise PortalError(f"certificate {new_certificate_code} covers {domains}, which does not match endpoint domain(s) of {mismatched}")
    if prod:
        _confirm(confirm, f"Switching {[t['code'] for t in targets]} on production environment {environment} to certificate {new_certificate_code}.")
    changed, errors = [], {}
    for d in targets:
        _, err = safe(lambda d=d: client.update_endpoint(environment, d["code"], {"sslCertificate": new_certificate_code}))
        (errors.__setitem__(d["code"], err) if err else changed.append(d["code"]))
    deleted = []
    if delete_old and changed and not errors:
        old_codes = {cert_code_of(d) for d in targets} - {None, new_certificate_code}
        for code in old_codes:
            cert, err = safe(lambda code=code: client.certificate(code))
            if cert and not cert.get("endpoints"):
                _, derr = safe(lambda code=code: client.delete_certificate(code))
                (errors.__setitem__(f"delete {code}", derr) if derr else deleted.append(code))
            elif cert:
                errors[f"delete {code}"] = f"still used by {[e.get('code') for e in cert.get('endpoints')]}"
    return WriteResult(
        subscription=client.sub.name, action="rotate_certificate", environment=environment, code=new_certificate_code, executed=bool(changed),
        response={"changed": changed, "errors": errors, "deleted_certificates": deleted, "expires": new_cert.get("expirationDate")},
        hint=f"{len(changed)} endpoint(s) now use {new_certificate_code}." + (f" Deleted {deleted}." if deleted else "") + (f" Errors: {errors}." if errors else ""),
    )


@tool(
    name="ccv2portal_delete_certificate",
    description="Delete an SSL certificate that no endpoint uses any more. Needs confirm=true.",
    annotations=DESTRUCTIVE,
)
@audited("ccv2portal_delete_certificate", env_arg=None)
def ccv2portal_delete_certificate(certificate_code: str, confirm: bool = False, subscription: str | None = None) -> WriteResult:
    client = registry.client(subscription)
    require_any(client.sub, PERM_ENDPOINTS_WRITE)
    cert = client.certificate(certificate_code)
    if cert.get("endpoints"):
        raise PortalError(f"certificate {certificate_code} is still used by {[e.get('code') for e in cert['endpoints']]}; rotate first")
    _confirm(confirm, f"Deleting certificate {certificate_code} ({cert.get('name')}) is irreversible.")
    client.delete_certificate(certificate_code)
    return WriteResult(subscription=client.sub.name, action="delete_certificate", code=certificate_code, executed=True, hint="Deleted.")


__all__ = [n for n in dir() if n.startswith("ccv2portal_")]
