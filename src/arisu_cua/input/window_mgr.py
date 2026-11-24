"""Windows top-level window enumeration, focus lock handling, and dual-monitor movement."""

from __future__ import annotations

import contextlib
import ctypes
import os
import subprocess
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from arisu_cua.config import WINDOW_ENUM_LIMIT
from arisu_cua.display.monitors import MonitorInfo, find_monitor_for_point, resolve_monitor
from arisu_cua.safety.killswitch import (
    GuardrailApprovalRequiredError,
    WindowMismatchError,
    abortable_sleep,
)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

_advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
_advapi32.OpenProcessToken.restype = wintypes.BOOL
_advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_advapi32.GetTokenInformation.restype = wintypes.BOOL
_advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
_advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
_advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

INTEGRITY_LOW = 0x1000
INTEGRITY_MEDIUM = 0x2000
INTEGRITY_HIGH = 0x3000
INTEGRITY_SYSTEM = 0x4000
INTEGRITY_PROTECTED = 0x5000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_INTEGRITY_LEVEL = 25

_PID_INTEGRITY_CACHE: dict[int, int | None] = {}
_SELF_INTEGRITY_RID: int | None = None

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_CLOAKED = 14
GA_ROOT = 2

SW_HIDE = 0
SW_MAXIMIZE = 3
SW_MINIMIZE = 6
SW_RESTORE = 9

SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010

SAFE_LAUNCH_MAP: dict[str, str] = {
    "notepad": "notepad.exe",
    "notepad.exe": "notepad.exe",
    "calc": "calc.exe",
    "calc.exe": "calc.exe",
    "mspaint": "mspaint.exe",
    "mspaint.exe": "mspaint.exe",
    "explorer": "explorer.exe",
    "explorer.exe": "explorer.exe",
}

_LAUNCH_TITLE_HINTS: dict[str, tuple[str, ...]] = {
    "notepad.exe": ("notepad",),
    "calc.exe": ("calculator", "kalkulator"),
    "mspaint.exe": ("paint",),
    "explorer.exe": ("file explorer", "explorer"),
}


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    pid: int
    left: int
    top: int
    right: int
    bottom: int
    monitor_index: int
    is_maximized: bool
    is_minimized: bool
    integrity_rid: int | None = None
    is_elevated: bool | None = None

    @property
    def monitor(self) -> int:
        return self.monitor_index

    def to_dict(self) -> dict:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "pid": self.pid,
            "rect": [self.left, self.top, self.right, self.bottom],
            "monitor": self.monitor_index,
            "is_maximized": self.is_maximized,
            "is_minimized": self.is_minimized,
            "integrity_rid": self.integrity_rid,
            "is_elevated": self.is_elevated,
        }


def get_pid_integrity_rid(pid: int) -> int | None:
    """Query a process's Windows Mandatory Integrity Level RID (e.g. 0x2000 Medium, 0x3000 High)."""
    if pid <= 0:
        return None
    if pid in _PID_INTEGRITY_CACHE:
        return _PID_INTEGRITY_CACHE[pid]

    h_proc = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h_proc:
        if len(_PID_INTEGRITY_CACHE) > 512:
            _PID_INTEGRITY_CACHE.clear()
        _PID_INTEGRITY_CACHE[pid] = None
        return None

    h_tok = wintypes.HANDLE()
    rid: int | None = None
    try:
        if _advapi32.OpenProcessToken(h_proc, _TOKEN_QUERY, ctypes.byref(h_tok)):
            sz = wintypes.DWORD(0)
            _advapi32.GetTokenInformation(h_tok, _TOKEN_INTEGRITY_LEVEL, None, 0, ctypes.byref(sz))
            if sz.value > 0:
                buf = ctypes.create_string_buffer(sz.value)
                if _advapi32.GetTokenInformation(
                    h_tok, _TOKEN_INTEGRITY_LEVEL, buf, sz.value, ctypes.byref(sz)
                ):
                    sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p)).contents.value
                    if sid_ptr:
                        cnt_ptr = _advapi32.GetSidSubAuthorityCount(sid_ptr)
                        cnt = int(cnt_ptr.contents.value) if cnt_ptr else 0
                        if cnt > 0:
                            rid_ptr = _advapi32.GetSidSubAuthority(sid_ptr, cnt - 1)
                            if rid_ptr:
                                rid = int(rid_ptr.contents.value)
    except Exception:
        rid = None
    finally:
        if h_tok.value:
            with contextlib.suppress(Exception):
                _kernel32.CloseHandle(h_tok)
        with contextlib.suppress(Exception):
            _kernel32.CloseHandle(h_proc)

    if len(_PID_INTEGRITY_CACHE) > 512:
        _PID_INTEGRITY_CACHE.clear()
    _PID_INTEGRITY_CACHE[pid] = rid
    return rid


