"""Process entry point. Importing the tool modules registers them on the shared MCPServer."""

from __future__ import annotations

import logging
import sys

from .app import PermissionDenied, ConfirmationRequired, mcp, registry  # noqa: F401 (re-exported)
from .config import ConfigError
from .tools_read import *  # noqa: F401,F403
from .tools_write import *  # noqa: F401,F403
from .tools_helpers import *  # noqa: F401,F403

log = logging.getLogger("ccv2_portal_mcp")


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = registry.settings
    try:
        cfg = registry.config
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        sys.exit(2)
    log.info(
        "ccv2-portal-mcp starting: transport=%s audit=%s subscriptions=%s",
        s.mcp_transport, s.audit_log_path or "stderr",
        {n: sub.redacted()["permissions"] for n, sub in cfg.subscriptions.items()},
    )
    if s.mcp_transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http", host=s.mcp_host, port=s.mcp_port)


if __name__ == "__main__":
    main()
