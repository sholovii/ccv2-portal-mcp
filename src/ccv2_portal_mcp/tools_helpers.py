"""Pipeline helpers: the things every CI pipeline re-implements around the raw API."""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone

from . import buildlog
from .app import (
    BUILD_TERMINAL,
    DEPLOYMENT_TERMINAL,
    DESTRUCTIVE,
    READ_ONLY,
    active_deployment,
    audited,
    backup_state,
    build_info,
    env_or_fail,
    is_production,
    latest_deployed,
    registry,
    require,
    safe,
    status_of,
    tool,
)
from .audit import SECRET_KEY
from .client import PortalClient, PortalError
from .config import PERM_BACKUP_RESTORE, PERM_DEPLOY, PERM_READ
from .models import (
    Check,
    ChecklistResult,
    DeployedState,
    Diagnosis,
    PropertyDiff,
    PropertyDiffResult,
    RollbackPlan,
    TimelineEvent,
    TimelineResult,
    WaitResult,
    WhatsDeployedResult,
)
from .tools_write import allowed_modes

log = logging.getLogger(__name__)


# --------------------------------------------------------------------- waits

def _wait(kind: str, code: str, fetch, terminal: set[str], timeout_sec: int | None, poll_sec: int | None, subscription: str | None) -> WaitResult:
    client = registry.client(subscription)
    s = registry.settings
    timeout = min(max(1, timeout_sec or s.wait_max_sec), s.wait_max_sec)
    poll = max(2, poll_sec or s.wait_poll_sec)
    start = time.monotonic()
    while True:
        raw = fetch(client, code)
        status = status_of(raw)
        pct = raw.get("percentage")
        done = status in terminal
        elapsed = time.monotonic() - start
        if done or elapsed + poll > timeout:
            if done:
                hint = f"{kind} finished with {status}."
                succeeded = status in {"SUCCESS", "DEPLOYED"}
            else:
                shown = status or "running"
                if kind == "build" and status in {None, "UNKNOWN", "SCHEDULED"}:
                    shown = "queued (the portal reports UNKNOWN until the build starts, typically within 5-10 min)"
                hint = f"Still {shown} after {elapsed:.0f}s; call again to keep waiting (each call waits at most {s.wait_max_sec}s)."
                succeeded = None
            return WaitResult(
                subscription=client.sub.name, kind=kind, code=code, status=status, percentage=pct,
                done=done, succeeded=succeeded, waited_sec=round(elapsed, 1), hint=hint,
            )
        time.sleep(poll)


@tool(
    name="ccv2portal_wait_for_build",
    description="Poll a build until it reaches a terminal state or the bounded timeout elapses (never longer than WAIT_MAX_SEC, default 60s). Returns done=false with a hint to call again while the build is still running.",
    annotations=READ_ONLY,
)
def ccv2portal_wait_for_build(build_code: str, timeout_sec: int | None = None, poll_sec: int | None = None, subscription: str | None = None) -> WaitResult:
    return _wait("build", build_code, lambda c, code: c.build_progress(code), BUILD_TERMINAL, timeout_sec, poll_sec, subscription)


@tool(
    name="ccv2portal_wait_for_deployment",
    description="Poll a deployment until it reaches a terminal state or the bounded timeout elapses (never longer than WAIT_MAX_SEC, default 60s). Returns done=false with a hint to call again while the deployment is still running.",
    annotations=READ_ONLY,
)
def ccv2portal_wait_for_deployment(deployment_code: str, timeout_sec: int | None = None, poll_sec: int | None = None, subscription: str | None = None) -> WaitResult:
    return _wait("deployment", deployment_code, lambda c, code: c.deployment_progress(code), DEPLOYMENT_TERMINAL, timeout_sec, poll_sec, subscription)


