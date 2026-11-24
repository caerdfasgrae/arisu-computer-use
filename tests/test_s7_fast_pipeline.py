"""Tests for Tahap S7 hybrid som+grid overlay and inline post-action verification."""

from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace

from mcp.types import ImageContent, TextContent
from PIL import Image

from arisu_cua import server
from arisu_cua.display.capture import prepare_captured_image
from arisu_cua.display.monitors import MonitorInfo
from arisu_cua.perception.uia_client import ElementBox


def _sample_monitor() -> MonitorInfo:
    return MonitorInfo(
        index=1,
        device=r"\\.\DISPLAY1",
        left=0,
        top=0,
        width=800,
        height=600,
        dpi_x=96,
        dpi_y=96,
        scale_pct=100,
        is_primary=True,
    )


def test_som_mode_includes_background_coordinate_grid() -> None:
    base = Image.new("RGB", (800, 600), (24, 24, 24))

    som_no_els = prepare_captured_image(
        base, monitor_index=1, fmt="webp", mode="som", som_elements=[]
    )
    decoded_no_els = Image.open(BytesIO(som_no_els.data)).convert("RGB")
    # Center grid line at norm x=500 -> px 400 must differ from background (24,24,24)
    grid_px = decoded_no_els.getpixel((400, 300))
    assert grid_px != (24, 24, 24)

    box = ElementBox(
        element_id=1,
        name="Sign In",
        control_type="ButtonControl",
        automation_id="btn_1",
        bbox_px=(100, 100, 220, 160),
        center_px=(160, 130),
        bbox_norm=(125, 167, 275, 267),
        center_norm=(200, 217),
        is_password=False,
        monitor_index=1,
        window_title="Chrome",
    )
    som_with_els = prepare_captured_image(
        base, monitor_index=1, fmt="webp", mode="som", som_elements=[box]
    )
    decoded_with_els = Image.open(BytesIO(som_with_els.data)).convert("RGB")
    # Both SoM box border (around norm x=125 -> px 100, y=100) AND grid line at x=400 are present
    assert decoded_with_els.getpixel((400, 300)) != (24, 24, 24)
    assert decoded_with_els.getpixel((100, 100)) != (24, 24, 24)


def test_inline_verification_returns_text_and_image_in_one_call(monkeypatch) -> None:
    mon = _sample_monitor()
    sleeps: list[float] = []
    capture_calls: list[dict] = []

    class _FakeHud:
        def send_event(self, *_a, **_kw):
            return {"ok": True}

        def send_cursor_cue(self, *_a, **_kw):
            return {"ok": True}

        def get_hud_status(self):
            return {"running": False, "hud_rect": None}

    monkeypatch.setattr(server, "enumerate_monitors", lambda: [mon])
    monkeypatch.setattr(server, "get_foreground_window_info", lambda: (101, (0, 0, 800, 600)))
    monkeypatch.setattr(server, "get_foreground_window_title", lambda: "Untitled - Notepad")
    monkeypatch.setattr(server, "get_cursor_pos", lambda: (100, 100))
    monkeypatch.setattr(server, "get_window_title_at_point", lambda _x, _y: "Untitled - Notepad")
    monkeypatch.setattr(server, "verify_expected_window", lambda *_a, **_kw: None)
    monkeypatch.setattr(server, "verify_pre_click_patch", lambda *_a, **_kw: {"checked": False})
    monkeypatch.setattr(server, "get_hud_client", lambda: _FakeHud())
    monkeypatch.setattr(server._BREAKER, "check_and_record", lambda *_a, **_kw: None)
    monkeypatch.setattr(server, "_AUDIT", SimpleNamespace(log=lambda **_kw: None))
    monkeypatch.setattr(
        server, "execute_mouse_action", lambda **kw: {"action": kw["action"], "px": [100, 100]}
    )
    monkeypatch.setattr(
        server, "execute_keyboard_action", lambda **kw: {"action": kw["action"], "chars": 6}
    )
    monkeypatch.setattr(
        server,
        "execute_scroll_action",
        lambda **kw: {"direction": kw["direction"], "amount": kw["amount"]},
    )
    monkeypatch.setattr(server, "abortable_sleep", lambda sec: sleeps.append(float(sec)))

    def fake_capture_payload(monitor="active", mode="clean", fmt="webp", **_kw):
        capture_calls.append({"monitor": monitor, "mode": mode, "fmt": fmt})
        meta = {
            "ok": True,
            "mode": mode,
            "monitor": 1,
            "foreground_window_title": "Untitled - Notepad",
            "vision_notice": "Read media_0.webp",
        }
        return [
            TextContent(type="text", text=json.dumps(meta)),
            ImageContent(type="image", data="AAAA", mimeType="image/webp"),
        ]

    monkeypatch.setattr(server, "_capture_payload", fake_capture_payload)

    # verify_mode='none' preserves single TextContent response
    plain = server.arisu_cu_mouse_action(action="click", x=200, y=200, verify_mode="none")
    assert len(plain) == 1 and isinstance(plain[0], TextContent)
    assert json.loads(plain[0].text)["ok"] is True
    assert sleeps == []

    # verify_mode='som' waits wait_after_s and returns TextContent + ImageContent inline
    inline_mouse = server.arisu_cu_mouse_action(
        action="click", x=200, y=200, wait_after_s=1.2, verify_mode="som"
    )
    assert len(inline_mouse) == 2
    assert isinstance(inline_mouse[0], TextContent)
    assert isinstance(inline_mouse[1], ImageContent)
    m_meta = json.loads(inline_mouse[0].text)
    assert m_meta["inline_verify"]["mode"] == "som"
    assert "vision_notice" in m_meta
    assert sleeps[-1] == 1.2

    # Keyboard with verify_mode='grid' and '\n' in text uses S8 submit wait default (1.5s)
    inline_kb = server.arisu_cu_keyboard_action(
        action="type",
        expected_window="Notepad",
        text="hello\n",
        verify_mode="grid",
    )
    assert len(inline_kb) == 2 and isinstance(inline_kb[1], ImageContent)
    assert json.loads(inline_kb[0].text)["inline_verify"]["mode"] == "grid"
    assert sleeps[-1] == 1.5

    # Scroll with verify_mode='clean'
    inline_scroll = server.arisu_cu_scroll_action(
        x=400, y=300, direction="down", amount=4, wait_after_s=0.5, verify_mode="clean"
    )
    assert len(inline_scroll) == 2 and isinstance(inline_scroll[1], ImageContent)
    assert json.loads(inline_scroll[0].text)["inline_verify"]["mode"] == "clean"
    assert [c["mode"] for c in capture_calls] == ["som", "grid", "clean"]
