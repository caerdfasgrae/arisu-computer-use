"""Adversarial & contract tests for Tahap S9 hardening, UIPI integrity, focus predicate & schema."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

from mcp.types import ImageContent, TextContent

from arisu_cua import server
from arisu_cua.display.monitors import MonitorInfo
from arisu_cua.input import window_mgr
from arisu_cua.perception.uia_client import ElementBox, get_uia_client
from arisu_cua.safety.killswitch import WindowBlockedByPolicyError


def _sample_monitor() -> MonitorInfo:
    return MonitorInfo(
        index=1,
        device=r"\\.\DISPLAY1",
        left=0,
        top=0,
        width=1920,
        height=1080,
        dpi_x=96,
        dpi_y=96,
        scale_pct=100,
        is_primary=True,
    )


class _FakeHud:
    def send_event(self, *_a, **_kw):
        return {"ok": True}

    def send_cursor_cue(self, *_a, **_kw):
        return {"ok": True}

    def get_hud_status(self):
        return {"running": False, "hud_rect": None}

    def consume_approval(self, **_kw):
        return {"ok": False, "approved": False}

    def request_approval(self, **_kw):
        return {"ok": True, "request_id": "req_s9"}


def _patch_basic_server(monkeypatch, *, fg_title: str = "Untitled - Notepad"):
    mon = _sample_monitor()
    sleeps: list[float] = []
    kb_calls: list[dict] = []
    audit_logs: list[dict] = []

    server._BREAKER.reset()
    monkeypatch.setattr(server._CORNER_FAILSAFE, "start", lambda: None)
    monkeypatch.setattr(server, "enumerate_monitors", lambda: [mon])
    monkeypatch.setattr(server, "get_foreground_window_info", lambda: (101, (0, 0, 1920, 1080)))
    monkeypatch.setattr(server, "get_foreground_window_title", lambda: fg_title)
    monkeypatch.setattr(server, "get_cursor_pos", lambda: (100, 100))
    monkeypatch.setattr(server, "get_window_title_at_point", lambda _x, _y: fg_title)
    monkeypatch.setattr(server, "verify_pre_click_patch", lambda *_a, **_kw: {"checked": False})
    monkeypatch.setattr(server, "get_hud_client", lambda: _FakeHud())
    monkeypatch.setattr(server, "_AUDIT", SimpleNamespace(log=lambda **kw: audit_logs.append(kw)))
    monkeypatch.setattr(
        server,
        "execute_mouse_action",
        lambda **kw: {"action": kw["action"], "px": [kw["px_x"], kw["px_y"]]},
    )

    def fake_kb(**kw):
        kb_calls.append(kw)
        return {"action": kw["action"], "chars": len(kw.get("text") or "")}

    monkeypatch.setattr(server, "execute_keyboard_action", fake_kb)
    monkeypatch.setattr(
        server,
        "execute_scroll_action",
        lambda **kw: {"direction": kw["direction"], "amount": kw["amount"]},
    )
    monkeypatch.setattr(server, "abortable_sleep", lambda s: sleeps.append(round(float(s), 3)))

    def fake_capture_payload(monitor="active", mode="grid", fmt="webp", **_kw):
        meta = {
            "ok": True,
            "mode": mode,
            "monitor": 1,
            "foreground_window_title": fg_title,
        }
        return [
            TextContent(type="text", text=json.dumps(meta)),
            ImageContent(type="image", data="AAAA", mimeType="image/webp"),
        ]

    monkeypatch.setattr(server, "_capture_payload", fake_capture_payload)
    return sleeps, kb_calls, audit_logs


def test_input_tools_enforce_window_denylist_b1(monkeypatch) -> None:
    _patch_basic_server(monkeypatch, fg_title="1Password - Vault")

    mouse_res = json.loads(server.arisu_cu_mouse_action(action="click", x=200, y=200)[0].text)
    kb_res = json.loads(
        server.arisu_cu_keyboard_action(action="type", expected_window="1Password", text="secret")[
            0
        ].text
    )
    scroll_res = json.loads(server.arisu_cu_scroll_action(x=200, y=200)[0].text)
    win_res = json.loads(
        server.arisu_cu_window_action(action="focus", title_or_hwnd=".env - Notepad")[0].text
    )

    for res in (mouse_res, kb_res, scroll_res, win_res):
        assert res["ok"] is False
        assert res["code"] == "WINDOW_BLOCKED_BY_POLICY"


def test_unapproved_sensitive_action_does_not_trip_repeat_loop_b2(monkeypatch) -> None:
    _patch_basic_server(monkeypatch, fg_title="Bank Portal - Chrome")
    uia = get_uia_client()
    uia.update_som_registry(
        [
            ElementBox(
                element_id=1,
                name="Transfer Now",
                control_type="ButtonControl",
                automation_id="btnPay",
                bbox_px=(100, 100, 200, 140),
                center_px=(150, 120),
                bbox_norm=(100, 100, 200, 140),
                center_norm=(150, 120),
                is_password=False,
                monitor_index=1,
                window_title="Bank Portal - Chrome",
            )
        ]
    )

    for _ in range(6):
        res = json.loads(server.arisu_cu_mouse_action(action="click", element_id=1)[0].text)
        assert res["ok"] is False
        assert res["code"] == "GUARDRAIL_APPROVAL_REQUIRED"

        hk = json.loads(
            server.arisu_cu_keyboard_action(
                action="hotkey", expected_window="Bank", keys=["ctrl", "w"]
            )[0].text
        )
        assert hk["ok"] is False
        assert hk["code"] == "GUARDRAIL_APPROVAL_REQUIRED"

    assert server._BREAKER.cap.count == 0
    uia.clear_som_registry()


def test_inline_verify_failure_preserves_executed_ok_verdict_b3(monkeypatch) -> None:
    _patch_basic_server(monkeypatch, fg_title="YouTube - Google Chrome")
    monkeypatch.setattr(
        server,
        "_capture_payload",
        lambda **_kw: (_ for _ in ()).throw(WindowBlockedByPolicyError("1Password", "1password")),
    )

    items = server.arisu_cu_mouse_action(action="click", x=400, y=300, verify_mode="grid")
    assert len(items) == 1
    data = json.loads(items[0].text)
    assert data["ok"] is True
    assert data["executed"] is True
    assert data["inline_verify"]["ok"] is False
    assert data["inline_verify"]["code"] == "WINDOW_BLOCKED_BY_POLICY"
    assert "INLINE_VERIFY_FAILED" in data["warnings"]


def test_password_check_fails_closed_on_uia_timeout_b4(monkeypatch) -> None:
    _sleeps, kb_calls, audit_logs = _patch_basic_server(monkeypatch, fg_title="Login - Chrome")
    monkeypatch.setattr(
        server,
        "get_uia_client",
        lambda: SimpleNamespace(
            check_focused_password=lambda: None,
            await_focused_control=lambda **_kw: None,
        ),
    )

    res_kb = json.loads(
        server.arisu_cu_keyboard_action(action="type", expected_window="Login", text="MySecret!")[
            0
        ].text
    )
    assert res_kb["ok"] is False
    assert res_kb["code"] == "PASSWORD_CHECK_UNAVAILABLE"
    assert kb_calls == []
    assert audit_logs[-1]["redact_text"] is True

    res_mouse = json.loads(
        server.arisu_cu_mouse_action(action="click", x=300, y=300, type_text="MySecret!")[0].text
    )
    assert res_mouse["ok"] is False
    assert res_mouse["code"] == "PASSWORD_CHECK_UNAVAILABLE"
    assert kb_calls == []
    assert audit_logs[-1]["redact_text"] is True
    assert audit_logs[-1]["args"]["type_text"] == "[REDACTED]"


def test_uipi_elevated_window_preflight_blocks_non_approvable_s9_2(monkeypatch) -> None:
    assert window_mgr.get_self_integrity_rid() == window_mgr.INTEGRITY_MEDIUM

    _patch_basic_server(monkeypatch, fg_title="Task Manager")
    monkeypatch.setattr(server, "get_hwnd_at_point", lambda _x, _y: 5555)
    monkeypatch.setattr(server, "get_pid_for_hwnd", lambda _hwnd: 9999)
    monkeypatch.setattr(server, "is_pid_elevated_over_self", lambda _pid: True)
    monkeypatch.setattr(server, "get_pid_integrity_rid", lambda _pid: window_mgr.INTEGRITY_HIGH)
    monkeypatch.setattr(server, "get_self_integrity_rid", lambda: window_mgr.INTEGRITY_MEDIUM)

    err = json.loads(server.arisu_cu_mouse_action(action="click", x=725, y=275)[0].text)
    assert err["ok"] is False
    assert err["code"] == "INPUT_INTEGRITY_BLOCKED"
    assert "0x3000" in err["error"]
    assert "run_command" in err["error"]


def test_window_action_restore_inline_verify_and_uipi_warnings_s9_3(monkeypatch) -> None:
    _patch_basic_server(monkeypatch, fg_title="Task Manager")

    monkeypatch.setattr(
        server,
        "execute_window_action",
        lambda **kw: {
            "action": kw["action"],
            "hwnd": 5555,
            "title": "Task Manager",
            "monitor": 2,
            "focused": False,
            "is_maximized": False,
        },
    )
    max_items = server.arisu_cu_window_action(action="maximize", title_or_hwnd="Task Manager")
    assert len(max_items) == 2 and isinstance(max_items[1], ImageContent)
    max_meta = json.loads(max_items[0].text)
    assert max_meta["ok"] is True
    assert max_meta["executed"] is True
    assert "FOCUS_NOT_CONFIRMED" in max_meta["warnings"]
    assert "MAXIMIZE_BLOCKED_BY_UIPI" in max_meta["warnings"]
    assert max_meta["inline_verify"]["mode"] == "grid"

    min_items = server.arisu_cu_window_action(action="minimize", title_or_hwnd="Task Manager")
    assert len(min_items) == 1 and isinstance(min_items[0], TextContent)


def test_bounded_uia_focus_predicate_and_invalid_param_combo_s9_4(monkeypatch) -> None:
    _sleeps, kb_calls, _audits = _patch_basic_server(monkeypatch, fg_title="Running applications")

    bad = json.loads(
        server.arisu_cu_mouse_action(action="move", x=200, y=200, type_text="oops")[0].text
    )
    assert bad["ok"] is False
    assert bad["code"] == "INVALID_PARAM_COMBO"

    poll_calls = 0

    def fake_await(**_kw):
        nonlocal poll_calls
        poll_calls += 1
        return {
            "ok": True,
            "is_password": False,
            "is_editable": True,
            "has_focus": True,
            "window_title": "Search",
            "control_type": "EditControl",
        }

    monkeypatch.setattr(
        server,
        "get_uia_client",
        lambda: SimpleNamespace(
            await_focused_control=fake_await,
            check_focused_password=lambda: False,
            clear_som_registry=lambda: None,
        ),
    )
    ok_res = server.arisu_cu_mouse_action(
        action="click", x=330, y=975, type_text="Task Manager", press_enter=True
    )
    meta = json.loads(ok_res[0].text)
    assert meta["ok"] is True
    assert poll_calls == 1
    assert meta["typed_chars"] == 12
    assert meta["pressed_enter"] is True
    assert kb_calls[0]["action"] == "type"


def test_skill_md_cheatsheet_matches_live_mcp_tool_schemas_s9_5() -> None:
    skill_path = (
        Path(__file__).resolve().parents[1]
        / "plugin"
        / "skills"
        / "arisu-computer-use"
        / "SKILL.md"
    )
    content = skill_path.read_text(encoding="utf-8")
    live_tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}

    parsed_sigs: dict[str, set[str]] = {}
    for line in content.splitlines():
        m = re.match(r"^- `(arisu_cu_[a-z_]+)\(([^)]*)\)`", line.strip())
        if not m:
            continue
        t_name, raw_args = m.group(1), m.group(2).strip()
        if not raw_args:
            parsed_sigs[t_name] = set()
        else:
            parsed_sigs[t_name] = {
                part.strip().split("=")[0].strip() for part in raw_args.split(",") if part.strip()
            }

    assert set(parsed_sigs.keys()) == set(live_tools.keys())
    for t_name, tool_obj in live_tools.items():
        schema_props = set(tool_obj.parameters.get("properties", {}).keys())
        assert parsed_sigs[t_name] == schema_props