@tool(
    name="ccv2portal_wait_for_backup",
    description="Poll a data backup until it is restorable, failed, or the bounded timeout elapses (never longer than WAIT_MAX_SEC per call). Returns done=false with a hint to call again while it is still running.",
    annotations=READ_ONLY,
)
def ccv2portal_wait_for_backup(environment: str, backup_code: str, timeout_sec: int | None = None, poll_sec: int | None = None, subscription: str | None = None) -> WaitResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_READ)
    s = registry.settings
    timeout = min(max(1, timeout_sec or s.wait_max_sec), s.wait_max_sec)
    poll = max(2, poll_sec or s.wait_poll_sec)
    start = time.monotonic()
    while True:
        raw = client.backup(environment, backup_code)
        state = backup_state(raw)
        elapsed = time.monotonic() - start
        if state != "running" or elapsed + poll > timeout:
            done = state != "running"
            return WaitResult(
                subscription=client.sub.name, kind="backup", code=backup_code, status=raw.get("status"), done=done,
                succeeded=(state == "done") if done else None, waited_sec=round(elapsed, 1),
                hint=f"backup {state}." if done else f"Still running after {elapsed:.0f}s; call again (each call waits at most {s.wait_max_sec}s).",
            )
        time.sleep(poll)


# ------------------------------------------------------------ what's deployed

def _deployed_state(client: PortalClient, env_code: str, env_raw: dict | None) -> DeployedState:
    state = DeployedState(environment=env_code, type=(env_raw or {}).get("type"))
    dep, err = safe(lambda: latest_deployed(client, env_code))
    if err:
        state.error = err
        return state
    if dep:
        state.deployment_code = dep.get("code")
        state.deployed_at = dep.get("deployedTimestamp") or dep.get("createdTimestamp")
        state.deployed_by = dep.get("createdBy")
        state.strategy = dep.get("strategy")
        state.database_update_mode = dep.get("databaseUpdateMode")
        state.build_code = dep.get("buildCode")
        if state.build_code:
            b, _ = safe(lambda: client.build(state.build_code))
            if b:
                state.branch, state.build_name = b.get("branch"), b.get("name")
                state.build_version, state.build_created_by = b.get("buildVersion"), b.get("createdBy")
    running, _ = safe(lambda: active_deployment(client, env_code))
    if running:
        state.active_deployment = f"{running.get('code')} ({status_of(running)}, build {running.get('buildCode')})"
    return state


@tool(
    name="ccv2portal_whats_deployed",
    description="Which build (code, branch, version, who, when) is live on each environment, plus a plain-language comparison between environments and any deployment currently running. Defaults to all readable environments.",
    annotations=READ_ONLY,
)
def ccv2portal_whats_deployed(environments: list[str] | None = None, subscription: str | None = None) -> WhatsDeployedResult:
    client = registry.client(subscription)
    all_envs = registry.environments(client)
    codes = environments or sorted(all_envs)
    states = []
    for code in codes:
        require(client.sub, code, PERM_READ)
        states.append(_deployed_state(client, code, all_envs.get(code)))
    comparison = []
    with_build = [s for s in states if s.build_code]
    for i, a in enumerate(with_build):
        for b in with_build[i + 1:]:
            if a.build_code == b.build_code:
                comparison.append(f"{a.environment} and {b.environment} run the same build {a.build_code}.")
            elif a.branch and a.branch == b.branch:
                comparison.append(f"{a.environment} and {b.environment} both run branch {a.branch} but different builds ({a.build_code} vs {b.build_code}).")
            else:
                comparison.append(f"{a.environment} runs {a.branch or '?'} ({a.build_code}); {b.environment} runs {b.branch or '?'} ({b.build_code}).")
    for s in states:
        if s.active_deployment:
            comparison.append(f"{s.environment}: deployment in progress: {s.active_deployment}.")
    return WhatsDeployedResult(subscription=client.sub.name, environments=states, comparison=comparison)


# ------------------------------------------------------------------ rollback

