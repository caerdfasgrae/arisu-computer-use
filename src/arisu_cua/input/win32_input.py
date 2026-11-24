"""Win32 SendInput driver for mouse, Unicode keyboard, and scroll with abort checks."""

from __future__ import annotations

import contextlib
import ctypes
import math
import time
from collections.abc import Callable
from ctypes import wintypes
from typing import Literal

from arisu_cua.config import CLIPBOARD_PASTE_THRESHOLD
from arisu_cua.display.monitors import (
    VirtualScreenRect,
    get_virtual_screen_rect,
    physical_to_virtualdesk,
)
from arisu_cua.safety.killswitch import (
    GuardrailApprovalRequiredError,
    InputDesktopUnavailableError,
    UserMouseContentionError,
    abortable_sleep,
    check_ready,
)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.argtypes = []
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.argtypes = []
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE

_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL

_CANONICAL_ALIASES: dict[str, str] = {
    "control": "ctrl",
    "lwin": "win",
    "rwin": "win",
    "windows": "win",
    "del": "delete",
    "return": "enter",
    "escape": "esc",
}

BLOCKED_HOTKEYS: set[frozenset[str]] = {
    frozenset({"ctrl", "w"}),
    frozenset({"ctrl", "f4"}),
    frozenset({"alt", "f4"}),
    frozenset({"ctrl", "shift", "w"}),
    frozenset({"ctrl", "q"}),
    frozenset({"shift", "delete"}),
    frozenset({"win", "r"}),
    frozenset({"ctrl", "alt", "delete"}),
}

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x01000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

WHEEL_DELTA = 120

VK_MAP: dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "pause": 0x13,
    "capslock": 0x14,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pgup": 0x21,
    "pageup": 0x21,
    "pgdn": 0x22,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "insert": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "win": 0x5B,
    "lwin": 0x5B,
    "windows": 0x5B,
    "rwin": 0x5C,
    "apps": 0x5D,
    "numpad0": 0x60,
    "numpad1": 0x61,
    "numpad2": 0x62,
    "numpad3": 0x63,
    "numpad4": 0x64,
    "numpad5": 0x65,
    "numpad6": 0x66,
    "numpad7": 0x67,
    "numpad8": 0x68,
    "numpad9": 0x69,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
    "semicolon": 0xBA,
    "plus": 0xBB,
    "comma": 0xBC,
    "minus": 0xBD,
    "period": 0xBE,
    "slash": 0xBF,
    "tilde": 0xC0,
    "lbracket": 0xDB,
    "backslash": 0xDC,
    "rbracket": 0xDD,
    "quote": 0xDE,
}

EXTENDED_VKS = {
    0x21,
    0x22,
    0x23,
    0x24,
    0x25,
    0x26,
    0x27,
    0x28,
    0x2D,
    0x2E,
    0x5B,
    0x5C,
    0x5D,
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUT_UNION)]


def _send_inputs(inputs: list[INPUT]) -> int:
    if not inputs:
        return 0
    arr = (INPUT * len(inputs))(*inputs)
    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    _user32.SendInput.restype = wintypes.UINT
    sent = int(_user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)))
    if sent != len(inputs):
        err = ctypes.get_last_error()
        if err == 5:
            raise InputDesktopUnavailableError(err)
        raise OSError(f"SendInput failed (sent {sent}/{len(inputs)}, winerr={err})")
    return sent


def get_cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def _mouse_input(dx: int, dy: int, flags: int, mouse_data: int = 0) -> INPUT:
    return INPUT(
        type=INPUT_MOUSE,
        union=_INPUT_UNION(
            mi=MOUSEINPUT(
                dx=dx,
                dy=dy,
                mouseData=mouse_data,
                dwFlags=flags,
                time=0,
                dwExtraInfo=0,
            )
        ),
    )


def _vk_input(vk: int, key_up: bool = False) -> INPUT:
    flags = KEYEVENTF_KEYUP if key_up else 0
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    return INPUT(
        type=INPUT_KEYBOARD,
        union=_INPUT_UNION(ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)),
    )