def get_self_integrity_rid() -> int | None:
    """Return the Mandatory Integrity Level RID of the current arisu-cua Python process."""
    global _SELF_INTEGRITY_RID
    if _SELF_INTEGRITY_RID is None:
        _SELF_INTEGRITY_RID = get_pid_integrity_rid(os.getpid())
    return _SELF_INTEGRITY_RID


def is_pid_elevated_over_self(pid: int) -> bool | None:
    """Tri-state UIPI comparison: True if target_rid > self_rid, None if unverifiable."""
    self_rid = get_self_integrity_rid()
    target_rid = get_pid_integrity_rid(pid)
    if self_rid is None or target_rid is None:
        return None
    return target_rid > self_rid


def get_pid_for_hwnd(hwnd: int) -> int:
    """Return the owning process ID for a Windows HWND, or 0 if invalid."""
    if not hwnd:
        return 0
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def get_hwnd_at_point(px_x: int, px_y: int) -> int:
    """Resolve top-level root HWND directly under physical screen coordinates."""
    _user32.WindowFromPoint.argtypes = [wintypes.POINT]
    _user32.WindowFromPoint.restype = wintypes.HWND
    _user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetAncestor.restype = wintypes.HWND

    hwnd = _user32.WindowFromPoint(wintypes.POINT(int(px_x), int(px_y)))
    if not hwnd:
        return 0
    root = _user32.GetAncestor(hwnd, GA_ROOT)
    return int(root) if root else int(hwnd)


def _is_cloaked(hwnd: int) -> bool:
    cloaked = wintypes.DWORD(0)
    try:
        hr = _dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd),
            DWMWA_CLOAKED,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        return hr == 0 and cloaked.value != 0
    except Exception:
        return False


def get_window_title(hwnd: int) -> str:
    """Read visible window title for hwnd (returns empty string if invalid/zero)."""
    if not hwnd:
        return ""
    length = int(_user32.GetWindowTextLengthW(wintypes.HWND(hwnd)))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 1)
    return buf.value.strip()


def get_foreground_window_title() -> str:
    return get_window_title(int(_user32.GetForegroundWindow()))


def get_window_title_at_point(px_x: int, px_y: int) -> str:
    """Resolve top-level root window title directly under physical screen coordinates."""
    hwnd = get_hwnd_at_point(px_x, px_y)
    return get_window_title(hwnd) if hwnd else ""


TRANSIENT_POPUP_TITLES: frozenset[str] = frozenset(
    {
        "popuphost",
        "context",
        "menu",
        "desktopwindowxamlsource",
    }
)


def verify_expected_window(
    expected_window: str | None,
    actual_title: str,
    *,
    required: bool = False,
    fallback_title: str | None = None,
) -> None:
    """Enforce substring target lock against active or point-under-cursor window title."""
    exp = (expected_window or "").strip()
    if required and not exp:
        raise WindowMismatchError("<non-empty expected_window required>", actual_title)
    if not exp or exp == "*":
        return
    exp_low = exp.lower()
    actual_clean = (actual_title or "").strip()
    if exp_low in actual_clean.lower():
        return
    if actual_clean.lower() in TRANSIENT_POPUP_TITLES:
        parent_title = (
            fallback_title if fallback_title is not None else get_foreground_window_title()
        )
        if exp_low in (parent_title or "").lower():
            return
    raise WindowMismatchError(exp, actual_title)


def get_foreground_window_info() -> tuple[int, tuple[int, int, int, int] | None]:
    hwnd = int(_user32.GetForegroundWindow())
    if not hwnd:
        return 0, None
    rect = wintypes.RECT()
    if not _user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
        return hwnd, None
    return hwnd, (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def list_windows(monitors: list[MonitorInfo], limit: int = WINDOW_ENUM_LIMIT) -> list[WindowInfo]:
    """Enumerate visible, non-cloaked top-level user windows."""
    windows: list[WindowInfo] = []
    primary_idx = next((m.index for m in monitors if m.is_primary), 1)
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd, _lparam) -> bool:
        if len(windows) >= limit:
            return False
        if not _user32.IsWindowVisible(hwnd):
            return True
        ex_style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if ex_style & WS_EX_TOOLWINDOW:
            return True
        length = _user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        if _is_cloaked(int(hwnd)):
            return True

        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title or title == "Program Manager":
            return True

        rect = wintypes.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        w = int(rect.right - rect.left)
        h = int(rect.bottom - rect.top)
        if w <= 0 or h <= 0:
            return True

        pid = wintypes.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid_val = int(pid.value)

        cx = int(rect.left + rect.right) // 2
        cy = int(rect.top + rect.bottom) // 2
        matched = find_monitor_for_point(monitors, cx, cy)
        mon_idx = matched.index if matched is not None else primary_idx

        windows.append(
            WindowInfo(
                hwnd=int(hwnd),
                title=title[:120],
                pid=pid_val,
                left=int(rect.left),
                top=int(rect.top),
                right=int(rect.right),
                bottom=int(rect.bottom),
                monitor_index=mon_idx,
                is_maximized=bool(_user32.IsZoomed(hwnd)),
                is_minimized=bool(_user32.IsIconic(hwnd)),
                integrity_rid=get_pid_integrity_rid(pid_val),
                is_elevated=is_pid_elevated_over_self(pid_val),
            )
        )
        return True

    cb = enum_proc(_callback)
    _user32.EnumWindows(cb, 0)
    return windows