@tool(
    name="ccv2portal_rollback_environment",
    description=(
        "Roll an environment back to the build of its previous successful deployment by redeploying it with "
        "database_update_mode=NONE (the portal has no rollback primitive; this is the documented practice). "
        "confirm=false (default) only returns the plan. Needs the deploy permission. "
        "Database schema changes made by the current build are NOT reverted."
    ),
    annotations=DESTRUCTIVE,
)
@audited("ccv2portal_rollback_environment")
def ccv2portal_rollback_environment(
    environment: str, confirm: bool = False, strategy: str = "ROLLING_UPDATE", target_build_code: str | None = None,
    subscription: str | None = None,
) -> RollbackPlan:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_DEPLOY)
    env_or_fail(client, environment)
    strategy = strategy.upper()
    if strategy not in {"ROLLING_UPDATE", "RECREATE"}:
        raise PortalError("rollback strategy must be ROLLING_UPDATE or RECREATE")

    deployed = client.deployments(environment=environment, status="DEPLOYED", limit=20)
    current = deployed[0] if deployed else None
    plan = RollbackPlan(
        subscription=client.sub.name, environment=environment,
        current_deployment=current and current.get("code"), current_build=current and current.get("buildCode"),
        target_deployment=None, target_build=target_build_code, strategy=strategy, database_update_mode="NONE",
        executed=False, hint="",
    )
    if current:
        b, _ = safe(lambda: client.build(current["buildCode"]))
        plan.current_branch = b and b.get("branch")
    if not target_build_code:
        target = None
        prev_code = current and current.get("previousDeploymentCode")
        if prev_code:
            target, _ = safe(lambda: client.deployment(prev_code))
        if not target or not target.get("buildCode") or target.get("buildCode") == plan.current_build:
            target = next((d for d in deployed[1:] if d.get("buildCode") and d.get("buildCode") != plan.current_build), None)
        if not target:
            plan.hint = "No previous successful deployment with a different build found; pass target_build_code explicitly."
            return plan
        plan.target_deployment, plan.target_build = target.get("code"), target.get("buildCode")
    tb, err = safe(lambda: client.build(plan.target_build))
    if err or not tb:
        plan.hint = f"Target build {plan.target_build} cannot be read: {err}"
        return plan
    plan.target_branch = tb.get("branch")
    if (tb.get("status") or "").upper() != "SUCCESS":
        plan.hint = f"Target build {plan.target_build} has status {tb.get('status')}; it cannot be deployed."
        return plan
    running = active_deployment(client, environment)
    if running:
        plan.hint = f"Deployment {running.get('code')} is {status_of(running)} on {environment}; wait or cancel before rolling back."
        return plan
    if not confirm:
        plan.hint = (
            f"Plan: redeploy build {plan.target_build} ({plan.target_branch}) to {environment} with NONE/{strategy}, "
            f"replacing {plan.current_build} ({plan.current_branch}). Re-run with confirm=true to execute."
        )
        return plan
    resp = client.create_deployment(build_code=plan.target_build, environment=environment, database_update_mode="NONE", strategy=strategy)
    plan.executed, plan.new_deployment_code = True, resp.get("code")
    plan.hint = f"Rollback deployment {plan.new_deployment_code} started; poll with ccv2portal_wait_for_deployment."
    return plan


# ----------------------------------------------------------------- diagnosis

CATEGORIES: list[tuple[str, re.Pattern, str]] = [
    ("branch-not-found", re.compile(r"(couldn't find remote ref|Remote branch .* not found|fatal: .*branch)", re.I), "The branch or tag does not exist in the repository connected to the subscription."),
    ("compile-error", re.compile(r"(error: cannot find symbol|error: .*does not exist|Compilation failed|\.java:\d+: error)", re.I), "Java compilation failed; fix the reported file/line."),
    ("test-failure", re.compile(r"(Tests run: .*Failures: [1-9]|Tests run: .*Errors: [1-9]|junit.*FAILED|Test .* FAILED)", re.I), "Unit tests failed in the build; run them locally against the same branch."),
    ("spring-context", re.compile(r"(BeanCreationException|NoSuchBeanDefinitionException|UnsatisfiedDependencyException|Error creating bean)", re.I), "Spring context failed to start; check bean definitions in the extensions listed."),
    ("out-of-memory", re.compile(r"(OutOfMemoryError|Java heap space|GC overhead limit)", re.I), "Build ran out of memory; look for runaway tasks or raise build memory via SAP."),
    ("manifest", re.compile(r"(manifest\.json|Invalid manifest|extension .* not found|Unknown extension)", re.I), "manifest.json references something the build cannot resolve."),
    ("dependency", re.compile(r"(Could not resolve|Could not find artifact|unresolved dependency|npm ERR!|ERESOLVE)", re.I), "A dependency could not be resolved (Maven/npm); check versions and registries."),
    ("transient", re.compile(r"(host not found|Connection reset|Read timed out|UnknownHostException|503 Service Unavailable)", re.I), "Looks transient (network/infrastructure); retry the build before investigating further."),
]