def _unicode_inputs(char: str) -> list[INPUT]:
    if char == "\n":
        return [_vk_input(VK_MAP["enter"], False), _vk_input(VK_MAP["enter"], True)]
    if char == "\t":
        return [_vk_input(VK_MAP["tab"], False), _vk_input(VK_MAP["tab"], True)]
    utf16 = char.encode("utf-16-le")
    events: list[INPUT] = []
    for i in range(0, len(utf16), 2):
        code_unit = int.from_bytes(utf16[i : i + 2], "little")
        events.append(
            INPUT(
                type=INPUT_KEYBOARD,
                union=_INPUT_UNION(
                    ki=KEYBDINPUT(
                        wVk=0, wScan=code_unit, dwFlags=KEYEVENTF_UNICODE, time=0, dwExtraInfo=0
                    )
                ),
            )
        )
        events.append(
            INPUT(
                type=INPUT_KEYBOARD,
                union=_INPUT_UNION(
                    ki=KEYBDINPUT(
                        wVk=0,
                        wScan=code_unit,
                        dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                        time=0,
                        dwExtraInfo=0,
                    )
                ),
            )
        )
    return events


def _canonical_key(key_name: str) -> str:
    key = key_name.strip().lower()
    return _CANONICAL_ALIASES.get(key, key)


def resolve_vk(key_name: str) -> int:
    key = key_name.strip().lower()
    if key in VK_MAP:
        return VK_MAP[key]
    if len(key) == 1:
        ch = key.upper()
        if "A" <= ch <= "Z" or "0" <= ch <= "9":
            return ord(ch)
        res = _user32.VkKeyScanW(ord(key_name.strip()))
        vk = res & 0xFF
        if res != -1 and vk != 0:
            return int(vk)
    raise ValueError(f"INVALID_KEY: Unknown key '{key_name}'")


def _wait_for_user_mouse_idle(
    yield_stable_s: float = 1.2,
    yield_timeout_s: float = 5.0,
    poll_s: float | None = None,
) -> None:
    """Yield pointer control until user stops moving the physical mouse or timeout expires."""
    interval = float(poll_s) if poll_s is not None else min(0.08, max(0.005, yield_stable_s / 4.0))
    start_ts = time.monotonic()
    last_x, last_y = get_cursor_pos()
    stable_since = start_ts
    idle_reached = False
    while True:
        abortable_sleep(interval)
        now = time.monotonic()
        cx, cy = get_cursor_pos()
        if math.hypot(cx - last_x, cy - last_y) > 3.0:
            last_x, last_y = cx, cy
            stable_since = now
        elif (now - stable_since) >= yield_stable_s:
            idle_reached = True
            break
        if (now - start_ts) >= yield_timeout_s:
            break
    if idle_reached:
        raise UserMouseContentionError(
            "Physical mouse moved by user and is now idle; re-capture screen before retrying",
            status="idle",
        )
    raise UserMouseContentionError(
        "Physical mouse still moving after yield timeout; yielded AI pointer control",
        status="timeout",
    )


