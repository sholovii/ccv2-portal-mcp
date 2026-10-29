"""Data behind the status page: configuration, live environment state and server facts.

Everything here is read-only. Live portal data is cached briefly per subscription so
that a page auto-refreshing every minute does not hammer the API.
"""

from __future__ import annotations

import json
import os
import platform
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .app import Registry, active_deployment, is_production, latest_deployed, safe, status_of
from .client import PortalClient
from .config import ALL_PERMISSIONS, ConfigError, SubscriptionConfig

STARTED_AT = time.time()


def _mask(value: str, keep: int = 6) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "…" + "*" * 4


def config_source(settings) -> str:
    if settings.ccv2_config:
        return f"JSON file {settings.ccv2_config}"
    if settings.ccv2_subscriptions.strip():
        return "environment variables (CCV2_SUBSCRIPTIONS)"
    return "environment variables (flat CCV2_* set)"


def subscription_config(sub: SubscriptionConfig, is_default: bool) -> dict[str, Any]:
    return {
        "name": sub.name,
        "code": sub.code,
        "is_default": is_default,
        "base_url": sub.base_url,
        "token_url": sub.token_url,
        "client_id": _mask(sub.client_id),
        "client_secret_set": bool(sub.client_secret),
        "resource_set": bool(sub.resource),
        "default_permissions": sorted(sub.default_permissions),
        "permissions": {k: sorted(v) for k, v in sorted(sub.permissions.items())},
    }


def environment_state(sub: SubscriptionConfig, client: PortalClient, code: str, raw: dict) -> dict[str, Any]:
    perms = sorted(sub.perms_for(code))
    state: dict[str, Any] = {
        "code": code,
        "name": raw.get("name"),
        "type": raw.get("type"),
        "production": is_production(raw, code),
        "status": raw.get("status"),
        "deployment_status": raw.get("deploymentStatus"),
        "hibernation_status": raw.get("hibernationStatus"),
        "permissions": perms,
        "permissions_from_default": code not in sub.permissions,
        "readable": "read" in perms,
        "deployed": None,
        "active_deployment": None,
        "error": None,
    }
    if not state["readable"]:
        return state
    dep, err = safe(lambda: latest_deployed(client, code))
    if err:
        state["error"] = err
        return state
    if dep:
        deployed = {
            "deployment_code": dep.get("code"),
            "build_code": dep.get("buildCode"),
            "strategy": dep.get("strategy"),
            "database_update_mode": dep.get("databaseUpdateMode"),
            "deployed_at": dep.get("deployedTimestamp") or dep.get("createdTimestamp"),
            "deployed_by": dep.get("createdBy"),
            "branch": None,
            "build_name": None,
        }
        if deployed["build_code"]:
            b, _ = safe(lambda: client.build(deployed["build_code"]))
            if b:
                deployed["branch"], deployed["build_name"] = b.get("branch"), b.get("name")
        state["deployed"] = deployed
    running, _ = safe(lambda: active_deployment(client, code))
    if running:
        state["active_deployment"] = {
            "deployment_code": running.get("code"),
            "status": status_of(running),
            "build_code": running.get("buildCode"),
        }
    return state


def subscription_live(registry: Registry, sub: SubscriptionConfig, *, fresh: bool) -> dict[str, Any]:
    """Auth probe plus every environment the portal lists, with the agent's permissions."""
    client = registry.client(sub.name)
    out: dict[str, Any] = {"auth": client.auth_check(), "environments": [], "unknown_environments": [], "error": None}
    if not out["auth"]["ok"]:
        out["error"] = "authentication failed; environments not fetched"
        return out
    envs, err = safe(lambda: registry.environments(client, fresh=fresh))
    if err:
        out["error"] = err
        return out
    for code in sorted(envs):
        out["environments"].append(environment_state(sub, client, code, envs[code]))
    out["unknown_environments"] = sorted(set(sub.permissions) - set(envs))
    return out


def audit_tail(path: str, lines: int) -> dict[str, Any]:
    if not path:
        return {"path": None, "note": "AUDIT_LOG_PATH not set; write calls are logged to stderr only", "entries": []}
    p = Path(path)
    if not p.exists():
        return {"path": str(p), "note": "no file yet (no write tool has been called)", "entries": []}
    try:
        raw = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return {"path": str(p), "note": f"cannot read: {exc}", "entries": []}
    entries = []
    for line in raw[-lines:]:
        try:
            entries.append(json.loads(line))
        except ValueError:
            entries.append({"raw": line})
    entries.reverse()
    return {"path": str(p), "note": None, "total_lines": len(raw), "entries": entries}


