"""Unit tests for abortable waits, outer corner failsafe, and mocked Win32 SendInput."""

import threading
import time

import pytest

from arisu_cua.display.monitors import VirtualScreenRect
from arisu_cua.input import win32_input
from arisu_cua.input.window_mgr import verify_expected_window
from arisu_cua.safety.killswitch import (
    AbortedByUserError,
    GuardrailApprovalRequiredError,
    SessionPausedError,
    WindowMismatchError,
    abortable_sleep,
    is_in_outer_corner,
    reset_abort,
    set_paused,
    trigger_abort,
)


@pytest.fixture(autouse=True)
def _clean_flags():
    reset_abort()
    set_paused(False)
    yield
    reset_abort()
    set_paused(False)


def test_abortable_sleep_interrupts_quickly() -> None:
    t0 = time.perf_counter()
    timer = threading.Timer(0.05, trigger_abort)
    timer.start()
    try:
        with pytest.raises(AbortedByUserError, match="ABORTED_BY_USER"):
            abortable_sleep(2.0)
    finally:
        timer.cancel()
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.35


def test_paused_session_blocks_execution() -> None:
    set_paused(True)
    with pytest.raises(SessionPausedError, match="SESSION_PAUSED"):
        abortable_sleep(0.1)


def test_outer_corner_failsafe_avoids_dual_monitor_seam() -> None:
    vrect = VirtualScreenRect(left=-1920, top=0, width=3520, height=1080)
    # (0, 0) is the seam between left and right monitors -> must NOT trigger failsafe
    assert is_in_outer_corner((0, 0), vrect) is False
    # Outer top-left corner (-1920, 0) triggers failsafe
    assert is_in_outer_corner((-1920, 0), vrect) is True
    assert is_in_outer_corner((-1918, 2), vrect) is True
    assert is_in_outer_corner((-1910, 0), vrect) is False


def test_mouse_drag_releases_left_button_on_mid_abort(monkeypatch) -> None:
    recorded_flags: list[int] = []

    def fake_send(inputs):
        for inp in inputs:
            recorded_flags.append(int(inp.union.mi.dwFlags))
        # Abort right after LEFTDOWN is sent
        if any(inp.union.mi.dwFlags & win32_input.MOUSEEVENTF_LEFTDOWN for inp in inputs):
            trigger_abort()
        return len(inputs)

    monkeypatch.setattr(win32_input, "_send_inputs", fake_send)
    vrect = VirtualScreenRect(left=-1920, top=0, width=3520, height=1080)

    with pytest.raises(AbortedByUserError):
        win32_input.execute_mouse_action(
            "drag", -500, 200, end_px_x=300, end_px_y=400, v_rect=vrect
        )

    assert any(f & win32_input.MOUSEEVENTF_LEFTDOWN for f in recorded_flags)
    assert any(f & win32_input.MOUSEEVENTF_LEFTUP for f in recorded_flags)
    assert recorded_flags[-1] & win32_input.MOUSEEVENTF_LEFTUP


def test_keyboard_unicode_and_hotkey_cleanup(monkeypatch) -> None:
    events_log: list[tuple[int, int, int]] = []

    def fake_send(inputs):
        for inp in inputs:
            ki = inp.union.ki
            events_log.append((int(ki.wVk), int(ki.wScan), int(ki.dwFlags)))
        return len(inputs)

    monkeypatch.setattr(win32_input, "_send_inputs", fake_send)

    res = win32_input.execute_keyboard_action("type", text="Halo\n✨")
    assert res["chars"] == 6
    # Verify newline became VK_RETURN and astral emoji became UTF-16 surrogate pair (4 events)
    vks = [e[0] for e in events_log]
    assert win32_input.VK_MAP["enter"] in vks
    surrogate_scans = [e[1] for e in events_log if e[2] & win32_input.KEYEVENTF_UNICODE]
    assert len(surrogate_scans) >= 8

    events_log.clear()
    win32_input.execute_keyboard_action("hotkey", keys=["ctrl", "s"])
    # ctrl down, s down, s up, ctrl up
    assert len(events_log) == 4
    assert events_log[0][0] == win32_input.VK_MAP["ctrl"] and events_log[0][2] == 0
    assert events_log[-1][0] == win32_input.VK_MAP["ctrl"] and (
        events_log[-1][2] & win32_input.KEYEVENTF_KEYUP
    )

    with pytest.raises(ValueError, match="INVALID_KEY"):
        win32_input.execute_keyboard_action("press", keys=["nonexistent_key_xyz"])