def _glide_pointer_to(
    target_x: int,
    target_y: int,
    vrect: VirtualScreenRect,
    smooth: bool = True,
    contention_threshold_px: float = 25.0,
    yield_stable_s: float = 1.2,
    yield_timeout_s: float = 5.0,
    on_contention: Callable[[], None] | None = None,
) -> dict[str, int]:
    """Glide pointer via Cubic Ease-Out while yielding cleanly on user mouse movement."""
    abs_base = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    start_x, start_y = get_cursor_pos()
    dist = math.hypot(target_x - start_x, target_y - start_y)
    if not smooth or dist <= 8.0:
        ax, ay = physical_to_virtualdesk(target_x, target_y, vrect)
        _send_inputs([_mouse_input(ax, ay, abs_base | MOUSEEVENTF_MOVE)])
        return {"glide_steps": 1, "glide_ms": 0}

    duration_ms = min(max(round(100 + dist * 0.14), 120), 280)
    steps = min(max(round(duration_ms / 12), 14), 22)
    step_sleep_s = (duration_ms / 1000.0) / steps

    for step in range(1, steps + 1):
        check_ready()
        t = step / steps
        u = 1.0 - (1.0 - t) ** 3
        sx = round(start_x + (target_x - start_x) * u)
        sy = round(start_y + (target_y - start_y) * u)
        sax, say = physical_to_virtualdesk(sx, sy, vrect)
        _send_inputs([_mouse_input(sax, say, abs_base | MOUSEEVENTF_MOVE)])
        post_send_x, post_send_y = get_cursor_pos()
        abortable_sleep(step_sleep_s)
        after_sleep_x, after_sleep_y = get_cursor_pos()
        if math.hypot(after_sleep_x - post_send_x, after_sleep_y - post_send_y) > (
            contention_threshold_px
        ):
            if on_contention is not None:
                with contextlib.suppress(Exception):
                    on_contention()
            _wait_for_user_mouse_idle(
                yield_stable_s=yield_stable_s,
                yield_timeout_s=yield_timeout_s,
            )

    return {"glide_steps": steps, "glide_ms": duration_ms}


def execute_mouse_action(
    action: Literal["click", "double_click", "right_click", "move", "drag"],
    px_x: int,
    px_y: int,
    end_px_x: int | None = None,
    end_px_y: int | None = None,
    v_rect: VirtualScreenRect | None = None,
    smooth: bool = True,
    on_contention: Callable[[], None] | None = None,
    yield_stable_s: float = 1.2,
    yield_timeout_s: float = 5.0,
) -> dict:
    """Execute Win32 mouse action using Cubic Ease-Out glide and VirtualDesk coordinates."""
    check_ready()
    vrect = v_rect or get_virtual_screen_rect()
    abs_base = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    ax, ay = physical_to_virtualdesk(px_x, px_y, vrect)

    glide_meta = _glide_pointer_to(
        px_x,
        px_y,
        vrect,
        smooth=smooth,
        yield_stable_s=yield_stable_s,
        yield_timeout_s=yield_timeout_s,
        on_contention=on_contention,
    )
    if action == "move":
        return {"action": action, "px": [px_x, px_y], **glide_meta}

    abortable_sleep(0.02)

    if action == "click":
        _send_inputs(
            [
                _mouse_input(ax, ay, abs_base | MOUSEEVENTF_LEFTDOWN),
                _mouse_input(ax, ay, abs_base | MOUSEEVENTF_LEFTUP),
            ]
        )
        return {"action": action, "px": [px_x, px_y], **glide_meta}

    if action == "double_click":
        _send_inputs(
            [
                _mouse_input(ax, ay, abs_base | MOUSEEVENTF_LEFTDOWN),
                _mouse_input(ax, ay, abs_base | MOUSEEVENTF_LEFTUP),
            ]
        )
        abortable_sleep(0.04)
        _send_inputs(
            [
                _mouse_input(ax, ay, abs_base | MOUSEEVENTF_LEFTDOWN),
                _mouse_input(ax, ay, abs_base | MOUSEEVENTF_LEFTUP),
            ]
        )
        return {"action": action, "px": [px_x, px_y], **glide_meta}

    if action == "right_click":
        _send_inputs(
            [
                _mouse_input(ax, ay, abs_base | MOUSEEVENTF_RIGHTDOWN),
                _mouse_input(ax, ay, abs_base | MOUSEEVENTF_RIGHTUP),
            ]
        )
        return {"action": action, "px": [px_x, px_y], **glide_meta}

    if action == "drag":
        if end_px_x is None or end_px_y is None:
            raise ValueError("INVALID_DRAG: end_x and end_y are required for drag")
        steps = 12
        _send_inputs([_mouse_input(ax, ay, abs_base | MOUSEEVENTF_LEFTDOWN)])
        try:
            for step in range(1, steps + 1):
                check_ready()
                sx = round(px_x + (end_px_x - px_x) * (step / steps))
                sy = round(px_y + (end_px_y - px_y) * (step / steps))
                sax, say = physical_to_virtualdesk(sx, sy, vrect)
                _send_inputs([_mouse_input(sax, say, abs_base | MOUSEEVENTF_MOVE)])
                abortable_sleep(0.015)
        finally:
            ex, ey = physical_to_virtualdesk(end_px_x, end_px_y, vrect)
            _send_inputs([_mouse_input(ex, ey, abs_base | MOUSEEVENTF_LEFTUP)])
        return {
            "action": action,
            "px": [px_x, px_y],
            "end_px": [end_px_x, end_px_y],
            **glide_meta,
        }

    raise ValueError(f"INVALID_ACTION: Unsupported mouse action '{action}'")


