"""Arisu 2D PySide6 HUD Card with WM_NCHITTEST, WS_EX_NOACTIVATE, and WDA exclusion."""

from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from arisu_cua.display.monitors import (
    MonitorInfo,
    enable_per_monitor_dpi_v2,
    enumerate_monitors,
    find_monitor_for_point,
)

# Enable Per-Monitor DPI Awareness V2 BEFORE importing PySide6 QtGui/QtWidgets.
DPI_MODE = enable_per_monitor_dpi_v2()

import contextlib
import time

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QWidget

from arisu_cua.config import get_config_path
from arisu_cua.hud.assets import (
    ASSETS_DIR,
    MOOD_TO_CARD_PNG,
    STATE_DEFAULT_LABELS,
    STATE_TO_CARD_PNG,
    ensure_assets_extracted,
)
from arisu_cua.hud.broker import DEFAULT_MUTEX_NAME, HudBridgeServer, SingleInstanceMutex
from arisu_cua.hud.cursor_overlay import ArisuCursorOverlay
from arisu_cua.input.window_mgr import list_windows

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

HWND_TOPMOST = wintypes.HWND(-1)
HWND_NOTOPMOST = wintypes.HWND(-2)

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

WM_NCHITTEST = 0x0084
WM_HOTKEY = 0x0312
HTTRANSPARENT = -1
HTCLIENT = 1

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
SPI_GETCLIENTAREAANIMATION = 0x1042

QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4

HOTKEY_TOGGLE_VISIBILITY = 101  # Ctrl+Alt+A
HOTKEY_TOGGLE_PAUSE = 102  # Ctrl+Alt+P
HOTKEY_EMERGENCY_STOP = 103  # Ctrl+Alt+Esc
HOTKEY_TOGGLE_PRIVACY = 104  # Ctrl+Alt+V
HOTKEY_APPROVE_PENDING = 105  # Ctrl+Alt+Y
HOTKEY_DENY_PENDING = 106  # Ctrl+Alt+N


def is_os_reduced_motion() -> bool:
    """Return True if Windows 'Show animations' preference is turned off."""
    anim_enabled = wintypes.BOOL(1)
    try:
        ok = _user32.SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(anim_enabled), 0
        )
        if ok:
            return not bool(anim_enabled.value)
    except Exception:
        pass
    return False


def is_exclusive_fullscreen_active() -> bool:
    """Return True if user is in D3D exclusive fullscreen or presentation mode."""
    state = ctypes.c_int(0)
    try:
        hr = _shell32.SHQueryUserNotificationState(ctypes.byref(state))
        if hr == 0:
            return int(state.value) in (
                QUNS_RUNNING_D3D_FULL_SCREEN,
                QUNS_PRESENTATION_MODE,
            )
    except Exception:
        pass
    return False


def _get_physical_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)


