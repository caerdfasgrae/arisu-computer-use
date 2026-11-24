"""Integration tests for the S1 arisu-cua FastMCP Stdio server."""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _schema_has_forbidden_keys(node) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        if node.get("additionalProperties") is False:
            found.append("additionalProperties=false")
        if "$ref" in node:
            found.append("$ref")
        if "const" in node:
            found.append("const")
        for v in node.values():
            found.extend(_schema_has_forbidden_keys(v))
    elif isinstance(node, list):
        for item in node:
            found.extend(_schema_has_forbidden_keys(item))
    return found


def test_stdio_server_tools_and_lifecycle() -> None:
    async def _run() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "arisu_cua.server"],
        )
        async with (
            stdio_client(params) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            listed = await session.list_tools()
            tools = {t.name: t for t in listed.tools}

            expected_tools = {
                "arisu_cu_get_system_context",
                "arisu_cu_capture_screen",
                "arisu_cu_inspect_ui_tree",
                "arisu_cu_mouse_action",
                "arisu_cu_keyboard_action",
                "arisu_cu_scroll_action",
                "arisu_cu_window_action",
                "arisu_cu_browser_action",
                "arisu_cu_wait_and_verify",
                "arisu_cu_session_control",
            }
            assert set(tools.keys()) == expected_tools
            assert len(tools) == 10

            for name in expected_tools:
                t = tools[name]
                assert t.annotations is not None
                assert t.inputSchema.get("title") == f"{name}Arguments"
                assert _schema_has_forbidden_keys(t.inputSchema) == []

            assert tools["arisu_cu_get_system_context"].annotations.readOnlyHint is True
            assert tools["arisu_cu_inspect_ui_tree"].annotations.readOnlyHint is True
            assert tools["arisu_cu_mouse_action"].annotations.destructiveHint is True
            assert "expected_window" in (
                tools["arisu_cu_keyboard_action"].inputSchema.get("required") or []
            )

            start_res = await session.call_tool(
                "arisu_cu_session_control", {"action": "start", "task_label": "pytest-s2"}
            )
            start_data = json.loads(start_res.content[0].text)
            assert start_data["ok"] is True
            assert start_data["active"] is True

            ctx_res = await session.call_tool("arisu_cu_get_system_context", {})
            ctx_data = json.loads(ctx_res.content[0].text)
            assert ctx_data["ok"] is True
            assert "foreground_window_title" in ctx_data
            assert len(ctx_data["monitors"]) >= 1

            uia_res = await session.call_tool(
                "arisu_cu_inspect_ui_tree",
                {"max_depth": 2, "max_items": 10, "interactive_only": True},
            )
            uia_data = json.loads(uia_res.content[0].text)
            assert uia_data["ok"] is True
            assert isinstance(uia_data["elements"], list)

            missing_el_res = await session.call_tool(
                "arisu_cu_mouse_action",
                {"action": "click", "element_id": 999},
            )
            missing_el_data = json.loads(missing_el_res.content[0].text)
            assert missing_el_data["ok"] is False
            assert missing_el_data["code"] == "ELEMENT_NOT_FOUND"

            mismatch_res = await session.call_tool(
                "arisu_cu_keyboard_action",
                {
                    "action": "type",
                    "expected_window": "__nonexistent_window_title_xyz__",
                    "text": "should be blocked",
                },
            )
            mismatch_data = json.loads(mismatch_res.content[0].text)
            assert mismatch_data["ok"] is False
            assert mismatch_data["code"] == "WINDOW_MISMATCH_BLOCKED"

            stop_res = await session.call_tool("arisu_cu_session_control", {"action": "stop"})
            stop_data = json.loads(stop_res.content[0].text)
            assert stop_data["ok"] is True
            assert stop_data["aborted"] is True

            wait_res = await session.call_tool("arisu_cu_wait_and_verify", {"seconds": 1.0})
            wait_data = json.loads(wait_res.content[0].text)
            assert wait_data["ok"] is False
            assert wait_data["code"] == "ABORTED_BY_USER"

    asyncio.run(_run())