def _open_clipboard_with_retry(retries: int = 5, delay_s: float = 0.01) -> bool:
    for attempt in range(retries):
        if _user32.OpenClipboard(None):
            return True
        if attempt + 1 < retries:
            time.sleep(delay_s)
    return False


def _get_clipboard_unicode_text() -> str | None:
    if not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    if not _open_clipboard_with_retry():
        return None
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(handle)
    except Exception:
        return None
    finally:
        with contextlib.suppress(Exception):
            _user32.CloseClipboard()


def _set_clipboard_unicode_text(text: str) -> bool:
    crlf = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    raw = crlf.encode("utf-16-le", errors="replace") + b"\x00\x00"
    if not _open_clipboard_with_retry():
        return False
    h_mem = None
    try:
        if not _user32.EmptyClipboard():
            return False
        h_mem = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw))
        if not h_mem:
            return False
        ptr = _kernel32.GlobalLock(h_mem)
        if not ptr:
            _kernel32.GlobalFree(h_mem)
            return False
        try:
            ctypes.memmove(ptr, raw, len(raw))
        finally:
            _kernel32.GlobalUnlock(h_mem)
        if not _user32.SetClipboardData(CF_UNICODETEXT, h_mem):
            _kernel32.GlobalFree(h_mem)
            return False
        h_mem = None
        return True
    except Exception:
        if h_mem:
            with contextlib.suppress(Exception):
                _kernel32.GlobalFree(h_mem)
        return False
    finally:
        with contextlib.suppress(Exception):
            _user32.CloseClipboard()


def _paste_text_via_clipboard(text: str) -> bool:
    check_ready()
    prev_text = _get_clipboard_unicode_text()
    if not _set_clipboard_unicode_text(text):
        return False
    ctrl_vk = VK_MAP["ctrl"]
    v_vk = ord("V")
    pressed: list[int] = []
    try:
        _send_inputs([_vk_input(ctrl_vk, False)])
        pressed.append(ctrl_vk)
        _send_inputs([_vk_input(v_vk, False)])
        pressed.append(v_vk)
        abortable_sleep(0.01)
    finally:
        if pressed:
            _send_inputs([_vk_input(vk, True) for vk in reversed(pressed)])
    try:
        abortable_sleep(0.12)
        return True
    finally:
        if prev_text is not None:
            with contextlib.suppress(Exception):
                _set_clipboard_unicode_text(prev_text)


