"""Tests for Tahap S6 Humanized Co-Pilot UX, Anti-Contention, TOCTOU Guard, and Hand-Off."""

from __future__ import annotations

import time

import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from arisu_cua.display.capture import (
    clear_capture_cache,
    record_monitor_capture,
    verify_pre_click_patch,
)
from arisu_cua.display.monitors import MonitorInfo, VirtualScreenRect
from arisu_cua.hud.broker import HudBridgeClient, HudBridgeServer
from arisu_cua.hud.cursor_overlay import ArisuCursorOverlay
from arisu_cua.hud.overlay import ArisuHudWindow
from arisu_cua.input import win32_input
from arisu_cua.safety.killswitch import (
    StaleScreenRegionError,
    UserMouseContentionError,
    reset_abort,
    set_paused,
)


@pytest.fixture(autouse=True)
def _reset_s6_state():
    reset_abort()
    set_paused(False)
    clear_capture_cache()
    yield
    clear_capture_cache()
    reset_abort()
    set_paused(False)


def test_smooth_mouse_glide_cubic_ease_out_and_short_hop(monkeypatch) -> None:
    recorded: list[tuple[int, int, int]] = []

    def fake_send(inputs):
        for inp in inputs:
            mi = inp.union.mi
            recorded.append((int(mi.dx), int(mi.dy), int(mi.dwFlags)))
        return len(inputs)

    monkeypatch.setattr(win32_input, "_send_inputs", fake_send)
    monkeypatch.setattr(win32_input, "get_cursor_pos", lambda: (0, 0))
    monkeypatch.setattr(win32_input, "abortable_sleep", lambda _s: None)

    vrect = VirtualScreenRect(left=0, top=0, width=1600, height=900)
    res = win32_input.execute_mouse_action("click", 1200, 600, v_rect=vrect, smooth=True)
    assert 14 <= res["glide_steps"] <= 22
    assert 120 <= res["glide_ms"] <= 280

    move_xs = [
        dx
        for dx, _dy, flags in recorded
        if (flags & win32_input.MOUSEEVENTF_MOVE)
        and not (flags & (win32_input.MOUSEEVENTF_LEFTDOWN | win32_input.MOUSEEVENTF_LEFTUP))
    ]
    assert len(move_xs) == res["glide_steps"]
    assert move_xs == sorted(move_xs)
    # Cubic Ease-Out covers larger distance initially and decelerates cleanly at target
    assert (move_xs[1] - move_xs[0]) > (move_xs[-1] - move_xs[-2])

    recorded.clear()
    short_res = win32_input.execute_mouse_action("move", 4, 4, v_rect=vrect, smooth=True)
    assert short_res["glide_steps"] == 1
    assert short_res["glide_ms"] == 0
    assert len(recorded) == 1


def test_physical_mouse_contention_yields_and_raises(monkeypatch) -> None:
    sent_flags: list[int] = []
    contention_events: list[str] = []
    cursor_calls = 0

    def fake_send(inputs):
        for inp in inputs:
            sent_flags.append(int(inp.union.mi.dwFlags))
        return len(inputs)

    def fake_cursor_pos() -> tuple[int, int]:
        nonlocal cursor_calls
        cursor_calls += 1
        # Call 1: initial start_pos; Call 2: post_send step 1; Call 3+: user hand jumped +60,+45
        if cursor_calls <= 2:
            return (0, 0)
        return (60, 45)

    monkeypatch.setattr(win32_input, "_send_inputs", fake_send)
    monkeypatch.setattr(win32_input, "get_cursor_pos", fake_cursor_pos)

    vrect = VirtualScreenRect(left=0, top=0, width=1600, height=900)
    with pytest.raises(UserMouseContentionError, match="USER_MOUSE_CONTENTION_YIELD"):
        win32_input.execute_mouse_action(
            "click",
            1000,
            500,
            v_rect=vrect,
            smooth=True,
            yield_stable_s=0.04,
            yield_timeout_s=0.25,
            on_contention=lambda: contention_events.append("yielded"),
        )

    assert contention_events == ["yielded"]
    assert not any(
        f & (win32_input.MOUSEEVENTF_LEFTDOWN | win32_input.MOUSEEVENTF_LEFTUP) for f in sent_flags
    )


