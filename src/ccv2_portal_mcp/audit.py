"""Append-only audit of every write the server performs on a subscription.

One JSON object per line: when, subscription, tool, environment, arguments (secret-looking
values masked), and the portal's answer or the error. Written to AUDIT_LOG_PATH when set
and always to the process log, so a container without a volume still leaves a trace.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("ccv2_portal_mcp.audit")

SECRET_KEY = re.compile(r"(secret|password|passwd|token|credential|private[_-]?key|apikey|api[_-]key)", re.I)


def mask_value(key: str, value: Any) -> Any:
    if isinstance(value, str) and SECRET_KEY.search(key or ""):
        return "***" if value else value
    return value


VALUE_FIELDS = {"value", "previous"}


def mask(obj: Any, key: str = "") -> Any:
    if isinstance(obj, dict):
        # A property payload: {"key": "db.password", "value": "..."} -> mask the value fields.
        secret_prop = isinstance(obj.get("key"), str) and bool(SECRET_KEY.search(obj["key"]))
        return {
            k: ("***" if secret_prop and k in VALUE_FIELDS and v else mask(v, k))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [mask(v, key) for v in obj]
    return mask_value(key, obj)


class Audit:
    def __init__(self, path: str | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.Lock()

    def record(
        self,
        *,
        subscription: str,
        tool: str,
        environment: str | None,
        args: dict[str, Any],
        result: Any = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "subscription": subscription,
            "tool": tool,
            "environment": environment,
            "args": mask(args),
            "result": mask(result) if result is not None else None,
            "error": error,
        }
        line = json.dumps(entry, default=str)
        (log.error if error else log.info)("AUDIT %s", line)
        if self._path:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        return entry