class ArisuHudWindow(QWidget):
    """Frameless 2D Arisu Desktop Pet HUD Card with click-through & exclusion."""

    CANVAS_W = 280
    CANVAS_H = 360
    CARD_RADIUS = 16.0
    BUBBLE_RADIUS = 12.0
    BUTTON_RADIUS = 7.0

    HOTKEY_ITEMS: tuple[tuple[str, str], ...] = (
        ("Ctrl+Alt+P", "Jeda / Lanjut"),
        ("Ctrl+Alt+Esc", "Stop & Tutup"),
        ("Ctrl+Alt+A", "Tampil / Hide"),
        ("Ctrl+Alt+V", "Mode Privasi"),
        ("Ctrl+Alt+Y", "Izinkan Aksi"),
        ("Ctrl+Alt+N", "Tolak Aksi"),
    )
    CONTROL_ITEMS: tuple[tuple[str, str], ...] = (
        ("Drag Atas", "Geser Posisi HUD"),
        ("Tap Kartu", "Ganti Ekspresi"),
        ("Klik Kanan", "Mode Privasi"),
        ("Sudut Kiri", "Failsafe 300ms"),
        ("Tombol P", "Pause / Resume"),
        ("Enter / Esc", "Izin / Stop"),
    )

    _bridge_event_signal = Signal(dict)

    def __init__(
        self,
        assets_dir: Path | None = None,
        config_path: Path | None = None,
        broker: HudBridgeServer | None = None,
        enable_broker: bool = True,
        enable_hotkeys: bool = True,
        close_on_stop: bool = True,
    ) -> None:
        super().__init__()
        self.assets_dir = ensure_assets_extracted(assets_dir or ASSETS_DIR)
        self.config_path = config_path or get_config_path()
        self._enable_hotkeys = enable_hotkeys
        self.close_on_stop = bool(close_on_stop)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setFixedSize(self.CANVAS_W, self.CANVAS_H)

        # Layout regions in logical coordinates matching HUD_PET_SPEC.md
        self.bubble_rect = QRect(10, 4, 202, 52)
        self.tab_toggle_rect = QRect(218, 8, 52, 44)
        self.card_rect = QRect(30, 60, 220, 260)
        self.drag_rect = QRect(44, 64, 192, 44)
        self.subtab_hotkeys_rect = QRect(36, 66, 100, 44)
        self.subtab_controls_rect = QRect(144, 66, 100, 44)
        self.pause_rect = QRect(34, 312, 102, 44)
        self.stop_rect = QRect(144, 312, 102, 44)

        self.active_tab: str = "arisu"
        self.active_subtab: str = "pintasan"
        self.state_name: str = "idle"
        self.mood_name: str = "smile"
        self.bubble_text: str = ""
        self.is_paused: bool = False
        self.privacy_mode: bool = False
        self.force_click_through: bool = False
        self.current_monitor: int = 1

        self._override_card_png: str | None = None
        self._pending_approval: dict[str, Any] | None = None
        self._pending_handoff: dict[str, Any] | None = None
        self._cursor_overlay: ArisuCursorOverlay | None = None
        self._pixmap_cache: dict[str, QPixmap] = {}
        self._dragging: bool = False
        self._drag_start_global = QPoint()
        self._drag_offset = QPoint()
        self._pressed_button: str | None = None
        self._hovered_button: str | None = None
        self._hotkeys_registered: bool = False
        self._reduced_motion: bool = is_os_reduced_motion()
        self._is_topmost: bool = True
        self._idle_moods = ("smile", "wink", "neutral")
        self._idle_mood_idx: int = 0
        self._playful_toggle: bool = False
        self._last_activity_ts: float = time.monotonic()

        self._bridge_event_signal.connect(
            self._handle_bridge_event_qt, Qt.ConnectionType.QueuedConnection
        )

        self._broker = broker
        if self._broker is None and enable_broker:
            self._broker = HudBridgeServer(on_event=self._on_broker_event_threadsafe)

        # Idle blink cadence, gentle idle mood variation, bubble auto-hide & fullscreen check
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(4500)
        self._blink_timer.timeout.connect(self._start_blink)

        self._blink_step_timer = QTimer(self)
        self._blink_step_timer.setSingleShot(True)

        self._bubble_hide_timer = QTimer(self)
        self._bubble_hide_timer.setSingleShot(True)
        self._bubble_hide_timer.timeout.connect(self._hide_bubble)

        self._sleepy_timer = QTimer(self)
        self._sleepy_timer.setInterval(15000)
        self._sleepy_timer.timeout.connect(self._check_idle_mood)

        self._fullscreen_timer = QTimer(self)
        self._fullscreen_timer.setInterval(2000)
        self._fullscreen_timer.timeout.connect(self._check_fullscreen_yield)

        self._stop_close_timer = QTimer(self)
        self._stop_close_timer.setSingleShot(True)
        self._stop_close_timer.timeout.connect(self._close_from_stop)

        if not self._reduced_motion:
            self._blink_timer.start()
            self._sleepy_timer.start()

    def _on_broker_event_threadsafe(self, event: dict[str, Any]) -> None:
        self._bridge_event_signal.emit(event)

    def _handle_bridge_event_qt(self, event: dict[str, Any]) -> None:
        ev_type = str(event.get("type") or "")
        if ev_type == "session_start":
            self._pending_approval = None
            self._pending_handoff = None
            label = str(event.get("task_label") or "Siap bantu!")
            self.set_paused_state(False)
            self.set_hud_state("working", f"Siap bantu: {label}", mood="wink")
        elif ev_type == "session_pause":
            self.set_paused_state(bool(event.get("paused", False)))
        elif ev_type == "session_stop":
            self._pending_approval = None
            self._pending_handoff = None
            self.set_paused_state(False)
            self.set_hud_state("success", "Selesai dikerjakan!", mood="laugh")
            self._schedule_close_on_stop()
        elif ev_type == "state":
            st = str(event.get("state") or "idle")
            txt = event.get("text")
            mood = event.get("mood")
            self.set_hud_state(
                st,
                None if txt is None else str(txt),
                mood=None if mood is None else str(mood),
            )
        elif ev_type == "approval_request":
            self._pending_approval = dict(event)
            self.active_tab = "arisu"
            desc = str(event.get("action_desc") or "Aksi sensitif")
            self.set_hud_state("shy", f"Izinmu: {desc}?", mood="shy")
        elif ev_type == "handoff_request":
            self._pending_handoff = dict(event)
            self.active_tab = "arisu"
            instr = str(event.get("instruction") or "Selesaikan aksi manual, lalu klik LANJUT")
            self.set_hud_state("shy", instr, mood="shy")
        elif ev_type == "cursor_cue":
            if not self._reduced_motion:
                if self._cursor_overlay is None:
                    self._cursor_overlay = ArisuCursorOverlay()
                self._cursor_overlay.show_cue(
                    action=str(event.get("action") or "move"),
                    target_px=(int(event.get("x") or 0), int(event.get("y") or 0)),
                    duration_ms=int(event.get("duration_ms") or 180),
                )
        elif ev_type == "set_click_through":
            self.set_force_click_through(bool(event.get("enabled", False)))

    def _load_card_pixmap(self, filename: str) -> QPixmap:
        if filename in self._pixmap_cache:
            return self._pixmap_cache[filename]
        path = self.assets_dir / "watch" / filename
        if not path.exists():
            path = self.assets_dir / "watch" / "arisu_smile.png"
        pm = QPixmap(str(path))
        if not pm.isNull():
            pm = pm.scaled(
                self.card_rect.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self._pixmap_cache[filename] = pm
        return pm

    def set_hud_state(
        self,
        state: str,
        text: str | None = None,
        mood: str | None = None,
    ) -> None:
        norm = state.strip().lower()
        if norm not in STATE_TO_CARD_PNG:
            norm = "idle"
        self.state_name = norm
        if mood and mood.strip().lower() in MOOD_TO_CARD_PNG:
            self.mood_name = mood.strip().lower()
        else:
            self.mood_name = ""
        self._override_card_png = None
        self._last_activity_ts = time.monotonic()

        raw_text = STATE_DEFAULT_LABELS.get(norm, "") if text is None else text.strip()
        is_gate_active = self._pending_approval is not None or self._pending_handoff is not None
        if self.privacy_mode and raw_text and not is_gate_active:
            self.bubble_text = "Mode Privasi: bekerja di layar aktif…"
        else:
            self.bubble_text = raw_text

        if self.bubble_text and norm in ("idle", "success", "shy") and not is_gate_active:
            self._bubble_hide_timer.start(4800)
        else:
            self._bubble_hide_timer.stop()

        self.update()

    def set_paused_state(self, paused: bool) -> None:
        self.is_paused = bool(paused)
        self._last_activity_ts = time.monotonic()
        if self.is_paused:
            self.mood_name = "surprised"
            self.bubble_text = "Dijeda dulu~ klik RESUME untuk lanjut"
            self._bubble_hide_timer.stop()
        elif self.state_name == "idle":
            self.mood_name = "smile"
            self.bubble_text = ""
        self.update()

    def toggle_privacy_mode(self) -> None:
        self.privacy_mode = not self.privacy_mode
        if self.privacy_mode:
            self.set_hud_state(self.state_name, "Mode Privasi aktif", mood="wink")
        else:
            self.set_hud_state(self.state_name, "Mode Privasi nonaktif", mood="smile")

    def _hide_bubble(self) -> None:
        self.bubble_text = ""
        if self.state_name == "success":
            self.state_name = "idle"
            self.mood_name = "smile"
        self.update()

    def _check_idle_mood(self) -> None:
        if (
            self._reduced_motion
            or self.is_paused
            or self._pending_approval is not None
            or self._pending_handoff is not None
        ):
            return
        if self.state_name != "idle":
            return
        if (time.monotonic() - self._last_activity_ts) >= 45.0:
            self.mood_name = "sleepy"
            self.update()
        else:
            self._idle_mood_idx = (self._idle_mood_idx + 1) % len(self._idle_moods)
            self.mood_name = self._idle_moods[self._idle_mood_idx]
            self.update()

    def _check_fullscreen_yield(self) -> None:
        hwnd = int(self.winId()) if self.winId() else 0
        if not hwnd:
            return
        fs_active = is_exclusive_fullscreen_active()
        if fs_active and self._is_topmost:
            _user32.SetWindowPos(
                wintypes.HWND(hwnd),
                HWND_NOTOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
            self._is_topmost = False
        elif not fs_active and not self._is_topmost:
            _user32.SetWindowPos(
                wintypes.HWND(hwnd),
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
            self._is_topmost = True

    def _start_blink(self) -> None:
        if self._reduced_motion or self.state_name != "idle" or self.is_paused:
            return
        self._override_card_png = "arisu_blink_half.png"
        self.update()
        self._blink_step_timer.stop()
        with contextlib.suppress(Exception):
            self._blink_step_timer.timeout.disconnect()
        self._blink_step_timer.timeout.connect(self._blink_close)
        self._blink_step_timer.start(75)

    def _blink_close(self) -> None:
        self._override_card_png = "arisu_blink_closed.png"
        self.update()
        with contextlib.suppress(Exception):
            self._blink_step_timer.timeout.disconnect()
        self._blink_step_timer.timeout.connect(self._blink_open)
        self._blink_step_timer.start(75)

    def _blink_open(self) -> None:
        self._override_card_png = None
        self.update()

    def _load_saved_positions(self) -> dict[str, list[int]]:
        if not self.config_path.exists():
            return {}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            pos = data.get("hud_positions")
            return pos if isinstance(pos, dict) else {}
        except Exception:
            return {}

    def _save_monitor_position(self, monitor_index: int, x: int, y: int) -> None:
        try:
            data: dict[str, Any] = {}
            if self.config_path.exists():
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
            positions = data.get("hud_positions")
            if not isinstance(positions, dict):
                positions = {}
            positions[str(monitor_index)] = [int(x), int(y)]
            data["hud_positions"] = positions
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _choose_default_monitor(self, monitors: list[MonitorInfo]) -> MonitorInfo:
        try:
            for win in list_windows(monitors, limit=25):
                if "antigravity" in win.title.lower() and not win.is_minimized:
                    matched = next((m for m in monitors if m.index == win.monitor_index), None)
                    if matched is not None:
                        return matched
        except Exception:
            pass
        return monitors[0]

    def _get_logical_monitor_bounds(self, mon: MonitorInfo) -> tuple[int, int, int, int]:
        """Return (left, top, right, bottom) in Qt logical coordinates for a Win32 MonitorInfo."""
        app = QApplication.instance()
        if app is not None:
            for screen in app.screens():
                if screen.name() == mon.device:
                    avail = screen.availableGeometry()
                    return (
                        avail.left(),
                        avail.top(),
                        avail.left() + avail.width(),
                        avail.top() + avail.height(),
                    )
        scale = max(float(mon.scale_pct) / 100.0, 0.5)
        left = round(mon.left / scale)
        top = round(mon.top / scale)
        right = left + round(mon.width / scale)
        bottom = top + round(mon.height / scale)
        return (left, top, right, bottom)

    def place_on_default_monitor(self) -> None:
        monitors = enumerate_monitors()
        if not monitors:
            return
        mon = self._choose_default_monitor(monitors)
        self.current_monitor = mon.index
        l_left, l_top, l_right, l_bottom = self._get_logical_monitor_bounds(mon)
        saved = self._load_saved_positions().get(str(mon.index))
        if isinstance(saved, list) and len(saved) == 2:
            sx, sy = int(saved[0]), int(saved[1])
            clamped_x = min(max(sx, l_left + 8), max(l_left + 8, l_right - self.CANVAS_W - 8))
            clamped_y = min(max(sy, l_top + 8), max(l_top + 8, l_bottom - self.CANVAS_H - 8))
            self.move(clamped_x, clamped_y)
        else:
            x = l_right - self.CANVAS_W - 20
            y = l_bottom - self.CANVAS_H - 16
            self.move(max(l_left + 8, x), max(l_top + 8, y))

    def apply_win32_styles(self) -> None:
        hwnd = int(self.winId())
        _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        _user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t

        ex_style = _user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE)
        ex_style |= WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST
        _user32.SetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE, ex_style)
        _user32.SetWindowPos(
            wintypes.HWND(hwnd),
            0,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )

        _user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        _user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
        _user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), WDA_EXCLUDEFROMCAPTURE)

        if self._enable_hotkeys and not self._hotkeys_registered:
            _user32.RegisterHotKey(
                wintypes.HWND(hwnd),
                HOTKEY_TOGGLE_VISIBILITY,
                MOD_CONTROL | MOD_ALT | MOD_NOREPEAT,
                ord("A"),
            )
            _user32.RegisterHotKey(
                wintypes.HWND(hwnd),
                HOTKEY_TOGGLE_PAUSE,
                MOD_CONTROL | MOD_ALT | MOD_NOREPEAT,
                ord("P"),
            )
            _user32.RegisterHotKey(
                wintypes.HWND(hwnd),
                HOTKEY_EMERGENCY_STOP,
                MOD_CONTROL | MOD_ALT | MOD_NOREPEAT,
                0x1B,
            )
            _user32.RegisterHotKey(
                wintypes.HWND(hwnd),
                HOTKEY_TOGGLE_PRIVACY,
                MOD_CONTROL | MOD_ALT | MOD_NOREPEAT,
                ord("V"),
            )
            _user32.RegisterHotKey(
                wintypes.HWND(hwnd),
                HOTKEY_APPROVE_PENDING,
                MOD_CONTROL | MOD_ALT | MOD_NOREPEAT,
                ord("Y"),
            )
            _user32.RegisterHotKey(
                wintypes.HWND(hwnd),
                HOTKEY_DENY_PENDING,
                MOD_CONTROL | MOD_ALT | MOD_NOREPEAT,
                ord("N"),
            )
            self._hotkeys_registered = True

    def set_force_click_through(self, enabled: bool) -> None:
        self.force_click_through = bool(enabled)
        hwnd = int(self.winId())
        if hwnd:
            ex_style = _user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE)
            if self.force_click_through:
                ex_style |= WS_EX_TRANSPARENT
            else:
                ex_style &= ~WS_EX_TRANSPARENT
            _user32.SetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE, ex_style)
            _user32.SetWindowPos(
                wintypes.HWND(hwnd),
                0,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
            )

    def _sync_broker_geometry(self) -> None:
        if self._broker is None:
            return
        hwnd = int(self.winId()) if self.winId() else 0
        if hwnd:
            rect = _get_physical_window_rect(hwnd)
        else:
            rect = (self.x(), self.y(), self.x() + self.width(), self.y() + self.height())
        self._broker.update_geometry(hwnd=hwnd, monitor=self.current_monitor, hud_rect=rect)

    def start_hud(self) -> None:
        self.place_on_default_monitor()
        self.createWinId()
        self.apply_win32_styles()
        if self._broker is not None:
            self._broker.start()
        self._fullscreen_timer.start()
        self.show()
        self._sync_broker_geometry()

    def _toggle_pause(self) -> None:
        next_paused = not self.is_paused
        self.set_paused_state(next_paused)
        if self._broker is not None:
            self._broker.trigger_hud_pause(next_paused)

    def _close_from_stop(self) -> None:
        self.close()
        app = QApplication.instance()
        if app is not None and self._broker is not None:
            app.quit()

    def _schedule_close_on_stop(self) -> None:
        if not self.close_on_stop:
            return
        self.hide()
        self._stop_close_timer.start(150)

    def _trigger_stop(self) -> None:
        handoff = self._pending_handoff
        self._pending_approval = None
        self._pending_handoff = None
        if handoff and self._broker is not None:
            with contextlib.suppress(Exception):
                self._broker.trigger_hud_handoff_decision(str(handoff["handoff_id"]), False)
        self.set_paused_state(False)
        self.set_hud_state("idle", "Dihentikan.", mood="surprised")
        if self._broker is not None:
            self._broker.trigger_hud_stop("Stopped via Arisu HUD")
        self._schedule_close_on_stop()

    def _resume_handoff(self) -> None:
        handoff = self._pending_handoff
        self._pending_handoff = None
        if handoff and self._broker is not None:
            with contextlib.suppress(Exception):
                self._broker.trigger_hud_handoff_decision(str(handoff["handoff_id"]), True)
        self.set_hud_state("working", "Oke, Arisu lanjutkan ya~", mood="wink")

    def _cancel_handoff(self) -> None:
        handoff = self._pending_handoff
        self._pending_handoff = None
        if handoff and self._broker is not None:
            with contextlib.suppress(Exception):
                self._broker.trigger_hud_handoff_decision(str(handoff["handoff_id"]), False)
        self.set_hud_state("idle", "Hand-off dibatalkan.", mood="neutral")

    def toggle_hotkeys_tab(self) -> None:
        self.active_tab = "hotkeys" if self.active_tab == "arisu" else "arisu"
        self.update()

    def set_hotkeys_subtab(self, subtab: str) -> None:
        norm = subtab.strip().lower()
        if norm in ("pintasan", "kontrol"):
            self.active_subtab = norm
            self.update()

    def _approve_pending(self) -> None:
        pending = self._pending_approval
        self._pending_approval = None
        if pending and self._broker is not None:
            with contextlib.suppress(Exception):
                self._broker.trigger_hud_approval_decision(str(pending["request_id"]), True)
        self.set_hud_state("success", "Diizinkan! Siap dijalankan.", mood="laugh")

    def _deny_pending(self) -> None:
        pending = self._pending_approval
        self._pending_approval = None
        if pending and self._broker is not None:
            with contextlib.suppress(Exception):
                self._broker.trigger_hud_approval_decision(str(pending["request_id"]), False)
        self.set_hud_state("idle", "Aksi ditolak aman.", mood="neutral")

    def _trigger_playful_reaction(self) -> None:
        self._playful_toggle = not self._playful_toggle
        if self._playful_toggle:
            self.set_hud_state(self.state_name, "Ehehe, Arisu siap bantu~", mood="shy")
        else:
            self.set_hud_state(self.state_name, "Ada yang mau dibuka?", mood="wink")

    def _button_at(self, pt: QPoint) -> str | None:
        if self.tab_toggle_rect.contains(pt):
            return "tab_toggle"
        if self.active_tab == "hotkeys":
            if self.subtab_hotkeys_rect.contains(pt):
                return "subtab_pintasan"
            if self.subtab_controls_rect.contains(pt):
                return "subtab_kontrol"
        if self.pause_rect.contains(pt):
            return "pause"
        if self.stop_rect.contains(pt):
            return "stop"
        return None

    def _is_drag_zone(self, pt: QPoint) -> bool:
        if self.active_tab == "hotkeys":
            return self.bubble_rect.contains(pt)
        return self.drag_rect.contains(pt)

    def _update_hover_state(self, pt: QPoint | None) -> None:
        if pt is None or self.force_click_through:
            hovered = None
            cursor_shape = None
        else:
            hovered = self._button_at(pt)
            if hovered is not None:
                cursor_shape = Qt.CursorShape.PointingHandCursor
            elif self._is_drag_zone(pt):
                cursor_shape = (
                    Qt.CursorShape.ClosedHandCursor
                    if self._dragging
                    else Qt.CursorShape.OpenHandCursor
                )
            else:
                cursor_shape = None

        if cursor_shape is not None:
            self.setCursor(cursor_shape)
        else:
            self.unsetCursor()

        if hovered != self._hovered_button:
            self._hovered_button = hovered
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self.force_click_through:
            return
        pt = event.position().toPoint()
        if event.button() == Qt.MouseButton.RightButton and self._is_drag_zone(pt):
            self.toggle_privacy_mode()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            btn = self._button_at(pt)
            if btn is not None:
                self._pressed_button = btn
                self.update()
            elif self._is_drag_zone(pt):
                self._dragging = True
                self._drag_start_global = event.globalPosition().toPoint()
                self._drag_offset = self._drag_start_global - self.frameGeometry().topLeft()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            return
        self._update_hover_state(event.position().toPoint())

    def leaveEvent(self, event: QEvent) -> None:
        if self._hovered_button is not None or self._pressed_button is not None:
            self._hovered_button = None
            self._pressed_button = None
            self.update()
        if not self._dragging:
            self.unsetCursor()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        pt = event.position().toPoint()
        if self._dragging:
            self._dragging = False
            self._update_hover_state(pt)
            delta = (event.globalPosition().toPoint() - self._drag_start_global).manhattanLength()
            if delta < 5:
                if self.active_tab == "arisu":
                    self._trigger_playful_reaction()
                return
            monitors = enumerate_monitors()
            hwnd = int(self.winId()) if self.winId() else 0
            if hwnd:
                wx, wy, wright, wbottom = _get_physical_window_rect(hwnd)
                px_cx, px_cy = (wx + wright) // 2, (wy + wbottom) // 2
            else:
                center = self.frameGeometry().center()
                px_cx, px_cy = center.x(), center.y()
            mon = find_monitor_for_point(monitors, px_cx, px_cy)
            if mon is not None:
                self.current_monitor = mon.index
                self._save_monitor_position(mon.index, self.x(), self.y())
            self._sync_broker_geometry()
            return

        pressed = self._pressed_button
        self._pressed_button = None
        self._update_hover_state(pt)
        self.update()
        if pressed == "tab_toggle" and self.tab_toggle_rect.contains(pt):
            self.toggle_hotkeys_tab()
            return
        if (
            self.active_tab == "hotkeys"
            and pressed == "subtab_pintasan"
            and self.subtab_hotkeys_rect.contains(pt)
        ):
            self.set_hotkeys_subtab("pintasan")
            return
        if (
            self.active_tab == "hotkeys"
            and pressed == "subtab_kontrol"
            and self.subtab_controls_rect.contains(pt)
        ):
            self.set_hotkeys_subtab("kontrol")
            return
        if self._pending_approval is not None:
            if pressed == "pause" and self.pause_rect.contains(pt):
                self._approve_pending()
            elif pressed == "stop" and self.stop_rect.contains(pt):
                self._deny_pending()
            return
        if self._pending_handoff is not None:
            if pressed == "pause" and self.pause_rect.contains(pt):
                self._resume_handoff()
            elif pressed == "stop" and self.stop_rect.contains(pt):
                self._cancel_handoff()
            return

        if pressed == "pause" and self.pause_rect.contains(pt):
            self._toggle_pause()
        elif pressed == "stop" and self.stop_rect.contains(pt):
            self._trigger_stop()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if self._pending_approval is not None or self._pending_handoff is not None:
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Y):
                if self._pending_approval is not None:
                    self._approve_pending()
                else:
                    self._resume_handoff()
                event.accept()
                return
            if key in (Qt.Key.Key_Escape, Qt.Key.Key_N):
                if self._pending_approval is not None:
                    self._deny_pending()
                else:
                    self._cancel_handoff()
                event.accept()
                return
        else:
            if key in (Qt.Key.Key_H, Qt.Key.Key_Question, Qt.Key.Key_Tab):
                self.toggle_hotkeys_tab()
                event.accept()
                return
            if key in (Qt.Key.Key_P, Qt.Key.Key_Space):
                self._toggle_pause()
                event.accept()
                return
            if key == Qt.Key.Key_Escape:
                self._trigger_stop()
                event.accept()
                return
        super().keyPressEvent(event)

    def nativeEvent(self, eventType: bytes, message: int) -> tuple[bool, int]:
        if bytes(eventType) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_NCHITTEST:
                if self.force_click_through:
                    return True, HTTRANSPARENT
                screen_x = ctypes.c_int16(msg.lParam & 0xFFFF).value
                screen_y = ctypes.c_int16((msg.lParam >> 16) & 0xFFFF).value
                wx, wy, wright, wbottom = _get_physical_window_rect(int(self.winId()))
                actual_w = max(wright - wx, 1)
                actual_h = max(wbottom - wy, 1)
                lx = round((screen_x - wx) * self.CANVAS_W / actual_w)
                ly = round((screen_y - wy) * self.CANVAS_H / actual_h)
                pt = QPoint(lx, ly)
                if self._button_at(pt) is not None or self._is_drag_zone(pt):
                    return True, HTCLIENT
                return True, HTTRANSPARENT

            if msg.message == WM_HOTKEY:
                hk_id = int(msg.wParam)
                if hk_id == HOTKEY_TOGGLE_VISIBILITY:
                    self.setVisible(not self.isVisible())
                    return True, 0
                if hk_id == HOTKEY_TOGGLE_PAUSE:
                    self._toggle_pause()
                    return True, 0
                if hk_id == HOTKEY_EMERGENCY_STOP:
                    self._trigger_stop()
                    return True, 0
                if hk_id == HOTKEY_TOGGLE_PRIVACY:
                    self.toggle_privacy_mode()
                    return True, 0
                if hk_id == HOTKEY_APPROVE_PENDING:
                    if self._pending_approval is not None:
                        self._approve_pending()
                    elif self._pending_handoff is not None:
                        self._resume_handoff()
                    return True, 0
                if hk_id == HOTKEY_DENY_PENDING:
                    if self._pending_approval is not None:
                        self._deny_pending()
                    elif self._pending_handoff is not None:
                        self._cancel_handoff()
                    return True, 0

        return super().nativeEvent(eventType, message)

    def _format_bubble_text(self, text: str, font: QFont, max_width: int) -> str:
        compact = " ".join(text.split())
        if not compact:
            return ""
        metrics = QFontMetrics(font)
        return metrics.elidedText(
            compact,
            Qt.TextElideMode.ElideRight,
            max(max_width * 2 - 14, 40),
        )

    def _paint_dock_button(
        self,
        painter: QPainter,
        rect: QRect,
        key: str,
        label: str,
        base_bg: str,
        hover_bg: str,
        press_bg: str,
        base_border: str,
        hover_border: str,
        border_width: float = 1.5,
    ) -> None:
        is_pressed = self._pressed_button == key
        is_hovered = self._hovered_button == key
        draw_rect = QRectF(rect).translated(0.0, 1.0) if is_pressed else QRectF(rect)
        bg = press_bg if is_pressed else (hover_bg if is_hovered else base_bg)
        border = "#FFFFFF" if is_pressed else (hover_border if is_hovered else base_border)
        stroke = 2.0 if (is_pressed or is_hovered) else border_width

        painter.setBrush(QColor(bg))
        painter.setPen(QPen(QColor(border), stroke))
        painter.drawRoundedRect(draw_rect, self.BUTTON_RADIUS, self.BUTTON_RADIUS)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(draw_rect.toRect(), Qt.AlignmentFlag.AlignCenter, label)

    def _paint_hotkey_rows(
        self,
        painter: QPainter,
        items: tuple[tuple[str, str], ...],
    ) -> None:
        key_font = QFont("Segoe UI")
        key_font.setPixelSize(13)
        key_font.setWeight(QFont.Weight.Bold)

        desc_font = QFont("Segoe UI")
        desc_font.setPixelSize(13)
        desc_font.setWeight(QFont.Weight.DemiBold)

        for idx, (key_label, desc) in enumerate(items):
            row_y = 116 + idx * 30
            key_rect = QRect(36, row_y, 92, 25)
            desc_rect = QRect(132, row_y, 112, 25)

            painter.setBrush(QColor("#1C3F7A"))
            painter.setPen(QPen(QColor("#BAC2DC"), 1.0))
            painter.drawRoundedRect(QRectF(key_rect), 5.0, 5.0)
            painter.setFont(key_font)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(key_rect, Qt.AlignmentFlag.AlignCenter, key_label)

            painter.setFont(desc_font)
            painter.setPen(QColor("#E0E8F5"))
            painter.drawText(
                desc_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                desc,
            )

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # 1. Header Bubble + Top-Right Tab Switcher (WCAG AA >=13px)
        header_text = self.bubble_text or (
            "Pintasan & Kontrol HUD" if self.active_tab == "hotkeys" else ""
        )
        gate_active = self._pending_approval is not None or self._pending_handoff is not None
        if header_text:
            painter.setBrush(QColor(28, 63, 122, 235))  # Navy Sapphire #1C3F7A 92%
            bubble_border = "#FCE3E6" if gate_active else "#E0E8F5"
            painter.setPen(QPen(QColor(bubble_border), 1.5))
            painter.drawRoundedRect(
                QRectF(self.bubble_rect), self.BUBBLE_RADIUS, self.BUBBLE_RADIUS
            )
            bubble_font = QFont("Segoe UI")
            bubble_font.setPixelSize(14)
            bubble_font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(bubble_font)
            painter.setPen(QColor("#FFFFFF"))
            inner_text_rect = self.bubble_rect.adjusted(10, 4, -10, -4)
            display_text = self._format_bubble_text(
                header_text, bubble_font, inner_text_rect.width()
            )
            painter.drawText(
                inner_text_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap,
                display_text,
            )

        dock_font = QFont("Segoe UI")
        dock_font.setPixelSize(13)
        dock_font.setWeight(QFont.Weight.Bold)
        painter.setFont(dock_font)
        in_hotkeys_tab = self.active_tab == "hotkeys"
        self._paint_dock_button(
            painter,
            self.tab_toggle_rect,
            "tab_toggle",
            "ARISU" if in_hotkeys_tab else "KEYS",
            base_bg="#1C3F7A" if in_hotkeys_tab else "#16264A",
            hover_bg="#27539A" if in_hotkeys_tab else "#21396E",
            press_bg="#132B54" if in_hotkeys_tab else "#0F1B36",
            base_border="#E0E8F5" if in_hotkeys_tab else "#BAC2DC",
            hover_border="#FFFFFF",
            border_width=2.0 if in_hotkeys_tab else 1.5,
        )

        # 2. Main Card Content (Arisu 2D Card OR Interactive Hotkeys & Controls Sub-Tabs)
        if in_hotkeys_tab:
            painter.setBrush(QColor(22, 38, 74, 245))
            painter.setPen(QPen(QColor("#E0E8F5"), 2.0))
            painter.drawRoundedRect(QRectF(self.card_rect), self.CARD_RADIUS, self.CARD_RADIUS)

            painter.setFont(dock_font)
            is_pintasan = self.active_subtab == "pintasan"
            self._paint_dock_button(
                painter,
                self.subtab_hotkeys_rect,
                "subtab_pintasan",
                "PINTASAN",
                base_bg="#1C3F7A" if is_pintasan else "#0F1B36",
                hover_bg="#27539A" if is_pintasan else "#21396E",
                press_bg="#132B54",
                base_border="#E0E8F5" if is_pintasan else "#BAC2DC",
                hover_border="#FFFFFF",
                border_width=2.0 if is_pintasan else 1.5,
            )
            self._paint_dock_button(
                painter,
                self.subtab_controls_rect,
                "subtab_kontrol",
                "KONTROL",
                base_bg="#1C3F7A" if not is_pintasan else "#0F1B36",
                hover_bg="#27539A" if not is_pintasan else "#21396E",
                press_bg="#132B54",
                base_border="#E0E8F5" if not is_pintasan else "#BAC2DC",
                hover_border="#FFFFFF",
                border_width=2.0 if not is_pintasan else 1.5,
            )
            self._paint_hotkey_rows(
                painter,
                self.HOTKEY_ITEMS if is_pintasan else self.CONTROL_ITEMS,
            )
        else:
            if self._override_card_png:
                card_png = self._override_card_png
            elif self.mood_name and self.mood_name in MOOD_TO_CARD_PNG:
                card_png = MOOD_TO_CARD_PNG[self.mood_name]
            else:
                card_png = STATE_TO_CARD_PNG.get(self.state_name, "arisu_smile.png")
            pm = self._load_card_pixmap(card_png)

            clip = QPainterPath()
            clip.addRoundedRect(QRectF(self.card_rect), self.CARD_RADIUS, self.CARD_RADIUS)
            painter.save()
            painter.setClipPath(clip)
            if not pm.isNull():
                painter.drawPixmap(self.card_rect, pm)
            else:
                painter.fillRect(self.card_rect, QColor("#16264A"))
            painter.restore()

            hint_rect = QRect(36, 282, 208, 24)
            painter.setBrush(QColor(22, 38, 74, 220))
            painter.setPen(QPen(QColor("#BAC2DC"), 1.0))
            painter.drawRoundedRect(QRectF(hint_rect), 6.0, 6.0)
            hint_font = QFont("Segoe UI")
            hint_font.setPixelSize(13)
            hint_font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(hint_font)
            painter.setPen(QColor("#E0E8F5"))
            painter.drawText(hint_rect, Qt.AlignmentFlag.AlignCenter, "Ctrl+Alt+P Jeda · Esc Stop")

            if (
                self._pending_approval is not None
                or self._pending_handoff is not None
                or self.is_paused
                or self.state_name == "error"
            ):
                border_color = QColor("#FCE3E6")
            elif self.state_name in ("working", "searching", "thinking"):
                border_color = QColor("#E0E8F5")
            else:
                border_color = QColor("#BAC2DC")
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(border_color, 2.0))
            painter.drawRoundedRect(QRectF(self.card_rect), self.CARD_RADIUS, self.CARD_RADIUS)

        # 3. Control Dock (Approval/Hand-off Gate or Pause/Stop, >= 44x44 px touch targets)
        painter.setFont(dock_font)
        if self._pending_approval is not None or self._pending_handoff is not None:
            pos_label = "✓ IZINKAN" if self._pending_approval is not None else "✓ LANJUT"
            neg_label = "✕ TOLAK" if self._pending_approval is not None else "✕ BATAL"
            self._paint_dock_button(
                painter,
                self.pause_rect,
                "pause",
                pos_label,
                base_bg="#1C3F7A",
                hover_bg="#27539A",
                press_bg="#132B54",
                base_border="#E0E8F5",
                hover_border="#FFFFFF",
                border_width=2.0,
            )
            self._paint_dock_button(
                painter,
                self.stop_rect,
                "stop",
                neg_label,
                base_bg="#16264A",
                hover_bg="#21396E",
                press_bg="#0F1B36",
                base_border="#F5C8D0",
                hover_border="#FCE3E6",
                border_width=2.0,
            )
        else:
            self._paint_dock_button(
                painter,
                self.pause_rect,
                "pause",
                "RESUME" if self.is_paused else "PAUSE",
                base_bg="#1C3F7A" if self.is_paused else "#16264A",
                hover_bg="#27539A" if self.is_paused else "#21396E",
                press_bg="#132B54" if self.is_paused else "#0F1B36",
                base_border="#BAC2DC",
                hover_border="#E0E8F5",
                border_width=1.5,
            )
            self._paint_dock_button(
                painter,
                self.stop_rect,
                "stop",
                "STOP",
                base_bg="#1C3F7A",
                hover_bg="#27539A",
                press_bg="#132B54",
                base_border="#F5C8D0",
                hover_border="#FCE3E6",
                border_width=2.0,
            )

        painter.end()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._blink_timer.stop()
        self._blink_step_timer.stop()
        self._bubble_hide_timer.stop()
        self._sleepy_timer.stop()
        self._fullscreen_timer.stop()
        self._stop_close_timer.stop()
        if self._cursor_overlay is not None:
            with contextlib.suppress(Exception):
                self._cursor_overlay.close()
            self._cursor_overlay = None
        if self._hotkeys_registered and self.winId():
            hwnd = int(self.winId())
            for hk_id in (
                HOTKEY_TOGGLE_VISIBILITY,
                HOTKEY_TOGGLE_PAUSE,
                HOTKEY_EMERGENCY_STOP,
                HOTKEY_TOGGLE_PRIVACY,
                HOTKEY_APPROVE_PENDING,
                HOTKEY_DENY_PENDING,
            ):
                with contextlib.suppress(Exception):
                    _user32.UnregisterHotKey(wintypes.HWND(hwnd), hk_id)
            self._hotkeys_registered = False
        if self._broker is not None:
            self._broker.stop()
        super().closeEvent(event)


def main() -> int:
    mutex = SingleInstanceMutex(DEFAULT_MUTEX_NAME)
    if not mutex.acquire():
        return 0

    app = QApplication(sys.argv)
    hud = ArisuHudWindow()
    hud.start_hud()
    try:
        return app.exec()
    finally:
        hud.close()
        mutex.release()


if __name__ == "__main__":
    raise SystemExit(main())
