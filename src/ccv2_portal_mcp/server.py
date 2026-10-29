"""Process entry point. Importing the tool modules registers them on the shared MCPServer."""

from __future__ import annotations

import argparse
import logging
import sys

from .app import PermissionDenied, ConfirmationRequired, mcp, registry  # noqa: F401 (re-exported)
from .config import ConfigError
from .tools_read import *  # noqa: F401,F403
from .tools_write import *  # noqa: F401,F403
from .tools_helpers import *  # noqa: F401,F403

log = logging.getLogger("ccv2_portal_mcp")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="ccv2-portal-mcp", description="MCP server for the SAP Commerce Cloud Portal API.")
    sub = ap.add_subparsers(dest="command")
    ui = sub.add_parser("ui", help="serve only the status page (for stdio setups, where the MCP server has no HTTP port)")
    ui.add_argument("--host", default=None, help="bind address (default UI_HOST, 127.0.0.1)")
    ui.add_argument("--port", type=int, default=None, help="port (default UI_PORT, 8850)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    # The SDK installs a Rich handler when the MCPServer object is created; replace it with
    # plain lines (force=True), since Rich wraps long records badly in narrow terminals.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
    args = parse_args(argv)
    s = registry.settings
    try:
        cfg = registry.config
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        sys.exit(2)
    perms = {n: sub.redacted()["permissions"] for n, sub in cfg.subscriptions.items()}
    if args.command == "ui":
        from .ui import run_standalone

        logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per portal call is noise here
        log.info("ccv2-portal-mcp status page: subscriptions=%s", perms)
        run_standalone(args.host or s.ui_host, args.port or s.ui_port)
        return
    log.info("ccv2-portal-mcp starting: transport=%s audit=%s subscriptions=%s", s.mcp_transport, s.audit_log_path or "stderr", perms)
    if s.mcp_transport == "stdio":
        mcp.run(transport="stdio")
    else:
        from .ui import register_on_mcp

        register_on_mcp()
        log.info("status page on http://%s:%d/", s.mcp_host, s.mcp_port)
        mcp.run(transport="streamable-http", host=s.mcp_host, port=s.mcp_port)


if __name__ == "__main__":
    main()
