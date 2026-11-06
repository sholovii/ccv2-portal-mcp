"""Long waits: progress notifications reach the client, the context stays out of the schema."""

from __future__ import annotations

import pytest

from ccv2_portal_mcp import app, server
from tests.conftest import SUB


def _build_progression(configured, statuses):
    calls = iter(statuses)
    state = {"last": statuses[-1]}

    def handler(request):
        st, pct = next(calls, state["last"])
        return 200, {"buildStatus": st, "percentage": pct}

    configured.route("GET", f"/v2/subscriptions/{SUB}/builds/b1/progress", handler=handler)


def test_wait_tools_are_async_with_context_outside_the_schema():
    tool = server.mcp._tool_manager.get_tool("ccv2portal_wait_for_build")
    assert tool.is_async and tool.context_kwarg == "ctx"
    props = tool.parameters["properties"]
    assert "ctx" not in props and {"build_code", "timeout_sec", "poll_sec", "subscription"} <= set(props)
    # sync tools are untouched
    plain = server.mcp._tool_manager.get_tool("ccv2portal_list_builds")
    assert not plain.is_async and plain.context_kwarg is None


def test_sync_wait_reports_progress_through_contextvar(configured):
    _build_progression(configured, [("BUILDING", 28), ("BUILDING", 61), ("SUCCESS", 100)])
    seen = []
    token = app.PROGRESS.set(lambda p, total, msg: seen.append((p, total, msg)))
    try:
        out = server.ccv2portal_wait_for_build("b1", timeout_sec=5, poll_sec=2)
    finally:
        app.PROGRESS.reset(token)
    assert out.done and out.succeeded
    assert [p for p, _, _ in seen] == [28, 61, 100]
    assert all(total == 100 for _, total, _ in seen)
    assert "build b1: BUILDING 28%" in seen[0][2] and "SUCCESS 100%" in seen[-1][2]


def test_progress_failures_do_not_break_the_wait(configured):
    _build_progression(configured, [("SUCCESS", 100)])

    def boom(*_):
        raise RuntimeError("client gone")

    token = app.PROGRESS.set(boom)
    try:
        out = server.ccv2portal_wait_for_build("b1")
    finally:
        app.PROGRESS.reset(token)
    assert out.done


@pytest.mark.anyio
async def test_async_wrapper_forwards_progress_to_the_mcp_context(configured):
    _build_progression(configured, [("BUILDING", 50), ("SUCCESS", 100)])
    tool = server.mcp._tool_manager.get_tool("ccv2portal_wait_for_build")
    notified = []

    class FakeCtx:
        async def report_progress(self, progress, total=None, message=None):
            notified.append((progress, total, message))

    out = await tool.fn("b1", timeout_sec=5, poll_sec=2, ctx=FakeCtx())
    assert out.done and out.status == "SUCCESS"
    assert [p for p, _, _ in notified] == [50, 100]


def test_timeout_is_clamped_to_the_ceiling_and_hint_mentions_it(configured, client):
    _build_progression(configured, [("BUILDING", 10)])
    server.registry.settings.wait_max_sec = 4
    server.registry.settings.wait_default_sec = 2
    out = server.ccv2portal_wait_for_build("b1", timeout_sec=999, poll_sec=2)
    assert not out.done and out.waited_sec <= 4.5
    assert "of the 4s requested" in out.hint
    out = server.ccv2portal_wait_for_build("b1", poll_sec=2)
    assert "timeout_sec=4" in out.hint  # the default window is shorter than the ceiling, so the hint says how to wait longer