def execute_keyboard_action(
    action: Literal["type", "press", "hotkey"],
    text: str | None = None,
    keys: list[str] | None = None,
    bypass_hotkey_block: bool = False,
    typing_mode: Literal["natural", "fast", "paste"] = "natural",
    allow_clipboard: bool = True,
) -> dict:
    """Execute Unicode typing, clipboard paste, single key press, or modifier hotkey."""
    check_ready()

    if action == "type":
        if text is None:
            raise ValueError("INVALID_KEYBOARD: 'text' is required for action='type'")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        total = len(normalized)
        use_paste = allow_clipboard and (
            typing_mode == "paste" or total >= CLIPBOARD_PASTE_THRESHOLD
        )
        if use_paste and _paste_text_via_clipboard(normalized):
            return {
                "action": "type",
                "chars": total,
                "typing_mode": typing_mode,
                "delivery": "clipboard_paste",
            }
        base_ms = 28.0 if total <= 80 else max(14.0, 28.0 - (total - 80) * 0.1)
        for idx, ch in enumerate(normalized):
            check_ready()
            if typing_mode == "natural" and ch == "\n" and idx > 0:
                abortable_sleep(0.120)
            _send_inputs(_unicode_inputs(ch))
            if idx + 1 < total:
                if typing_mode == "fast":
                    abortable_sleep(0.012)
                else:
                    extra_s = (
                        0.012 if ch in (" ", ".", ",", ":", ";", "/", "@", "-", "?", "!") else 0.0
                    )
                    abortable_sleep((base_ms / 1000.0) + extra_s)
        return {
            "action": "type",
            "chars": total,
            "typing_mode": typing_mode,
            "delivery": "sendinput",
        }

    if action == "press":
        if not keys or len(keys) != 1:
            raise ValueError("INVALID_KEYBOARD: 'keys' must contain exactly 1 key for 'press'")
        vk = resolve_vk(keys[0])
        _send_inputs([_vk_input(vk, False), _vk_input(vk, True)])
        return {"action": "press", "keys": keys}

    if action == "hotkey":
        if not keys:
            raise ValueError("INVALID_KEYBOARD: 'keys' cannot be empty for 'hotkey'")
        canonical_combo = frozenset(_canonical_key(k) for k in keys)
        if canonical_combo in BLOCKED_HOTKEYS and not bypass_hotkey_block:
            raise GuardrailApprovalRequiredError(f"hotkey {'+'.join(keys)}")
        vks = [resolve_vk(k) for k in keys]
        pressed: list[int] = []
        try:
            for vk in vks:
                _send_inputs([_vk_input(vk, False)])
                pressed.append(vk)
                abortable_sleep(0.01)
        finally:
            if pressed:
                _send_inputs([_vk_input(vk, True) for vk in reversed(pressed)])
        return {"action": "hotkey", "keys": keys}

    raise ValueError(f"INVALID_ACTION: Unsupported keyboard action '{action}'")


def execute_scroll_action(
    px_x: int,
    px_y: int,
    direction: Literal["up", "down", "left", "right"],
    amount: int = 3,
    v_rect: VirtualScreenRect | None = None,
) -> dict:
    """Move cursor to target and emit vertical or horizontal mouse wheel events."""
    check_ready()
    if amount < 1 or amount > 50:
        raise ValueError("INVALID_SCROLL: amount must be between 1 and 50")
    vrect = v_rect or get_virtual_screen_rect()
    abs_base = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    ax, ay = physical_to_virtualdesk(px_x, px_y, vrect)
    _send_inputs([_mouse_input(ax, ay, abs_base | MOUSEEVENTF_MOVE)])
    abortable_sleep(0.015)

    delta = WHEEL_DELTA * amount
    if direction == "up":
        flag, signed_delta = MOUSEEVENTF_WHEEL, delta
    elif direction == "down":
        flag, signed_delta = MOUSEEVENTF_WHEEL, -delta
    elif direction == "right":
        flag, signed_delta = MOUSEEVENTF_HWHEEL, delta
    elif direction == "left":
        flag, signed_delta = MOUSEEVENTF_HWHEEL, -delta
    else:
        raise ValueError(f"INVALID_SCROLL: Unsupported direction '{direction}'")

    dword_delta = ctypes.c_uint32(signed_delta).value
    _send_inputs([_mouse_input(ax, ay, flag, mouse_data=dword_delta)])
    return {"direction": direction, "amount": amount, "px": [px_x, px_y]}