def find_window(monitors: list[MonitorInfo], title_or_hwnd: int | str) -> WindowInfo:
    windows = list_windows(monitors, limit=WINDOW_ENUM_LIMIT)
    if isinstance(title_or_hwnd, int) or (
        isinstance(title_or_hwnd, str) and title_or_hwnd.strip().isdigit()
    ):
        target_hwnd = int(title_or_hwnd)
        for win in windows:
            if win.hwnd == target_hwnd:
                return win
        raise ValueError(f"WINDOW_NOT_FOUND: No visible window with hwnd={target_hwnd}")

    query = str(title_or_hwnd).strip().lower()
    if not query:
        raise ValueError("WINDOW_NOT_FOUND: Empty window title query")
    for win in windows:
        if win.title.lower() == query:
            return win
    for win in windows:
        if query in win.title.lower():
            return win
    raise ValueError(f"WINDOW_NOT_FOUND: No visible window matching '{title_or_hwnd}'")


def _focus_hwnd(hwnd: int) -> bool:
    if _user32.IsIconic(wintypes.HWND(hwnd)):
        _user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)

    if _user32.SetForegroundWindow(wintypes.HWND(hwnd)):
        return True

    # Handle Windows foreground lock via temporary AttachThreadInput
    fg_hwnd = int(_user32.GetForegroundWindow())
    fg_tid = _user32.GetWindowThreadProcessId(wintypes.HWND(fg_hwnd), None) if fg_hwnd else 0
    cur_tid = _kernel32.GetCurrentThreadId()
    attached = False
    try:
        if fg_tid and fg_tid != cur_tid:
            attached = bool(_user32.AttachThreadInput(cur_tid, fg_tid, True))
        _user32.BringWindowToTop(wintypes.HWND(hwnd))
        _user32.SetForegroundWindow(wintypes.HWND(hwnd))
    finally:
        if attached:
            _user32.AttachThreadInput(cur_tid, fg_tid, False)
    return int(_user32.GetForegroundWindow()) == hwnd


def _launch_safe_app(
    app_key: str,
    target_monitor: int | str | None,
    monitors: list[MonitorInfo],
) -> dict:
    exe_name = SAFE_LAUNCH_MAP.get(app_key.strip().lower())
    if not exe_name:
        raise GuardrailApprovalRequiredError(
            f"launch '{app_key}' not in safe allowlist ({sorted(SAFE_LAUNCH_MAP)})"
        )

    sys_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    candidates = [sys_root / "System32" / exe_name, sys_root / exe_name]
    exe_path = next((str(p) for p in candidates if p.is_file()), None)
    if exe_path is None:
        raise ValueError(f"LAUNCH_PATH_NOT_FOUND: '{exe_name}' missing under SystemRoot")

    existing_hwnds = {w.hwnd for w in list_windows(monitors, limit=WINDOW_ENUM_LIMIT)}
    proc = subprocess.Popen([exe_path])
    hints = _LAUNCH_TITLE_HINTS.get(exe_name, (exe_name.removesuffix(".exe"),))

    launched_win: WindowInfo | None = None
    reused_existing = False
    for _ in range(14):  # ~2.1s max poll
        abortable_sleep(0.15)
        current = list_windows(monitors, limit=WINDOW_ENUM_LIMIT)
        for win in current:
            if win.hwnd not in existing_hwnds and (
                win.pid == proc.pid or any(h in win.title.lower() for h in hints)
            ):
                launched_win = win
                break
        if launched_win is not None:
            break

    if launched_win is None:
        for win in list_windows(monitors, limit=WINDOW_ENUM_LIMIT):
            if win.pid == proc.pid or any(h in win.title.lower() for h in hints):
                launched_win = win
                reused_existing = win.hwnd in existing_hwnds and win.pid != proc.pid
                break

    if launched_win is None:
        raise ValueError(f"WINDOW_NOT_FOUND: Launched '{exe_name}' (pid={proc.pid}) without window")

    final_monitor = launched_win.monitor_index
    focused = _focus_hwnd(launched_win.hwnd)
    if target_monitor is not None:
        move_res = execute_window_action(
            action="move_to_monitor",
            title_or_hwnd=launched_win.hwnd,
            target_monitor=target_monitor,
            monitors=monitors,
        )
        final_monitor = int(move_res["target_monitor"])
        focused = bool(move_res["focused"])

    return {
        "action": "launch",
        "exe": exe_name,
        "exe_path": exe_path,
        "hwnd": launched_win.hwnd,
        "title": launched_win.title,
        "pid": launched_win.pid,
        "monitor": final_monitor,
        "focused": focused,
        "reused_existing": reused_existing,
    }


