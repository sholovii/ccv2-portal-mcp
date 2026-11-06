"""Typical durations from the subscription's own history, for wait hints.

A minimal storefront builds in ten minutes, a full suite in forty; a fixed number in a hint
would be wrong for one of them. These helpers read the last successful builds or
deployments and derive a suggested timeout for a single wait call.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Any

from .auth import AuthError
from .client import PortalClient, PortalError

SAMPLES = 10
MARGIN = 1.25


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _summary(durations: list[float], wait_max_sec: int) -> dict[str, Any] | None:
    durations = [d for d in durations if d > 0][:SAMPLES]
    if not durations:
        return None
    longest = max(durations)
    suggested = min(wait_max_sec, int(math.ceil(longest * MARGIN / 60.0)) * 60)
    return {
        "samples": len(durations),
        "min_sec": int(min(durations)),
        "median_sec": int(statistics.median(durations)),
        "max_sec": int(longest),
        "suggested_timeout_sec": suggested,
    }


def typical_build_duration(client: PortalClient, wait_max_sec: int) -> dict[str, Any] | None:
    """From the last successful builds (portal start/end timestamps); None without history."""
    try:
        builds = client.builds(limit=30)
    except (PortalError, AuthError):
        return None
    durations = []
    for b in builds:
        if (b.get("status") or "").upper() != "SUCCESS":
            continue
        start, end = _ts(b.get("buildStartTimestamp")), _ts(b.get("buildEndTimestamp"))
        if start and end:
            durations.append((end - start).total_seconds())
    return _summary(durations, wait_max_sec)


def typical_deployment_duration(client: PortalClient, wait_max_sec: int, environment: str | None = None) -> dict[str, Any] | None:
    """From recent deployments that reached DEPLOYED (created -> deployed); None without history.

    The portal marks superseded deployments UNDEPLOYED, so no status filter: any deployment with
    a deployedTimestamp counts, which keeps more than the single live one as a sample.
    """
    try:
        deployments = client.deployments(environment=environment, limit=3 * SAMPLES)
    except (PortalError, AuthError):
        return None
    durations = []
    for d in deployments:
        start, end = _ts(d.get("createdTimestamp")), _ts(d.get("deployedTimestamp"))
        if start and end:
            durations.append((end - start).total_seconds())
    return _summary(durations, wait_max_sec)


def _minutes(sec: int) -> str:
    return f"{max(1, round(sec / 60))} min"


def describe(summary: dict[str, Any] | None, *, kind: str, wait_tool: str, wait_max_sec: int) -> str:
    """One sentence for a hint: observed range and the single wait call that covers it."""
    if not summary:
        return f"No {kind} history to estimate from; one {wait_tool} call can wait up to timeout_sec={wait_max_sec}."
    lo, hi = _minutes(summary["min_sec"]), _minutes(summary["max_sec"])
    span = lo if lo == hi else f"{lo} to {hi}"
    return (
        f"Recent {kind}s here took {span} ({summary['samples']} sample{'s' if summary['samples'] != 1 else ''}); "
        f"one {wait_tool} call with timeout_sec={summary['suggested_timeout_sec']} covers it."
    )
