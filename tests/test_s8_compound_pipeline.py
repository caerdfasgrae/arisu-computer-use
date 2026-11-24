"""Tests for Tahap S8 engine-default auto-verify, compound click+type+enter, and payload diet."""

from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace

from mcp.types import ImageContent, TextContent
from PIL import Image

from arisu_cua import config, server
from arisu_cua.display.capture import prepare_captured_image
from arisu_cua.display.monitors import MonitorInfo


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


class _MockHud:
    def send_event(self, *_a, **_kw):
        return {"ok": True}

    def send_cursor_cue(self, *_a, **_kw):
        return {"ok": True}

    def get_hud_status(self):
        return {"running": False, "hud_rect": None}

    def consume_approval(self, **_kw):
        return {"ok": False, "approved": False}

    def request_approval(self, **_kw):
        return {"ok": True, "request_id": "req_test"}


def _patch_server_env(
    monkeypatch,
    *,
    password_focused: bool = False,
):
    mon = _sample_monitor()
    sleeps: list[float] = []
    capture_calls: list[dict] = []
    kb_calls: list[dict] = []
    audit_logs: list[dict] = []

    monkeypatch.setattr(server, "enumerate_monitors", lambda: [mon])
    monkeypatch.setattr(server, "get_foreground_window_info", lambda: (101, (0, 0, 1920, 1080)))
    monkeypatch.setattr(server, "get_foreground_window_title", lambda: "YouTube - Google Chrome")
    monkeypatch.setattr(server, "get_cursor_pos", lambda: (100, 100))
    monkeypatch.setattr(
        server, "get_window_title_at_point", lambda _x, _y: "YouTube - Google Chrome"
    )
    monkeypatch.setattr(server, "verify_expected_window", lambda *_a, **_kw: None)
    monkeypatch.setattr(server, "verify_pre_click_patch", lambda *_a, **_kw: {"checked": False})
    monkeypatch.setattr(server, "get_hud_client", lambda: _MockHud())
    monkeypatch.setattr(
        server,
        "get_uia_client",
        lambda: SimpleNamespace(check_focused_password=lambda: password_focused),
    )
    monkeypatch.setattr(server._BREAKER, "check_and_record", lambda *_a, **_kw: None)
    monkeypatch.setattr(server, "_AUDIT", SimpleNamespace(log=lambda **kw: audit_logs.append(kw)))
    monkeypatch.setattr(
        server,
        "execute_mouse_action",
        lambda **kw: {"action": kw["action"], "px": [kw["px_x"], kw["px_y"]]},
    )

    def fake_kb(**kw):
        kb_calls.append(kw)
        text = kw.get("text") or ""
        return {"action": kw["action"], "chars": len(text)}

    monkeypatch.setattr(server, "execute_keyboard_action", fake_kb)
    monkeypatch.setattr(
        server,
        "execute_scroll_action",
        lambda **kw: {"direction": kw["direction"], "amount": kw["amount"]},
    )
    monkeypatch.setattr(server, "abortable_sleep", lambda sec: sleeps.append(round(float(sec), 3)))

    def fake_capture_payload(monitor="active", mode="clean", fmt="webp", **_kw):
        capture_calls.append({"monitor": monitor, "mode": mode, "fmt": fmt})
        meta = {
            "ok": True,
            "mode": mode,
            "monitor": 1,
            "foreground_window_title": "YouTube - Google Chrome",
            "vision_notice": "Read media_0.webp",
        }
        return [
            TextContent(type="text", text=json.dumps(meta)),
            ImageContent(type="image", data="AAAA", mimeType="image/webp"),
        ]

    monkeypatch.setattr(server, "_capture_payload", fake_capture_payload)
    return sleeps, capture_calls, kb_calls, audit_logs


def test_default_verify_mode_is_grid_on_mutating_tools(monkeypatch) -> None:
    sleeps, capture_calls, _kb_calls, _audits = _patch_server_env(monkeypatch)

    mouse_res = server.arisu_cu_mouse_action(action="click", x=400, y=150)
    kb_res = server.arisu_cu_keyboard_action(action="type", expected_window="Chrome", text="hello")
    scroll_res = server.arisu_cu_scroll_action(x=500, y=500, direction="down", amount=3)

    for res in (mouse_res, kb_res, scroll_res):
        assert len(res) == 2
        assert isinstance(res[0], TextContent)
        assert isinstance(res[1], ImageContent)
        meta = json.loads(res[0].text)
        assert meta["ok"] is True
        assert meta["inline_verify"]["mode"] == "grid"

    assert [c["mode"] for c in capture_calls] == ["grid", "grid", "grid"]
    assert sleeps == [0.6, 0.6, 0.6]


def test_compound_mouse_click_type_text_and_press_enter(monkeypatch) -> None:
    sleeps, capture_calls, kb_calls, _audits = _patch_server_env(monkeypatch)

    res = server.arisu_cu_mouse_action(
        action="click",
        x=500,
        y=200,
        type_text="Gemini 4 Neon",
        press_enter=True,
    )

    assert len(res) == 2
    assert isinstance(res[0], TextContent)
    assert isinstance(res[1], ImageContent)
    meta = json.loads(res[0].text)
    assert meta["ok"] is True
    assert meta["typed_chars"] == 13
    assert meta["pressed_enter"] is True
    assert meta["inline_verify"]["mode"] == "grid"

    assert len(kb_calls) == 2
    assert kb_calls[0] == {"action": "type", "text": "Gemini 4 Neon", "typing_mode": "natural"}
    assert kb_calls[1] == {"action": "press", "keys": ["enter"]}
    assert sleeps == [0.15, 0.12, 1.5]
    assert [c["mode"] for c in capture_calls] == ["grid"]


def test_compound_mouse_click_blocks_unapproved_password_field(monkeypatch) -> None:
    _sleeps, capture_calls, kb_calls, audit_logs = _patch_server_env(
        monkeypatch, password_focused=True
    )

    res = server.arisu_cu_mouse_action(
        action="click",
        x=320,
        y=420,
        type_text="SuperSecret123",
        press_enter=True,
    )

    assert len(res) == 1
    assert isinstance(res[0], TextContent)
    err = json.loads(res[0].text)
    assert err["ok"] is False
    assert err["code"] == "GUARDRAIL_APPROVAL_REQUIRED"
    assert kb_calls == []
    assert capture_calls == []
    assert audit_logs[-1]["redact_text"] is True
    assert audit_logs[-1]["args"]["type_text"] == "[REDACTED]"


def test_payload_diet_config_defaults_1024_q72() -> None:
    assert config.DEFAULT_MAX_SIZE == 1024
    assert config.DEFAULT_IMAGE_QUALITY == 72

    img = Image.new("RGB", (1920, 1080), "#1C3F7A")
    shot = prepare_captured_image(img, monitor_index=1, mode="clean")
    assert (shot.width, shot.height) == (1024, 576)
    assert shot.mime_type == "image/webp"
    assert Image.open(BytesIO(shot.data)).size == (1024, 576)
