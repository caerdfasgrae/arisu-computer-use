"""Tests for Tahap S3 curated 2D assets, Single-Instance Named Mutex, WS broker, and PySide6 HUD."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QImage
from PySide6.QtWidgets import QApplication

from arisu_cua.hud.assets import MOOD_TO_CARD_PNG, STATE_TO_CARD_PNG, ensure_assets_extracted
from arisu_cua.hud.broker import (
    CREATE_NO_WINDOW,
    HUD_SPAWN_CREATION_FLAGS,
    HudBridgeClient,
    HudBridgeServer,
    SingleInstanceMutex,
)
from arisu_cua.hud.overlay import ArisuHudWindow
from arisu_cua.safety.guardrails import compute_action_hash
from arisu_cua.safety.killswitch import ABORT_EVENT, PAUSED_EVENT, reset_abort, set_paused


def test_curated_2d_assets_extracted() -> None:
    assets_dir = ensure_assets_extracted()
    assert (assets_dir / "palette.json").exists()
    assert (assets_dir / "HUD_PET_SPEC.md").exists()
    assert (assets_dir / "LAYER_MAP.md").exists()

    for filename in STATE_TO_CARD_PNG.values():
        card_path = assets_dir / "watch" / filename
        assert card_path.exists(), f"Missing curated watch card: {card_path}"

    with Image.open(assets_dir / "watch" / "arisu_neutral.png") as card_img:
        assert card_img.size == (432, 514)

    for mood_png in MOOD_TO_CARD_PNG.values():
        assert (assets_dir / "watch" / mood_png).exists()

    assert (HUD_SPAWN_CREATION_FLAGS & CREATE_NO_WINDOW) == CREATE_NO_WINDOW
    assert (HUD_SPAWN_CREATION_FLAGS & 0x00000008) == 0  # DETACHED_PROCESS must be unset

    with Image.open(assets_dir / "watch" / "face_neutral.png") as face_img:
        assert face_img.size == (47, 47)

    layers = list((assets_dir / "layers").glob("*.png"))
    assert len(layers) == 32


def test_single_instance_named_mutex() -> None:
    mutex_name = f"Local\\ArisuTestMutex_{uuid.uuid4().hex}"
    m1 = SingleInstanceMutex(mutex_name)
    m2 = SingleInstanceMutex(mutex_name)
    try:
        assert m1.acquire() is True
        assert m2.acquire() is False
        m1.release()
        assert m2.acquire() is True
    finally:
        m1.release()
        m2.release()


def test_hud_ws_broker_auth_state_and_stop_abort(tmp_path) -> None:
    reset_abort()
    set_paused(False)
    bridge_path = tmp_path / "hud_bridge.json"
    received_events: list[dict] = []

    server = HudBridgeServer(
        bridge_path=bridge_path,
        port=0,
        on_event=received_events.append,
    )
    client = HudBridgeClient(bridge_path=bridge_path, timeout_s=1.0)
    try:
        server.start()
        status = client.get_hud_status()
        assert status["running"] is True
        assert status["pid"] == os.getpid()

        bad = client.send_event("state", token_override="invalid-token", state="working")
        assert bad["ok"] is False
        assert bad["code"] == "UNAUTHORIZED"

        client.start_listener()
        assert client.wait_subscribed(timeout_s=2.0) is True

        r1 = client.send_event("session_start", session_id="s1", task_label="Test HUD")
        assert r1["ok"] is True
        r2 = client.send_event("state", state="searching", text="Mencari UI…")
        assert r2["ok"] is True
        assert server.current_state == "searching"
        assert any(ev.get("type") == "session_start" for ev in received_events)

        t0 = time.perf_counter()
        server.trigger_hud_stop("Stop test")
        assert ABORT_EVENT.wait(timeout=0.5) is True
        assert (time.perf_counter() - t0) < 0.15

        # Out-of-Band approval request -> physical HUD decision -> single-use consumption
        action_hash = compute_action_hash(
            "arisu_cu_keyboard_action", "Notepad", {"keys": ["ctrl", "w"]}
        )
        req_res = client.request_approval(action_hash, "Hotkey ctrl+w", "Notepad")
        assert req_res["ok"] is True
        req_id = req_res["request_id"]
        assert client.consume_approval(action_hash)["approved"] is False

        token = server.trigger_hud_approval_decision(req_id, approved=True)
        assert token and token.startswith("tok_")
        assert client.consume_approval(action_hash, approval_token=token)["approved"] is True
        # Single-use replay rejected
        replay = client.consume_approval(action_hash, approval_token=token)
        assert replay["ok"] is False and replay["code"] == "INVALID_APPROVAL_TOKEN"

        server.trigger_hud_pause(True)
        deadline = time.perf_counter() + 0.5
        while not PAUSED_EVENT.is_set() and time.perf_counter() < deadline:
            time.sleep(0.01)
        assert PAUSED_EVENT.is_set() is True

        server.trigger_hud_pause(False)
        deadline = time.perf_counter() + 0.5
        while PAUSED_EVENT.is_set() and time.perf_counter() < deadline:
            time.sleep(0.01)
        assert PAUSED_EVENT.is_set() is False
    finally:
        client.close()
        server.stop()
        reset_abort()
        set_paused(False)


def test_hud_overlay_offscreen_render_and_hittest(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    assert app is not None

    hud = ArisuHudWindow(
        config_path=tmp_path / "config.json",
        enable_broker=False,
        enable_hotkeys=False,
    )
    try:
        assert hud.CANVAS_W == 280
        assert hud.CANVAS_H == 360
        assert hud.pause_rect.width() >= 44 and hud.pause_rect.height() >= 44
        assert hud.stop_rect.width() >= 44 and hud.stop_rect.height() >= 44

        for state, mood, label in (
            ("idle", "smile", ""),
            ("working", "wink", "Klik di Monitor 2~"),
            ("working", "laugh", "Atur jendela Notepad!"),
            ("shy", "shy", "Izinmu dulu nih…"),
            ("success", "laugh", "Selesai!"),
            ("error", "surprised", "Timeout"),
        ):
            hud.set_hud_state(state, label, mood=mood)
            canvas = QImage(hud.CANVAS_W, hud.CANVAS_H, QImage.Format.Format_RGBA8888)
            canvas.fill(Qt.GlobalColor.transparent)
            hud.render(canvas)
            # Ensure card pixels inside card_rect center are rendered non-transparent
            center_px = canvas.pixelColor(hud.card_rect.center())
            assert center_px.alpha() > 0

        hud.toggle_privacy_mode()
        assert hud.privacy_mode is True
        hud.set_hud_state("working", "Rahasia banget", mood="wink")
        assert "Mode Privasi" in hud.bubble_text
        hud.toggle_privacy_mode()
        assert hud.privacy_mode is False

        hud.set_paused_state(True)
        assert hud.is_paused is True
        hud.set_paused_state(False)
        assert hud.is_paused is False

        hud.set_force_click_through(True)
        assert hud.force_click_through is True
        hud.set_force_click_through(False)
        assert hud.force_click_through is False

        assert hud.CARD_RADIUS == 16.0
        assert hud.BUBBLE_RADIUS == 12.0
        assert hud.BUTTON_RADIUS == 7.0
        assert len({hud.CARD_RADIUS, hud.BUBBLE_RADIUS, hud.BUTTON_RADIUS}) == 3

        bubble_font = QFont("Segoe UI")
        bubble_font.setPixelSize(14)
        bubble_font.setWeight(QFont.Weight.DemiBold)
        elided = hud._format_bubble_text("X" * 180, bubble_font, hud.bubble_rect.width() - 20)
        assert elided.endswith("…")
        assert len(elided) < 180

        hud._pressed_button = "pause"
        hud._hovered_button = "stop"
        state_canvas = QImage(hud.CANVAS_W, hud.CANVAS_H, QImage.Format.Format_RGBA8888)
        state_canvas.fill(Qt.GlobalColor.transparent)
        hud.render(state_canvas)
        assert state_canvas.pixelColor(hud.pause_rect.center()).alpha() > 0
        assert state_canvas.pixelColor(hud.stop_rect.center()).alpha() > 0
        assert state_canvas.pixelColor(hud.tab_toggle_rect.center()).alpha() > 0
        hud._pressed_button = None
        hud._hovered_button = None

        assert hud.tab_toggle_rect.width() >= 44 and hud.tab_toggle_rect.height() >= 44
        assert hud.subtab_hotkeys_rect.width() >= 44 and hud.subtab_hotkeys_rect.height() >= 44
        assert hud.subtab_controls_rect.width() >= 44 and hud.subtab_controls_rect.height() >= 44

        hud.toggle_hotkeys_tab()
        assert hud.active_tab == "hotkeys"
        for subtab in ("pintasan", "kontrol"):
            hud.set_hotkeys_subtab(subtab)
            assert hud.active_subtab == subtab
            tab_canvas = QImage(hud.CANVAS_W, hud.CANVAS_H, QImage.Format.Format_RGBA8888)
            tab_canvas.fill(Qt.GlobalColor.transparent)
            hud.render(tab_canvas)
            assert tab_canvas.pixelColor(hud.card_rect.center()).alpha() > 0
            assert tab_canvas.pixelColor(hud.subtab_hotkeys_rect.center()).alpha() > 0
            assert tab_canvas.pixelColor(hud.subtab_controls_rect.center()).alpha() > 0

        hud.toggle_hotkeys_tab()
        assert hud.active_tab == "arisu"

        assert hud.close_on_stop is True
        hud.show()
        assert hud.isVisible() is True
        hud._trigger_stop()
        app.processEvents()
        assert hud.isVisible() is False
    finally:
        hud.close()
        hud.deleteLater()
        app.processEvents()


def test_hud_high_dpi_150_stays_on_screen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """HUD must convert physical Win32 monitor bounds to Qt logical coordinates on 150% DPI."""
    from arisu_cua.display.monitors import MonitorInfo
    from arisu_cua.hud import overlay as overlay_mod

    app = QApplication.instance() or QApplication([])
    fake_mon = MonitorInfo(
        index=1,
        device=r"\\.\DISPLAY_FAKE_150",
        left=0,
        top=0,
        width=1920,
        height=1080,
        dpi_x=144,
        dpi_y=144,
        scale_pct=150,
        is_primary=True,
    )
    monkeypatch.setattr(overlay_mod, "enumerate_monitors", lambda: [fake_mon])
    hud = overlay_mod.ArisuHudWindow(
        config_path=tmp_path / "config.json",
        enable_broker=False,
        enable_hotkeys=False,
    )
    try:
        hud.place_on_default_monitor()
        # Logical bounds for 1920x1080 @ 150% are 1280x720
        assert 0 <= hud.x() <= 1280 - hud.CANVAS_W
        assert 0 <= hud.y() <= 720 - hud.CANVAS_H
        # Physical equivalent (x * 1.5, y * 1.5) must stay strictly inside 1920x1080
        assert (hud.x() + hud.CANVAS_W) * 1.5 <= 1920
        assert (hud.y() + hud.CANVAS_H) * 1.5 <= 1080
    finally:
        hud.close()
        hud.deleteLater()
        app.processEvents()