def test_pre_click_64x64_patch_toctou_guard() -> None:
    mon = MonitorInfo(
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
    frame = Image.new("RGB", (800, 600), (20, 40, 80))

    # Recent capture (< min_age_s=0.8) skips patch check
    record_monitor_capture(mon, frame, ts=time.monotonic())
    fresh = verify_pre_click_patch(
        400,
        300,
        mon=mon,
        patch_grabber=lambda rect: Image.new(
            "RGB", (rect["width"], rect["height"]), (255, 255, 255)
        ),
    )
    assert fresh["checked"] is False

    # Stale timestamp (> 0.8s): matching live patch passes even near monitor edge
    record_monitor_capture(mon, frame, ts=time.monotonic() - 1.5)
    same = verify_pre_click_patch(
        798,
        598,
        mon=mon,
        patch_grabber=lambda rect: Image.new("RGB", (rect["width"], rect["height"]), (20, 40, 80)),
    )
    assert same["checked"] is True
    assert same["mean_diff"] == 0.0

    # Shifted/scrolled content raises STALE_SCREEN_REGION_CHANGED
    with pytest.raises(StaleScreenRegionError, match="STALE_SCREEN_REGION_CHANGED"):
        verify_pre_click_patch(
            400,
            300,
            mon=mon,
            patch_grabber=lambda rect: Image.new(
                "RGB", (rect["width"], rect["height"]), (240, 240, 240)
            ),
        )


def test_natural_vs_fast_typing_cadence(monkeypatch) -> None:
    sleeps: list[float] = []

    monkeypatch.setattr(win32_input, "_send_inputs", lambda inputs: len(inputs))
    monkeypatch.setattr(win32_input, "abortable_sleep", lambda s: sleeps.append(round(s, 4)))

    res_nat = win32_input.execute_keyboard_action("type", text="a b\nc", typing_mode="natural")
    assert res_nat["chars"] == 5
    assert res_nat["typing_mode"] == "natural"
    assert sleeps == [0.028, 0.04, 0.028, 0.12, 0.028]

    sleeps.clear()
    res_fast = win32_input.execute_keyboard_action("type", text="a b\nc", typing_mode="fast")
    assert res_fast["chars"] == 5
    assert res_fast["typing_mode"] == "fast"
    assert sleeps == [0.012, 0.012, 0.012, 0.012]


def test_cursor_overlay_offscreen_render_and_ripple() -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None

    overlay = ArisuCursorOverlay()
    try:
        overlay.show_cue("click", (300, 300), duration_ms=100, show_window=False)
        overlay._ripple_start_ts = time.monotonic()
        overlay._ripple_progress = 0.5
        canvas = QImage(overlay.SIZE, overlay.SIZE, QImage.Format.Format_RGBA8888)
        canvas.fill(Qt.GlobalColor.transparent)
        overlay.render(canvas)

        # Verify ARISU badge pill and concentric Sapphire cursor ring draw non-transparent pixels
        assert canvas.pixelColor(44, 72).alpha() > 0
        assert canvas.pixelColor(44 + 18, 44).alpha() > 0
    finally:
        overlay.close()
        overlay.deleteLater()
        app.processEvents()


def test_copilot_handoff_broker_and_hud_dock_flow(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None

    hud: ArisuHudWindow | None = None
    server = HudBridgeServer(bridge_path=tmp_path / "bridge.json", port=0)
    hud = ArisuHudWindow(
        config_path=tmp_path / "cfg.json",
        broker=server,
        enable_broker=False,
        enable_hotkeys=False,
        close_on_stop=False,
    )
    server._on_event = hud._on_broker_event_threadsafe
    client = HudBridgeClient(bridge_path=tmp_path / "bridge.json", timeout_s=1.0)
    try:
        server.start()
        req = client.request_handoff("Silakan login EdLink dulu~")
        assert req["ok"] is True
        hid = req["handoff_id"]

        app.processEvents()
        assert hud._pending_handoff is not None
        assert hud.active_tab == "arisu"
        assert client.get_handoff_status(hid)["status"] == "pending"

        canvas = QImage(hud.CANVAS_W, hud.CANVAS_H, QImage.Format.Format_RGBA8888)
        canvas.fill(Qt.GlobalColor.transparent)
        hud.render(canvas)
        assert canvas.pixelColor(hud.pause_rect.center()).alpha() > 0

        hud._resume_handoff()
        assert hud._pending_handoff is None
        assert client.get_handoff_status(hid)["status"] == "resumed"

        req2 = client.request_handoff("Tes batal")
        hid2 = req2["handoff_id"]
        app.processEvents()
        assert hud._pending_handoff is not None
        hud._cancel_handoff()
        assert client.get_handoff_status(hid2)["status"] == "cancelled"
    finally:
        hud.close()
        hud.deleteLater()
        server.stop()
        app.processEvents()