def execute_window_action(
    action: Literal["focus", "minimize", "maximize", "restore", "move_to_monitor", "launch"],
    title_or_hwnd: int | str,
    target_monitor: int | str | None,
    monitors: list[MonitorInfo],
) -> dict:
    """Focus, minimize, maximize, restore, move across monitors, or safely launch an app."""
    if action == "launch":
        return _launch_safe_app(str(title_or_hwnd), target_monitor, monitors)

    win = find_window(monitors, title_or_hwnd)
    hwnd = win.hwnd
    whandle = wintypes.HWND(hwnd)

    if action == "focus":
        focused = _focus_hwnd(hwnd)
        return {
            "action": action,
            "hwnd": hwnd,
            "title": win.title,
            "pid": win.pid,
            "monitor": win.monitor_index,
            "focused": focused,
        }

    if action == "minimize":
        _user32.ShowWindow(whandle, SW_MINIMIZE)
        return {
            "action": action,
            "hwnd": hwnd,
            "title": win.title,
            "pid": win.pid,
            "monitor": win.monitor_index,
            "is_minimized": bool(_user32.IsIconic(whandle)),
        }

    if action == "maximize":
        if _user32.IsIconic(whandle):
            _user32.ShowWindow(whandle, SW_RESTORE)
        _user32.ShowWindow(whandle, SW_MAXIMIZE)
        focused = _focus_hwnd(hwnd)
        return {
            "action": action,
            "hwnd": hwnd,
            "title": win.title,
            "pid": win.pid,
            "monitor": win.monitor_index,
            "focused": focused,
            "is_maximized": bool(_user32.IsZoomed(whandle)),
        }

    if action == "restore":
        _user32.ShowWindow(whandle, SW_RESTORE)
        focused = _focus_hwnd(hwnd)
        return {
            "action": action,
            "hwnd": hwnd,
            "title": win.title,
            "pid": win.pid,
            "monitor": win.monitor_index,
            "focused": focused,
            "is_maximized": bool(_user32.IsZoomed(whandle)),
            "is_minimized": bool(_user32.IsIconic(whandle)),
        }

    if action == "move_to_monitor":
        if target_monitor is None:
            raise ValueError(
                "INVALID_WINDOW_ACTION: target_monitor is required for move_to_monitor"
            )
        dst_mon = resolve_monitor(monitors, target_monitor)
        was_max = bool(_user32.IsZoomed(whandle))
        was_min = bool(_user32.IsIconic(whandle))
        if was_max or was_min:
            _user32.ShowWindow(whandle, SW_RESTORE)

        rect = wintypes.RECT()
        _user32.GetWindowRect(whandle, ctypes.byref(rect))
        cur_w = max(int(rect.right - rect.left), 400)
        cur_h = max(int(rect.bottom - rect.top), 300)
        dst_w = min(cur_w, max(dst_mon.width - 80, 320))
        dst_h = min(cur_h, max(dst_mon.height - 80, 240))
        dst_x = dst_mon.left + max((dst_mon.width - dst_w) // 2, 0)
        dst_y = dst_mon.top + max((dst_mon.height - dst_h) // 2, 0)

        _user32.SetWindowPos(
            whandle,
            0,
            dst_x,
            dst_y,
            dst_w,
            dst_h,
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
        if was_max:
            _user32.ShowWindow(whandle, SW_MAXIMIZE)
        focused = _focus_hwnd(hwnd)
        return {
            "action": action,
            "hwnd": hwnd,
            "title": win.title,
            "pid": win.pid,
            "monitor": dst_mon.index,
            "target_monitor": dst_mon.index,
            "restored_maximize": was_max,
            "focused": focused,
        }

    raise ValueError(f"INVALID_ACTION: Unsupported window action '{action}'")
