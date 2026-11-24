"""Transparent click-through AI cursor ring and click ripple overlay for S6 Co-Pilot UX."""

from __future__ import annotations

import contextlib
import ctypes
import time
from ctypes import wintypes

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QColor, QFont, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QApplication, QWidget

from arisu_cua.display.monitors import enumerate_monitors, find_monitor_for_point
from arisu_cua.input.win32_input import get_cursor_pos

_user32 = ctypes.WinDLL("user32", use_last_error=True)

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

WM_NCHITTEST = 0x0084
HTTRANSPARENT = -1


class ArisuCursorOverlay(QWidget):
    """88x88 transparent click-through visual AI cursor indicator and click ripple."""

    SIZE = 88

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedSize(self.SIZE, self.SIZE)

        self._styles_applied: bool = False
        self._action: str = "move"
        self._target_px: tuple[int, int] = (0, 0)
        self._duration_s: float = 0.18
        self._cue_start_ts: float = 0.0
        self._ripple_start_ts: float | None = None
        self._ripple_progress: float = 0.0
        self._is_click_action: bool = False

        self._cue_timer = QTimer(self)
        self._cue_timer.setInterval(16)
        self._cue_timer.timeout.connect(self._tick)

    def apply_win32_styles(self) -> None:
        if self._styles_applied:
            return
        hwnd = int(self.winId())
        if not hwnd:
            return
        _user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        _user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t

        ex_style = _user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE)
        ex_style |= (
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        )
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
        with contextlib.suppress(Exception):
            _user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), WDA_EXCLUDEFROMCAPTURE)
        self._styles_applied = True

    def nativeEvent(self, eventType: bytes, message: int) -> tuple[bool, int]:
        if bytes(eventType) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_NCHITTEST:
                return True, HTTRANSPARENT
        return super().nativeEvent(eventType, message)

    def _physical_to_logical(self, px_x: int, px_y: int) -> tuple[int, int]:
        """Convert Win32 physical screen pixels to Qt logical coordinates for QWidget.move()."""
        with contextlib.suppress(Exception):
            monitors = enumerate_monitors()
            mon = find_monitor_for_point(monitors, int(px_x), int(px_y))
            if mon is not None:
                app = QApplication.instance()
                if app is not None:
                    for screen in app.screens():
                        if screen.name() == mon.device:
                            dpr = max(float(screen.devicePixelRatio()), 0.5)
                            geo = screen.geometry()
                            lx = geo.x() + round((int(px_x) - mon.left) / dpr)
                            ly = geo.y() + round((int(px_y) - mon.top) / dpr)
                            return lx, ly
                scale = max(float(mon.scale_pct) / 100.0, 0.5)
                return round(int(px_x) / scale), round(int(px_y) / scale)
        return int(px_x), int(px_y)

    def _center_on(self, px_x: int, px_y: int) -> None:
        half = self.SIZE // 2
        lx, ly = self._physical_to_logical(px_x, px_y)
        self.move(lx - half, ly - half)

    def show_cue(
        self,
        action: str,
        target_px: tuple[int, int],
        duration_ms: int = 180,
        show_window: bool = True,
    ) -> None:
        self._action = str(action or "move")
        self._target_px = (int(target_px[0]), int(target_px[1]))
        self._duration_s = max(int(duration_ms), 80) / 1000.0
        self._cue_start_ts = time.monotonic()
        self._ripple_start_ts = None
        self._ripple_progress = 0.0
        self._is_click_action = self._action in ("click", "double_click", "right_click")

        try:
            cx, cy = get_cursor_pos()
        except Exception:
            cx, cy = self._target_px

        self._center_on(cx, cy)
        if show_window:
            self.createWinId()
            self.apply_win32_styles()
            self.show()
        self._cue_timer.start()
        self.update()

    def _tick(self) -> None:
        now = time.monotonic()
        elapsed = now - self._cue_start_ts

        if self._ripple_start_ts is None:
            if elapsed < self._duration_s:
                with contextlib.suppress(Exception):
                    cx, cy = get_cursor_pos()
                    self._center_on(cx, cy)
                self.update()
                return

            self._center_on(self._target_px[0], self._target_px[1])
            if self._is_click_action:
                self._ripple_start_ts = now
                self._ripple_progress = 0.0
                self.update()
                return

            self._cue_timer.stop()
            self.hide()
            return

        ripple_elapsed = now - self._ripple_start_ts
        if ripple_elapsed >= 0.18:
            self._cue_timer.stop()
            self._ripple_start_ts = None
            self._ripple_progress = 0.0
            self.hide()
            return

        self._ripple_progress = min(1.0, max(0.0, ripple_elapsed / 0.18))
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        center = QPointF(self.SIZE / 2.0, self.SIZE / 2.0)

        if self._ripple_start_ts is not None:
            radius = 10.0 + 20.0 * self._ripple_progress
            alpha = max(0, round(230 * (1.0 - self._ripple_progress)))
            ripple_color = QColor("#FCE3E6")
            ripple_color.setAlpha(alpha)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(ripple_color, 3.0))
            painter.drawEllipse(center, radius, radius)

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#1C3F7A"), 3.0))
        painter.drawEllipse(center, 18.0, 18.0)
        painter.setPen(QPen(QColor("#E0E8F5"), 1.8))
        painter.drawEllipse(center, 14.0, 14.0)

        badge_rect = QRect(22, 62, 44, 20)
        painter.setBrush(QColor(28, 63, 122, 235))
        painter.setPen(QPen(QColor("#E0E8F5"), 1.2))
        painter.drawRoundedRect(QRectF(badge_rect), 6.0, 6.0)

        font = QFont("Segoe UI")
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, "ARISU")
        painter.end()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._cue_timer.stop()
        super().closeEvent(event)