def test_verify_expected_window_target_lock() -> None:
    verify_expected_window("notepad", "Untitled - Notepad", required=True)
    verify_expected_window("*", "Any Window", required=True)
    verify_expected_window(None, "Google Chrome", required=False)

    with pytest.raises(WindowMismatchError, match="WINDOW_MISMATCH_BLOCKED"):
        verify_expected_window("Notepad", "GOAT Plan | Command Code - Google Chrome", required=True)

    with pytest.raises(WindowMismatchError, match="WINDOW_MISMATCH_BLOCKED"):
        verify_expected_window("", "Untitled - Notepad", required=True)


def test_blocked_sensitive_hotkeys(monkeypatch) -> None:
    sent_calls = 0

    def fake_send(_inputs):
        nonlocal sent_calls
        sent_calls += 1
        return 0

    monkeypatch.setattr(win32_input, "_send_inputs", fake_send)

    for combo in (
        ["ctrl", "w"],
        ["Control", "W"],
        ["alt", "f4"],
        ["ctrl", "f4"],
        ["shift", "delete"],
        ["win", "r"],
    ):
        with pytest.raises(GuardrailApprovalRequiredError, match="GUARDRAIL_APPROVAL_REQUIRED"):
            win32_input.execute_keyboard_action("hotkey", keys=combo)

    assert sent_calls == 0


def test_keyboard_type_paces_single_characters(monkeypatch) -> None:
    call_lengths: list[int] = []

    def fake_send(inputs):
        call_lengths.append(len(inputs))
        return len(inputs)

    monkeypatch.setattr(win32_input, "_send_inputs", fake_send)

    res = win32_input.execute_keyboard_action("type", text="Hi!")
    assert res["chars"] == 3
    assert call_lengths == [2, 2, 2]


def test_resolve_hwnd_on_monitor_cross_screen(monkeypatch) -> None:
    from arisu_cua import server
    from arisu_cua.display.monitors import MonitorInfo
    from arisu_cua.input.window_mgr import WindowInfo

    mon1 = MonitorInfo(
        index=1,
        device=r"\\.\DISPLAY2",
        left=-1920,
        top=0,
        width=1920,
        height=1080,
        dpi_x=96,
        dpi_y=96,
        scale_pct=100,
        is_primary=False,
    )
    mon2 = MonitorInfo(
        index=2,
        device=r"\\.\DISPLAY1",
        left=0,
        top=0,
        width=1600,
        height=900,
        dpi_x=96,
        dpi_y=96,
        scale_pct=100,
        is_primary=True,
    )
    fake_windows = [
        WindowInfo(
            hwnd=101,
            title="Minimized Mon2",
            pid=1,
            left=-32000,
            top=-32000,
            right=-31840,
            bottom=-31972,
            monitor_index=2,
            is_maximized=False,
            is_minimized=True,
        ),
        WindowInfo(
            hwnd=202,
            title="Notepad",
            pid=2,
            left=200,
            top=200,
            right=1200,
            bottom=700,
            monitor_index=2,
            is_maximized=False,
            is_minimized=False,
        ),
    ]
    assert fake_windows[1].monitor == 2
    monkeypatch.setattr(server, "list_windows", lambda monitors, limit=25: fake_windows)

    # Foreground window is on Monitor 1, resolving Monitor 2 should pick hwnd 202 (non-minimized)
    picked = server._resolve_hwnd_on_monitor(
        mon2, [mon1, mon2], fg_hwnd=999, fg_rect=(-1900, 0, -100, 1000)
    )
    assert picked == 202

    # Foreground window on Monitor 2 should preserve fg_hwnd directly
    same_screen = server._resolve_hwnd_on_monitor(
        mon2, [mon1, mon2], fg_hwnd=303, fg_rect=(100, 100, 900, 700)
    )
    assert same_screen == 303
