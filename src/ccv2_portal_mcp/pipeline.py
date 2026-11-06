"""Server-side pipeline: build -> (backup) -> deploy, run in a background thread.

An agent calls ccv2portal_build_and_deploy once, gets a pipeline id back immediately, and
watches it with ccv2portal_wait_for_pipeline (progress notifications) or
ccv2portal_get_pipeline. Every write inside the pipeline goes through the ordinary write
tools, so the audit log records each step exactly as if the agent had called it.

State lives in this process: under stdio that is the AI client's session, in the container
it is the server. A restart loses in-flight pipelines; get_pipeline says so.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .app import (
    BUILD_TERMINAL,
    DEPLOYMENT_TERMINAL,
    DESTRUCTIVE,
    READ_ONLY,
    active_deployment,
    audited,
    backup_state,
    env_or_fail,
    is_production,
    registry,
    report_progress,
    require,
    require_any,
    status_of,
    tool,
)
from .client import PortalError
from .config import ConfigError, PERM_BACKUP_RESTORE, PERM_BUILD, PERM_DEPLOY
from .models import PipelineList, PipelineState, PipelineStep

log = logging.getLogger(__name__)

STEP_NAMES = ("build", "wait_build", "backup", "wait_backup", "wait_environment", "deploy", "wait_deployment")
TERMINAL = {"succeeded", "failed", "cancelled"}
HISTORY = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Pipeline:
    def __init__(self, **spec: Any) -> None:
        self.id = f"pl-{secrets.token_hex(4)}"
        self.subscription: str = spec["subscription"]
        self.environment: str = spec["environment"]
        self.branch: str = spec["branch"]
        self.build_name: str | None = spec.get("build_name")
        self.database_update_mode: str = spec["database_update_mode"]
        self.strategy: str = spec["strategy"]
        self.backup_first: bool = spec["backup_first"]
        self.confirm: bool = spec["confirm"]
        self.created_at = _now()
        self.finished_at: str | None = None
        self.status = "running"
        self.progress = 0
        self.message = "starting"
        self.cancel_requested = False
        self.build_code: str | None = None
        self.backup_code: str | None = None
        self.deployment_code: str | None = None
        self.diagnosis: dict | None = None
        self.error: str | None = None
        self.steps: list[PipelineStep] = [PipelineStep(name=n, status="pending") for n in STEP_NAMES]
        if not self.backup_first:
            for st in self.steps:
                if st.name in ("backup", "wait_backup"):
                    st.status = "skipped"
        self.lock = threading.RLock()

    # ----------------------------------------------------------- state helpers

    def step(self, name: str) -> PipelineStep:
        return next(s for s in self.steps if s.name == name)

    def begin(self, name: str, message: str) -> None:
        with self.lock:
            st = self.step(name)
            st.status, st.started_at = "running", _now()
            self.message = message

    def end(self, name: str, *, code: str | None = None, detail: str | None = None) -> None:
        with self.lock:
            st = self.step(name)
            st.status, st.finished_at, st.code, st.detail = "done", _now(), code, detail

    def fail(self, name: str, error: str) -> None:
        with self.lock:
            st = self.step(name)
            st.status, st.finished_at, st.error = "failed", _now(), error
            self.status, self.error, self.finished_at = "failed", error, _now()
            self.message = f"{name} failed: {error}"
            for other in self.steps:
                if other.status == "pending":
                    other.status = "skipped"

    def cancelled(self, name: str) -> None:
        with self.lock:
            self.step(name).status = "cancelled"
            self.status, self.finished_at = "cancelled", _now()
            self.message = f"cancelled before {name}"
            for other in self.steps:
                if other.status == "pending":
                    other.status = "skipped"

    def done(self) -> None:
        with self.lock:
            self.status, self.finished_at, self.progress = "succeeded", _now(), 100
            self.message = f"build {self.build_code} deployed to {self.environment} as {self.deployment_code}"

    def set_progress(self, progress: int, message: str) -> None:
        with self.lock:
            self.progress, self.message = progress, message

    def state(self) -> PipelineState:
        with self.lock:
            hint = {
                "running": f"Pipeline running: {self.message}. Watch with ccv2portal_wait_for_pipeline or ccv2portal_get_pipeline.",
                "succeeded": f"Pipeline finished: {self.message}.",
                "failed": f"Pipeline failed: {self.error}. Steps already executed stay as they are (a started build or deployment keeps running in the portal).",
                "cancelled": f"Pipeline cancelled: {self.message}. Steps already started keep running in the portal.",
            }[self.status]
            return PipelineState(
                id=self.id, subscription=self.subscription, environment=self.environment, branch=self.branch,
                build_name=self.build_name, database_update_mode=self.database_update_mode, strategy=self.strategy,
                backup_first=self.backup_first, status=self.status, progress=self.progress, message=self.message,
                created_at=self.created_at, finished_at=self.finished_at, build_code=self.build_code,
                backup_code=self.backup_code, deployment_code=self.deployment_code, diagnosis=self.diagnosis,
                error=self.error, steps=[s.model_copy() for s in self.steps], hint=hint,
            )


class PipelineStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Pipeline] = {}
        self._threads: dict[str, threading.Thread] = {}

    def add(self, p: Pipeline, runner: Callable[[Pipeline], None]) -> None:
        with self._lock:
            self._items[p.id] = p
            while len(self._items) > HISTORY:
                oldest = next(iter(self._items))
                if self._items[oldest].status in TERMINAL:
                    self._items.pop(oldest)
                    self._threads.pop(oldest, None)
                else:
                    break
            t = threading.Thread(target=runner, args=(p,), name=p.id, daemon=True)
            self._threads[p.id] = t
        t.start()

    def get(self, pipeline_id: str) -> Pipeline:
        with self._lock:
            p = self._items.get(pipeline_id)
        if p is None:
            raise PortalError(
                f"unknown pipeline {pipeline_id!r}. Pipelines live in the server process and do not survive a restart; "
                "use ccv2portal_list_builds / ccv2portal_list_deployments to see what ran."
            )
        return p

    def recent(self, limit: int = 20) -> list[Pipeline]:
        with self._lock:
            items = list(self._items.values())
        return list(reversed(items))[:limit]

    def join(self, pipeline_id: str, timeout: float | None = None) -> None:
        """Block until the pipeline thread exits (tests and shutdown)."""
        with self._lock:
            t = self._threads.get(pipeline_id)
        if t:
            t.join(timeout)

    def snapshot(self, limit: int = 10) -> list[dict]:
        return [p.state().model_dump() for p in self.recent(limit)]


store = PipelineStore()


# ------------------------------------------------------------------- runner

def _poll(p: Pipeline, fetch: Callable[[], dict], is_done: Callable[[dict], bool], step: str, lo: int, hi: int, label: str) -> dict:
    """Poll `fetch` until is_done, mapping the portal percentage into [lo, hi] of pipeline progress."""
    s = registry.settings
    start = time.monotonic()
    while True:
        raw = fetch()
        pct = raw.get("percentage")
        status = status_of(raw) or raw.get("status") or "queued"
        scaled = lo + int((hi - lo) * (pct or 0) / 100)
        p.set_progress(scaled, f"{label}: {status} {pct if pct is not None else '?'}%")
        if is_done(raw):
            return raw
        if time.monotonic() - start > s.wait_max_sec:
            raise PortalError(f"{label} still {status} after {s.wait_max_sec}s; giving up on the pipeline (the portal operation continues)")
        if p.cancel_requested:
            raise _Cancelled(step)
        time.sleep(max(2, s.wait_poll_sec))


def _wait_environment_free(p: Pipeline, client, running: dict) -> None:
    """Poll until no deployment is active on the target; a newly started one is waited for too."""
    s = registry.settings
    start = time.monotonic()
    while running:
        code = running.get("code")
        _poll(
            p, lambda: client.deployment_progress(code), lambda r: status_of(r) in DEPLOYMENT_TERMINAL,
            "wait_environment", p.progress, p.progress, f"deployment {code} already running on {p.environment}",
        )
        running = active_deployment(client, p.environment)
        if running and time.monotonic() - start > s.wait_max_sec:
            raise PortalError(f"{p.environment} still busy with deployment {running.get('code')} after {s.wait_max_sec}s")


class _Cancelled(Exception):
    def __init__(self, step: str) -> None:
        super().__init__(step)
        self.step = step


def run_pipeline(p: Pipeline) -> None:
    from . import tools_helpers, tools_write  # late import: those modules import app, not this one

    client = registry.client(p.subscription)
    sub = p.subscription
    step = "build"
    try:
        # 1. build
        if p.cancel_requested:
            raise _Cancelled("build")
        p.begin("build", f"creating build from {p.branch}")
        res = tools_write.ccv2portal_create_build(p.branch, name=p.build_name, subscription=sub)
        p.build_code = res.code
        p.end("build", code=res.code)

        # 2. wait for the build
        step = "wait_build"
        p.begin("wait_build", f"waiting for build {p.build_code}")
        raw = _poll(p, lambda: client.build_progress(p.build_code), lambda r: status_of(r) in BUILD_TERMINAL, "wait_build", 0, 50, f"build {p.build_code}")
        if status_of(raw) != "SUCCESS":
            try:
                p.diagnosis = tools_helpers.ccv2portal_diagnose_build_failure(p.build_code, subscription=sub).model_dump()
            except Exception as exc:  # diagnosis is best effort
                log.info("pipeline %s: diagnosis failed: %s", p.id, exc)
            raise PortalError(f"build {p.build_code} finished with {status_of(raw)}" + (f": {p.diagnosis['summary']}" if p.diagnosis else ""))
        p.end("wait_build", code=p.build_code, detail="SUCCESS")

        # 3. optional backup
        if p.backup_first:
            step = "backup"
            if p.cancel_requested:
                raise _Cancelled("backup")
            p.begin("backup", "creating pre-deploy backup")
            res = tools_write.ccv2portal_create_backup(p.environment, f"pre-deploy {p.build_code} ({p.branch})", "QUICK", subscription=sub)
            p.backup_code = res.code
            p.end("backup", code=res.code)
            step = "wait_backup"
            p.begin("wait_backup", f"waiting for backup {p.backup_code}")
            raw = _poll(p, lambda: client.backup(p.environment, p.backup_code), lambda r: backup_state(r) != "running", "wait_backup", 50, 60, f"backup {p.backup_code}")
            if backup_state(raw) != "done":
                raise PortalError(f"pre-deploy backup {p.backup_code} ended as {raw.get('status')}; deployment not started")
            p.end("wait_backup", code=p.backup_code, detail=raw.get("status"))

        # 4. let a deployment already running on the target finish (another session or a
        #    colleague may have started one while the build ran)
        step = "wait_environment"
        running = active_deployment(client, p.environment)
        if running:
            p.begin("wait_environment", f"deployment {running.get('code')} is already running on {p.environment}; waiting for it")
            _wait_environment_free(p, client, running)
            p.end("wait_environment", code=running.get("code"), detail="finished")
        else:
            p.step("wait_environment").status = "skipped"

        # 5. deploy
        step = "deploy"
        if p.cancel_requested:
            raise _Cancelled("deploy")
        p.begin("deploy", f"deploying build {p.build_code} to {p.environment}")
        res = tools_write.ccv2portal_create_deployment(
            p.build_code, p.environment, database_update_mode=p.database_update_mode, strategy=p.strategy,
            confirm=p.confirm, backup_first=False, subscription=sub,
        )
        p.deployment_code = res.code
        p.end("deploy", code=res.code)

        # 6. wait for the deployment
        step = "wait_deployment"
        p.begin("wait_deployment", f"waiting for deployment {p.deployment_code}")
        raw = _poll(p, lambda: client.deployment_progress(p.deployment_code), lambda r: status_of(r) in DEPLOYMENT_TERMINAL, "wait_deployment", 60, 100, f"deployment {p.deployment_code}")
        if status_of(raw) != "DEPLOYED":
            raise PortalError(f"deployment {p.deployment_code} finished with {status_of(raw)}")
        p.end("wait_deployment", code=p.deployment_code, detail="DEPLOYED")
        p.done()
    except _Cancelled as exc:
        p.cancelled(exc.step)
    except Exception as exc:  # any failure ends the pipeline; the record carries the reason
        p.fail(step, str(exc))
    finally:
        registry.audit.record(
            subscription=sub, tool="ccv2portal_build_and_deploy.finished", environment=p.environment,
            args={"pipeline": p.id}, result={"status": p.status, "build_code": p.build_code, "backup_code": p.backup_code, "deployment_code": p.deployment_code},
            error=p.error,
        )


# -------------------------------------------------------------------- tools

@tool(
    name="ccv2portal_build_and_deploy",
    description=(
        "Run build -> (optional quick backup) -> deploy -> wait as one server-side pipeline and return a pipeline id "
        "immediately. Validates permissions, modes and confirmation up front; needs 'build' and 'deploy' on the environment "
        "(and 'backup_restore' with backup_first), confirm=true for production, INITIALIZE or RECREATE. A deployment already "
        "running on the target is waited for, not a reason to refuse. Watch it with "
        "ccv2portal_wait_for_pipeline (progress notifications) or ccv2portal_get_pipeline. Each step is audited like a direct call."
    ),
    annotations=DESTRUCTIVE,
)
@audited("ccv2portal_build_and_deploy")
def ccv2portal_build_and_deploy(
    branch: str,
    environment: str,
    database_update_mode: str = "NONE",
    strategy: str = "ROLLING_UPDATE",
    backup_first: bool = False,
    build_name: str | None = None,
    confirm: bool = False,
    subscription: str | None = None,
) -> PipelineState:
    from .tools_write import DB_MODES, STRATEGIES, _confirm, allowed_modes

    client = registry.client(subscription)
    if not branch.strip():
        raise ConfigError("branch must not be empty")
    db_mode, strat = database_update_mode.upper(), strategy.upper()
    if db_mode not in DB_MODES:
        raise ConfigError(f"database_update_mode must be one of {sorted(DB_MODES)}")
    if strat not in STRATEGIES:
        raise ConfigError(f"strategy must be one of {sorted(STRATEGIES)}")
    require_any(client.sub, PERM_BUILD)
    require(client.sub, environment, PERM_DEPLOY)
    if backup_first:
        require(client.sub, environment, PERM_BACKUP_RESTORE)
    env_raw = env_or_fail(client, environment)
    prod = is_production(env_raw, environment)
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
        _confirm(confirm, f"Pipeline would build {branch} and deploy it to {environment} with {db_mode}/{strat}: " + "; ".join(reasons) + ".")

    p = Pipeline(
        subscription=client.sub.name, environment=environment, branch=branch, build_name=build_name,
        database_update_mode=db_mode, strategy=strat, backup_first=backup_first, confirm=confirm,
    )
    store.add(p, run_pipeline)
    return p.state()


@tool(
    name="ccv2portal_get_pipeline",
    description="State of a pipeline started by ccv2portal_build_and_deploy: status, progress, current step, codes of the build, backup and deployment, and the failure diagnosis if the build failed.",
    annotations=READ_ONLY,
)
def ccv2portal_get_pipeline(pipeline_id: str) -> PipelineState:
    return store.get(pipeline_id).state()


@tool(
    name="ccv2portal_list_pipelines",
    description="Recent pipelines of this server process, newest first (in-flight pipelines do not survive a server restart).",
    annotations=READ_ONLY,
)
def ccv2portal_list_pipelines(limit: int = 20) -> PipelineList:
    items = [p.state() for p in store.recent(limit)]
    return PipelineList(pipelines=items, running=sum(1 for p in items if p.status == "running"))


@tool(
    name="ccv2portal_wait_for_pipeline",
    description="Block until a pipeline finishes or timeout_sec elapses (default WAIT_DEFAULT_SEC, up to WAIT_MAX_SEC = one hour), reporting progress on every poll. Returns the pipeline state; done when status is not 'running'.",
    annotations=READ_ONLY,
    progress=True,
)
def ccv2portal_wait_for_pipeline(pipeline_id: str, timeout_sec: int | None = None, poll_sec: int | None = None) -> PipelineState:
    from .tools_helpers import _again_hint, _wait_window

    p = store.get(pipeline_id)
    timeout, poll = _wait_window(timeout_sec, poll_sec)
    start = time.monotonic()
    while True:
        state = p.state()
        elapsed = time.monotonic() - start
        report_progress(state.progress, 100, f"pipeline {p.id}: {state.message} ({elapsed:.0f}s)")
        if state.status != "running":
            return state
        if elapsed + poll > timeout:
            state.hint = _again_hint(f"running ({state.message})", elapsed, timeout)
            return state
        time.sleep(poll)


@tool(
    name="ccv2portal_cancel_pipeline",
    description="Stop a running pipeline before its next step. A build or deployment already started in the portal keeps running (use ccv2portal_cancel_deployment for a deployment); the pipeline just stops advancing.",
    annotations=DESTRUCTIVE,
)
@audited("ccv2portal_cancel_pipeline", env_arg=None)
def ccv2portal_cancel_pipeline(pipeline_id: str, subscription: str | None = None) -> PipelineState:
    p = store.get(pipeline_id)
    with p.lock:
        if p.status != "running":
            raise PortalError(f"pipeline {pipeline_id} is already {p.status}")
        p.cancel_requested = True
        p.message = "cancellation requested; stopping before the next step"
    return p.state()


__all__ = [n for n in dir() if n.startswith("ccv2portal_")]