@tool(
    name="ccv2portal_diagnose_build_failure",
    description="Explain why a build failed: reads progress and the log, classifies the first error (branch-not-found, compile-error, test-failure, spring-context, out-of-memory, manifest, dependency, transient, unknown) and returns the relevant log lines and a suggestion.",
    annotations=READ_ONLY,
)
def ccv2portal_diagnose_build_failure(build_code: str, subscription: str | None = None) -> Diagnosis:
    client = registry.client(subscription)
    build = build_info(client.build(build_code))
    progress, _ = safe(lambda: client.build_progress(build_code))
    status = (progress or {}).get("buildStatus") or build.status
    portal_error = (progress or {}).get("errorMessage")
    diag = Diagnosis(
        subscription=client.sub.name, build_code=build_code, status=status, branch=build.branch,
        category="unknown", summary="", portal_error=portal_error, evidence=[], suggestion="",
    )
    if (status or "").upper() not in {"FAIL", "UNKNOWN"}:
        diag.summary = f"Build is {status}; nothing to diagnose."
        diag.category = "not-failed"
        return diag
    zip_bytes, err = safe(lambda: client.build_logs_zip(build_code))
    text = ""
    if zip_bytes:
        entries = buildlog.extract_entries(zip_bytes)
        if entries:
            diag.log_entry, text = entries[0]
    else:
        diag.evidence.append(f"log unavailable: {err}")
    haystack = "\n".join(filter(None, [portal_error, text]))
    for name, rx, suggestion in CATEGORIES:
        m = rx.search(haystack)
        if m:
            diag.category, diag.suggestion = name, suggestion
            if text:
                line_no = text[: max(0, m.start() - len(portal_error or "") - 1)].count("\n")
                lines = text.splitlines()
                lo, hi = max(0, line_no - 5), min(len(lines), line_no + 30)
                diag.evidence.extend(f"{j + 1}: {lines[j]}" for j in range(lo, hi))
            break
    if diag.category == "unknown":
        window = buildlog.first_error(text) if text else None
        diag.evidence.extend(window or (text.splitlines()[-40:] if text else []))
        diag.suggestion = "No known pattern matched; read the evidence lines, or use ccv2portal_get_build_log with mode='grep'."
    diag.summary = f"Build {build_code} ({build.branch}) failed: {diag.category}." + (f" Portal says: {portal_error}" if portal_error else "")
    return diag


# ----------------------------------------------------------------- checklist

