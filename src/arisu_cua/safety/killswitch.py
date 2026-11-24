"""Safety primitives, structured error types, and abortable execution control."""

from __future__ import annotations

import contextlib
import ctypes
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

from arisu_cua.display.monitors import VirtualScreenRect, get_virtual_screen_rect


class ArisuError(RuntimeError):
    """Base error carrying a structured machine-readable error code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class AbortedByUserError(ArisuError):
    def __init__(self, message: str = "Execution aborted by user kill-switch") -> None:
        super().__init__("ABORTED_BY_USER", message)


class SessionPausedError(ArisuError):
    def __init__(self, message: str = "Session is currently paused") -> None:
        super().__init__("SESSION_PAUSED", message)


class WindowMismatchError(ArisuError):
    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            "WINDOW_MISMATCH_BLOCKED",
            f"Expected window matching '{expected}', "
            f"but active/target window is '{actual or '<none>'}'",
        )


class GuardrailApprovalRequiredError(ArisuError):
    def __init__(self, action_desc: str) -> None:
        super().__init__(
            "GUARDRAIL_APPROVAL_REQUIRED",
            f"Sensitive action '{action_desc}' is blocked without explicit human approval",
        )


class UIATimeoutError(ArisuError):
    def __init__(self, timeout_s: float) -> None:
        super().__init__(
            "UIA_TIMEOUT",
            f"UIA worker subprocess exceeded {timeout_s:.1f}s timeout and was killed/respawned",
        )


class ElementNotFoundError(ArisuError):
    def __init__(self, element_id: int) -> None:
        super().__init__(
            "ELEMENT_NOT_FOUND",
            f"Set-of-Mark element_id [{element_id}] not found in current capture registry; "
            "run arisu_cu_capture_screen(mode='som') first",
        )


class InvalidApprovalTokenError(ArisuError):
    def __init__(self, message: str = "Approval token is invalid, expired, or mismatched") -> None:
        super().__init__("INVALID_APPROVAL_TOKEN", message)


class RateLimitExceededError(ArisuError):
    def __init__(self, max_actions: int, window_s: float) -> None:
        super().__init__(
            "RATE_LIMIT_EXCEEDED",
            f"Mutating action rate exceeded {max_actions} actions within {window_s:.0f}s",
        )


class RepeatLoopDetectedError(ArisuError):
    def __init__(self, threshold: int) -> None:
        super().__init__(
            "REPEAT_LOOP_DETECTED",
            f"Detected {threshold} identical consecutive mutating actions without UI progress",
        )


class ActionCapReachedError(ArisuError):
    def __init__(self, cap: int) -> None:
        super().__init__(
            "ACTION_CAP_REACHED",
            f"Session reached hard cap of {cap} mutating actions; "
            "call arisu_cu_session_control(action='extend') or start a new session",
        )


class WindowBlockedByPolicyError(ArisuError):
    def __init__(self, window_title: str, matched_rule: str) -> None:
        super().__init__(
            "WINDOW_BLOCKED_BY_POLICY",
            f"Window '{window_title}' blocked by sensitive privacy policy ('{matched_rule}')",
        )


class InputIntegrityBlockedError(ArisuError):
    def __init__(self, target_window: str, target_rid: int, self_rid: int) -> None:
        super().__init__(
            "INPUT_INTEGRITY_BLOCKED",
            f"Window '{target_window}' runs at higher Windows integrity "
            f"(0x{target_rid:x} > 0x{self_rid:x} UIPI); GUI input cannot be approved on HUD. "
            "Use run_command CLI or arisu_cu_session_control(action='handoff').",
        )


class PasswordCheckUnavailableError(ArisuError):
    def __init__(self) -> None:
        super().__init__(
            "PASSWORD_CHECK_UNAVAILABLE",
            "Could not verify whether focused control is a password field "
            "(UIA timeout/error); typing blocked fail-closed",
        )


class InvalidParamComboError(ArisuError):
    def __init__(self, message: str) -> None:
        super().__init__("INVALID_PARAM_COMBO", message)


class InputDesktopUnavailableError(ArisuError):
    def __init__(self, winerr: int = 5) -> None:
        super().__init__(
            "INPUT_DESKTOP_UNAVAILABLE",
            f"Win32 SendInput blocked by active desktop/UAC/lock screen (winerr={winerr})",
        )


class UserMouseContentionError(ArisuError):
    def __init__(
        self,
        message: str = "Physical mouse moved by user; yielded AI pointer control",
        *,
        status: str = "idle",
    ) -> None:
        self.status = status
        super().__init__("USER_MOUSE_CONTENTION_YIELD", message)


class StaleScreenRegionError(ArisuError):
    def __init__(self, mean_diff: float, threshold: float = 42.0) -> None:
        super().__init__(
            "STALE_SCREEN_REGION_CHANGED",
            f"Target 64x64 patch changed before click "
            f"(mean_diff={mean_diff:.1f} > {threshold:.1f}); "
            "capture screen again before acting",
        )


class HandoffCancelledError(ArisuError):
    def __init__(self, message: str = "Co-pilot hand-off cancelled by user on HUD") -> None:
        super().__init__("HANDOFF_CANCELLED_BY_USER", message)


class HandoffTimeoutError(ArisuError):
    def __init__(self, timeout_s: float = 180.0) -> None:
        super().__init__(
            "HANDOFF_TIMEOUT",
            f"Co-pilot hand-off timed out after {timeout_s:.0f}s without user confirmation",
        )


class BrowserConnectionError(ArisuError):
    def __init__(self, message: str) -> None:
        super().__init__("BROWSER_CDP_UNAVAILABLE", message)


class BrowserElementNotFoundError(ArisuError):
    def __init__(self, target_desc: str) -> None:
        super().__init__(
            "BROWSER_ELEMENT_NOT_FOUND",
            f"DOM element '{target_desc}' not found in active browser tab; "
            "run arisu_cu_browser_action(action='snapshot') to inspect available elements",
        )


ABORT_EVENT = threading.Event()
PAUSED_EVENT = threading.Event()


def trigger_abort(reason: str = "Emergency abort triggered") -> None:
    ABORT_EVENT.set()


def reset_abort() -> None:
    ABORT_EVENT.clear()


def set_paused(paused: bool) -> None:
    if paused:
        PAUSED_EVENT.set()
    else:
        PAUSED_EVENT.clear()


def check_ready() -> None:
    if ABORT_EVENT.is_set():
        raise AbortedByUserError()
    if PAUSED_EVENT.is_set():
        raise SessionPausedError()


def abortable_sleep(seconds: float) -> None:
    """Wait up to `seconds`, returning immediately with AbortedByUserError on abort."""
    check_ready()
    if seconds <= 0:
        return
    if ABORT_EVENT.wait(timeout=seconds):
        raise AbortedByUserError("Wait interrupted by user abort")
    if PAUSED_EVENT.is_set():
        raise SessionPausedError("Session paused during wait")


def is_in_outer_corner(
    cursor_px: tuple[int, int],
    v_rect: VirtualScreenRect,
    margin_px: int = 2,
) -> bool:
    """Check outer top-left corner (v_left, v_top) rather than dual-monitor seam (0, 0)."""
    cx, cy = cursor_px
    return (
        v_rect.left <= cx <= v_rect.left + margin_px and v_rect.top <= cy <= v_rect.top + margin_px
    )


def _read_win32_cursor() -> tuple[int, int]:
    pt = wintypes.POINT()
    ctypes.WinDLL("user32", use_last_error=True).GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


class CornerFailsafeMonitor:
    """Abort session if cursor dwells in outer top-left corner (v_left, v_top) >= 300ms."""

    def __init__(
        self,
        dwell_s: float = 0.30,
        poll_s: float = 0.05,
        cursor_getter: Callable[[], tuple[int, int]] = _read_win32_cursor,
        vrect_getter: Callable[[], VirtualScreenRect] = get_virtual_screen_rect,
        on_trigger: Callable[[str], None] | None = None,
    ) -> None:
        self.dwell_s = float(dwell_s)
        self.poll_s = float(poll_s)
        self._cursor_getter = cursor_getter
        self._vrect_getter = vrect_getter
        self._on_trigger = on_trigger
        self._stop_ev = threading.Event()
        self._thread: threading.Thread | None = None
        self._corner_since: float | None = None

    def check_once(self, now: float | None = None) -> bool:
        """Evaluate a single cursor sample; returns True when corner failsafe triggers."""
        ts = time.monotonic() if now is None else float(now)
        try:
            cursor = self._cursor_getter()
            vrect = self._vrect_getter()
        except Exception:
            return False

        if is_in_outer_corner(cursor, vrect):
            if self._corner_since is None:
                self._corner_since = ts
            elif (ts - self._corner_since) >= self.dwell_s:
                reason = "Outer corner failsafe dwell (v_left, v_top) >= 300ms"
                trigger_abort(reason)
                if self._on_trigger is not None:
                    with contextlib.suppress(Exception):
                        self._on_trigger(reason)
                self._corner_since = None
                return True
        else:
            self._corner_since = None
        return False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_ev.clear()
        self._corner_since = None

        def _loop() -> None:
            while not self._stop_ev.is_set():
                self.check_once()
                self._stop_ev.wait(timeout=self.poll_s)

        self._thread = threading.Thread(target=_loop, daemon=True, name="arisu-corner-failsafe")
        self._thread.start()

    def stop(self) -> None:
        self._stop_ev.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._corner_since = None
