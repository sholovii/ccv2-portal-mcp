"""Exercise the read-only tools against a real subscription configured in .env.

    uv run python scripts/smoke.py [--env d1] [--build <code>] [--deployment <code>]

Prints a compact summary per tool and the answers to the open questions from
docs/research.md (services path, pagination, 429s). Never writes anything.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from ccv2_portal_mcp import server
from ccv2_portal_mcp.auth import AuthError
from ccv2_portal_mcp.client import PortalError


FAILURES: list[str] = []


def step(title: str):
    print(f"\n=== {title}")


def attempt(title: str, fn):
    """Run one step; report a portal/auth error and continue with the next step."""
    step(title)
    try:
        return fn()
    except (PortalError, AuthError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        FAILURES.append(f"{title}: {exc}")
        return None
    except Exception as exc:  # a bug in the tool, not the portal: report and keep going
        print(f"CRASHED: {type(exc).__name__}: {exc}", file=sys.stderr)
        FAILURES.append(f"{title}: {type(exc).__name__}: {str(exc).splitlines()[0]}")
        return None


def show(obj, keys: list[str] | None = None, limit: int = 5):
    data = obj.model_dump() if hasattr(obj, "model_dump") else obj
    if keys:
        data = {k: data.get(k) for k in keys}
    text = json.dumps(data, indent=2, default=str)
    lines = text.splitlines()
    print("\n".join(lines[:80]) + ("\n..." if len(lines) > 80 else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", help="environment code to inspect (default: first readable)")
    ap.add_argument("--build", help="build code for get_build / get_build_log")
    ap.add_argument("--deployment", help="deployment code for get_deployment")
    args = ap.parse_args()

    try:
        step("list_subscriptions")
        subs = server.ccv2portal_list_subscriptions()
        show(subs)

        step("list_environments")
        t0 = time.monotonic()
        envs = server.ccv2portal_list_environments()
        print(f"({time.monotonic() - t0:.2f}s)")
        for e in envs.environments:
            print(f"  {e.code:6} {e.type or '':12} {e.status or '':10} deploy={e.deploymentStatus} hib={e.hibernationStatus} perms={e.permissions}")
        if not envs.environments:
            print("no readable environments; check CCV2_PERMISSIONS / technical user environments")
            return 1
        env = args.env or envs.environments[0].code

        step(f"get_environment {env}")
        detail = server.ccv2portal_get_environment(env)
        print("services_error:", detail.services_error)
        for s in detail.services:
            print(f"  {s.code:40} desired={s.desiredReplicas} available={s.availableReplicas} runnable={s.runnable}")
        print("latest_deployment:", detail.latest_deployment and detail.latest_deployment.model_dump(include={"code", "buildCode", "status", "strategy", "databaseUpdateMode", "createdTimestamp"}))
        print("latest_build:", detail.latest_build and detail.latest_build.model_dump(include={"code", "branch", "status", "buildVersion"}))
        print("scaling_error:", detail.scaling_error)
        if detail.scaling:
            print("scaling keys:", sorted(detail.scaling))

        modes = attempt("get_deployment_modes", server.ccv2portal_get_deployment_modes)
        if modes is not None:
            show(modes)

        step("list_builds (limit 5)")
        builds = server.ccv2portal_list_builds(limit=5)
        for b in builds.builds:
            print(f"  {b.code:12} {b.status or '':10} {b.branch or '':40} {b.buildStartTimestamp}")
        build_code = args.build or (builds.builds[0].code if builds.builds else None)

        t0 = time.monotonic()
        many = attempt("list_builds pagination (limit 150 -> expect two pages)", lambda: server.ccv2portal_list_builds(limit=150))
        if many is not None:
            print(f"got {len(many.builds)} builds in {time.monotonic() - t0:.2f}s")

        if build_code:
            step(f"get_build {build_code}")
            bd = server.ccv2portal_get_build(build_code)
            print("terminal:", bd.terminal, "progress:", bd.progress and bd.progress.model_dump(include={"buildStatus", "percentage", "numberOfTasks", "errorMessage"}))
            print("deployments from it:", [d.code for d in bd.deployments])

            t0 = time.monotonic()
            lg = attempt(f"get_build_log {build_code} (mode=errors)", lambda: server.ccv2portal_get_build_log(build_code))
            if lg is not None:
                print(f"({time.monotonic() - t0:.2f}s) entries={lg.entries} entry={lg.entry} total_lines={lg.total_lines} mode={lg.mode} returned={lg.returned_lines}")
                print("\n".join(lg.lines[:15]))

        step(f"list_deployments env={env} (limit 5)")
        deps = server.ccv2portal_list_deployments(environment=env, limit=5)
        for d in deps.deployments:
            print(f"  {d.code:12} {d.status or '':10} build={d.buildCode} {d.strategy}/{d.databaseUpdateMode} {d.createdTimestamp}")
        dep_code = args.deployment or (deps.deployments[0].code if deps.deployments else None)

        if dep_code:
            step(f"get_deployment {dep_code}")
            dd = server.ccv2portal_get_deployment(dep_code)
            print("terminal:", dd.terminal, "build branch:", dd.build and dd.build.branch)
            if dd.progress:
                print("status:", dd.progress.deploymentStatus, "pct:", dd.progress.percentage)
                for st in dd.progress.stages:
                    print(f"  stage {st.name!r:35} {st.status} failed={st.failed_steps}")

            w = attempt(f"wait_for_deployment {dep_code} (bounded)", lambda: server.ccv2portal_wait_for_deployment(dep_code, timeout_sec=5))
            if w is not None:
                show(w)

        for name, fn in (
            ("list_endpoints", lambda: server.ccv2portal_list_endpoints(env)),
            ("list_certificates", server.ccv2portal_list_certificates),
            ("list_backups", lambda: server.ccv2portal_list_backups(env)),
            ("list_restores", lambda: server.ccv2portal_list_restores(env)),
            ("get_scaling_options", lambda: server.ccv2portal_get_scaling_options(env)),
            ("list_scheduled_activities", lambda: server.ccv2portal_list_scheduled_activities(env)),
            ("whats_deployed", server.ccv2portal_whats_deployed),
            (f"release_timeline (7 days, env={env})", lambda: server.ccv2portal_release_timeline(days=7, environment=env)),
        ):
            r = attempt(name, fn)
            if r is not None:
                show(r)

        step("rapid polling: 10 progress calls back to back (429 check)")
        if build_code:
            client = server.registry.client(None)
            t0 = time.monotonic()
            for _ in range(10):
                client.build_progress(build_code)
            print(f"10 calls in {time.monotonic() - t0:.2f}s, no 429 raised")

        if FAILURES:
            print("\nFAILED STEPS:")
            for f in FAILURES:
                print(" -", f)
            return 1
        print("\nOK")
        return 0
    except (PortalError, AuthError) as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