def _age_days(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() / 86400
    except ValueError:
        return None


@tool(
    name="ccv2portal_pre_deploy_checklist",
    description="Everything to know before deploying: environment availability and hibernation, permissions, running deployment, build status and branch, allowed modes, last backup age, maintenance mode on endpoints, certificate expiry, pending blue-green decision. ready=true when nothing failed.",
    annotations=READ_ONLY,
)
def ccv2portal_pre_deploy_checklist(
    environment: str, build_code: str | None = None, database_update_mode: str = "NONE", strategy: str = "ROLLING_UPDATE",
    subscription: str | None = None,
) -> ChecklistResult:
    client = registry.client(subscription)
    require(client.sub, environment, PERM_READ)
    checks: list[Check] = []
    env_raw = env_or_fail(client, environment)
    prod = is_production(env_raw, environment)

    st = (env_raw.get("status") or "").upper()
    checks.append(Check(name="environment status", level="ok" if st in {"AVAILABLE", "READY_FOR_DEPLOYMENT"} else "fail", detail=f"{environment} is {st or 'unknown'} ({env_raw.get('type')})"))
    hib = (env_raw.get("hibernationStatus") or "").upper()
    if hib and hib not in {"ACTIVE", "AWAKE", "NONE", "NOT_HIBERNATED"}:
        checks.append(Check(name="hibernation", level="fail", detail=f"hibernationStatus={hib}"))

    perms = client.sub.perms_for(environment)
    need = {PERM_DEPLOY}
    missing = need - perms
    checks.append(Check(name="agent permissions", level="ok" if not missing else "fail", detail=f"granted {sorted(perms)}" + (f", missing {sorted(missing)}" if missing else "")))

    running, err = safe(lambda: active_deployment(client, environment))
    checks.append(Check(name="running deployment", level="unknown" if err else ("fail" if running else "ok"), detail=err or (f"{running.get('code')} is {status_of(running)}" if running else "none")))

    if build_code:
        b, err = safe(lambda: client.build(build_code))
        if err:
            checks.append(Check(name="build", level="fail", detail=err))
        else:
            ok = (b.get("status") or "").upper() == "SUCCESS"
            checks.append(Check(name="build", level="ok" if ok else "fail", detail=f"{build_code} {b.get('status')} branch={b.get('branch')} created {b.get('buildStartTimestamp')} by {b.get('createdBy')}"))
            if prod and b.get("branch") and not re.search(r"(release|main|master|hotfix)", b["branch"], re.I):
                checks.append(Check(name="branch naming", level="warn", detail=f"branch {b['branch']!r} does not look like a release branch for production"))

    db_allowed, st_allowed = allowed_modes(client, environment)
    dm, sg = database_update_mode.upper(), strategy.upper()
    if db_allowed is None and st_allowed is None:
        checks.append(Check(name="allowed modes", level="unknown", detail="deploymentmodes response not understood or unavailable"))
    else:
        bad = []
        if db_allowed is not None and dm not in db_allowed:
            bad.append(f"{dm} not in {sorted(db_allowed)}")
        if st_allowed is not None and sg not in st_allowed:
            bad.append(f"{sg} not in {sorted(st_allowed)}")
        checks.append(Check(name="allowed modes", level="fail" if bad else "ok", detail="; ".join(bad) or f"{dm}/{sg} allowed"))
    if dm == "INITIALIZE":
        checks.append(Check(name="database mode", level="warn" if not prod else "fail", detail="INITIALIZE wipes the database"))
    if sg == "RECREATE":
        checks.append(Check(name="strategy", level="warn", detail="RECREATE causes downtime"))

    if PERM_BACKUP_RESTORE in perms or PERM_READ in perms:
        backups, err = safe(lambda: client.backups(environment))
        if err:
            checks.append(Check(name="last backup", level="unknown", detail=err))
        else:
            done = [b for b in backups if (b.get("status") or "").upper() in {"SUCCESS", "COMPLETED", "DONE", "AVAILABLE"} or b.get("canBeRestored")]
            newest = max(done, key=lambda b: b.get("createdTimestamp") or "", default=None)
            age = _age_days(newest and newest.get("createdTimestamp"))
            if newest is None:
                checks.append(Check(name="last backup", level="warn" if prod else "ok", detail="no restorable backup found"))
            else:
                level = "ok" if age is not None and age <= 1 else ("warn" if prod else "ok")
                checks.append(Check(name="last backup", level=level, detail=f"{newest.get('databackupCode')} ({newest.get('databackupType')}) {age:.1f} days old" if age is not None else str(newest.get("databackupCode"))))

    endpoints, err = safe(lambda: client.endpoints(environment))
    if err:
        checks.append(Check(name="maintenance mode", level="unknown", detail=err))
    else:
        on = [e.get("code") for e in endpoints if e.get("maintenanceMode")]
        checks.append(Check(name="maintenance mode", level="warn" if on else "ok", detail=f"on for {on}" if on else f"off on all {len(endpoints)} endpoints"))

    certs, err = safe(client.certificates)
    if not err:
        soon = [f"{c.get('name') or c.get('code')} ({_age_days(c.get('expirationDate')) and -_age_days(c.get('expirationDate')):.0f}d)"
                for c in certs if any((e.get("environmentCode") == environment) for e in c.get("endpoints") or [])
                and _age_days(c.get("expirationDate")) is not None and -_age_days(c.get("expirationDate")) < 30]
        checks.append(Check(name="certificates", level="warn" if soon else "ok", detail=f"expiring within 30 days: {soon}" if soon else "none expiring within 30 days"))

    pending, err = safe(lambda: [d for d in client.deployments(environment=environment, limit=5) if (d.get("strategy") or "").upper() == "GREEN" and status_of(d) == "DEPLOYED" and d.get("expireAtTimestamp")])
    if pending:
        checks.append(Check(name="blue-green", level="warn", detail=f"green deployment {pending[0].get('code')} awaits a decision (expires {pending[0].get('expireAtTimestamp')})"))

    ready = not any(c.level == "fail" for c in checks)
    return ChecklistResult(subscription=client.sub.name, environment=environment, build_code=build_code, ready=ready, checks=checks)


# ----------------------------------------------------------- property diff

@tool(
    name="ccv2portal_compare_properties",
    description="Compare the values of the given property keys between two environments for one service aspect (the API reads one key at a time, so pass the keys you care about). Secret-looking values are compared but masked.",
    annotations=READ_ONLY,
)
def ccv2portal_compare_properties(
    environment_a: str, environment_b: str, keys: list[str], service: str = "hcs_common", subscription: str | None = None
) -> PropertyDiffResult:
    client = registry.client(subscription)
    require(client.sub, environment_a, PERM_READ)
    require(client.sub, environment_b, PERM_READ)
    diffs = []
    for key in keys:
        a, ea = safe(lambda: client.property(environment_a, service, key))
        b, eb = safe(lambda: client.property(environment_b, service, key))
        va = a.get("value") if a else None
        vb = b.get("value") if b else None
        masked = bool(SECRET_KEY.search(key))
        diffs.append(PropertyDiff(
            key=key, a=("***" if masked and va else va), b=("***" if masked and vb else vb), same=(va == vb and not ea and not eb),
            masked=masked, error_a=ea, error_b=eb,
        ))
    return PropertyDiffResult(
        subscription=client.sub.name, service=service, environment_a=environment_a, environment_b=environment_b,
        diffs=diffs, different=sum(1 for d in diffs if not d.same),
    )


# -------------------------------------------------------------- timeline

@tool(
    name="ccv2portal_release_timeline",
    description="Builds and deployments of the last `days` days merged into one chronological list (optionally filtered by environment and/or branch substring). Useful for release notes and incident timelines; pair timestamps with the log tools.",
    annotations=READ_ONLY,
)
def ccv2portal_release_timeline(
    days: int = 7, environment: str | None = None, branch: str | None = None, limit: int = 100, subscription: str | None = None
) -> TimelineResult:
    client = registry.client(subscription)
    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    since_iso = since.isoformat(timespec="seconds")
    if environment:
        require(client.sub, environment, PERM_READ)
    builds = {b["code"]: b for b in client.builds(limit=min(200, limit * 2))}
    events: list[TimelineEvent] = []
    for b in builds.values():
        at = b.get("buildStartTimestamp")
        if at and at < since_iso:
            continue
        if branch and branch.lower() not in (b.get("branch") or "").lower():
            continue
        events.append(TimelineEvent(at=at, kind="build", code=b["code"], status=b.get("status"), branch=b.get("branch"), by=b.get("createdBy"), detail=b.get("name")))
    for d in client.deployments(environment=environment, limit=min(200, limit * 2)):
        at = d.get("createdTimestamp")
        if at and at < since_iso:
            continue
        env = d.get("environmentCode")
        if env and not client.sub.allows(env, PERM_READ):
            continue
        b = builds.get(d.get("buildCode") or "")
        if branch and not (b and branch.lower() in (b.get("branch") or "").lower()):
            continue
        events.append(TimelineEvent(
            at=at, kind="deployment", code=d["code"], status=d.get("status"), environment=env, build_code=d.get("buildCode"),
            branch=b and b.get("branch"), by=d.get("createdBy"),
            detail=f"{d.get('databaseUpdateMode')}/{d.get('strategy')}" + (f", deployed {d.get('deployedTimestamp')}" if d.get("deployedTimestamp") else ""),
        ))
    events.sort(key=lambda e: e.at or "", reverse=True)
    return TimelineResult(subscription=client.sub.name, since=since_iso, events=events[:limit])




__all__ = [n for n in dir() if n.startswith("ccv2portal_")]