def tools_summary(tools: list[Any]) -> dict[str, Any]:
    """`tools` are the MCPTool objects from MCPServer.list_tools()."""
    items = []
    for t in tools:
        ann = getattr(t, "annotations", None)
        # the SDK names these read_only_hint / destructive_hint; older releases used camelCase
        read_only = bool(getattr(ann, "read_only_hint", None) or getattr(ann, "readOnlyHint", False)) if ann else False
        destructive = bool(getattr(ann, "destructive_hint", None) or getattr(ann, "destructiveHint", False)) if ann else False
        items.append({"name": t.name, "read_only": read_only, "destructive": destructive})
    items.sort(key=lambda i: (not i["read_only"], i["name"]))
    return {
        "count": len(items),
        "read_only": sum(1 for i in items if i["read_only"]),
        "write": sum(1 for i in items if not i["read_only"]),
        "items": items,
    }


class StatusCollector:
    """Builds the status document; caches the live part per subscription."""

    def __init__(self, registry: Registry, *, served_by: str) -> None:
        self.registry = registry
        self.served_by = served_by  # "mcp" (same process as the MCP server) or "ui" (standalone)
        self._lock = threading.Lock()
        self._live: dict[str, tuple[float, dict[str, Any]]] = {}
        self._fetching: dict[str, threading.Lock] = {}

    def server_facts(self) -> dict[str, Any]:
        s = self.registry.settings
        http = s.mcp_transport != "stdio"
        return {
            "name": "ccv2-portal-mcp",
            "version": __version__,
            "transport": s.mcp_transport,
            "mcp_endpoint": f"http://{s.mcp_host}:{s.mcp_port}/mcp" if http else None,
            "served_by": self.served_by,
            "running": self.served_by == "mcp",
            "note": (
                "This page is served by the MCP server process."
                if self.served_by == "mcp"
                else "Standalone status page. With the stdio transport the MCP server runs inside the AI client's process; this page shares its configuration but cannot see whether that process is alive."
            ),
            "pid": os.getpid(),
            "started_at": datetime.fromtimestamp(STARTED_AT, timezone.utc).isoformat(timespec="seconds"),
            "uptime_sec": round(time.time() - STARTED_AT),
            "python": platform.python_version(),
            "platform": f"{platform.system()} {platform.machine()}",
            "config_source": config_source(s),
            "settings": {
                "http_timeout_sec": s.http_timeout_sec,
                "wait_max_sec": s.wait_max_sec,
                "wait_poll_sec": s.wait_poll_sec,
                "build_log_max_lines": s.build_log_max_lines,
                "retry_429_max": s.retry_429_max,
                "token_early_refresh_sec": s.token_early_refresh_sec,
                "ui_cache_sec": s.ui_cache_sec,
            },
            "audit_log_path": s.audit_log_path or None,
            "permissions_catalogue": sorted(ALL_PERMISSIONS),
        }

    def _cached(self, name: str, *, refresh: bool) -> dict[str, Any] | None:
        ttl = self.registry.settings.ui_cache_sec
        with self._lock:
            hit = self._live.get(name)
            if hit and not refresh and time.time() - hit[0] < ttl:
                data = dict(hit[1])
                data["cached"] = True
                data["age_sec"] = round(time.time() - hit[0])
                return data
            return None

    def _live_for(self, sub: SubscriptionConfig, *, refresh: bool) -> dict[str, Any]:
        hit = self._cached(sub.name, refresh=refresh)
        if hit is not None:
            return hit
        with self._lock:
            fetch_lock = self._fetching.setdefault(sub.name, threading.Lock())
        # Two page loads at once (two tabs, a reload during a fetch) share one round of
        # portal calls: the second waits for the first and then takes the fresh cache entry.
        with fetch_lock:
            if not refresh:
                hit = self._cached(sub.name, refresh=False)
                if hit is not None:
                    return hit
            data = subscription_live(self.registry, sub, fresh=refresh)
            data["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with self._lock:
                self._live[sub.name] = (time.time(), data)
        out = dict(data)
        out["cached"] = False
        out["age_sec"] = 0
        return out

    def collect(self, *, tools: list[Any] | None = None, live: bool = True, refresh: bool = False) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "server": self.server_facts(),
            "tools": tools_summary(tools or []),
            "config_error": None,
            "subscriptions": [],
            "audit": audit_tail(self.registry.settings.audit_log_path, self.registry.settings.ui_audit_lines),
        }
        try:
            cfg = self.registry.config
        except ConfigError as exc:
            doc["config_error"] = str(exc)
            return doc
        for name, sub in cfg.subscriptions.items():
            entry = subscription_config(sub, is_default=(name == cfg.default_subscription or len(cfg.subscriptions) == 1))
            entry["live"] = self._live_for(sub, refresh=refresh) if live else None
            doc["subscriptions"].append(entry)
        return doc
