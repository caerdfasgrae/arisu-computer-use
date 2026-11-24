"""Display discovery, Per-Monitor DPI v2 awareness, and coordinate normalization."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "MonitorInfo",
    "VirtualScreenRect",
    "enable_per_monitor_dpi_v2",
    "enumerate_monitors",
    "get_virtual_screen_rect",
    "norm_to_physical",
    "physical_to_norm",
    "physical_to_virtualdesk",
    "resolve_monitor",
]

import ctypes
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shcore = ctypes.WinDLL("shcore", use_last_error=True)

_DPI_INITIALIZED: str | None = None

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
MDT_EFFECTIVE_DPI = 0
MONITOR_DEFAULTTONEAREST = 2


@dataclass(frozen=True)
class MonitorInfo:
    index: int
    device: str
    left: int
    top: int
    width: int
    height: int
    dpi_x: int
    dpi_y: int
    scale_pct: int
    is_primary: bool

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def contains(self, px_x: int, px_y: int) -> bool:
        return self.left <= px_x < self.right and self.top <= px_y < self.bottom

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "device": self.device,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
            "dpi_x": self.dpi_x,
            "dpi_y": self.dpi_y,
            "scale_pct": self.scale_pct,
            "is_primary": self.is_primary,
        }


@dataclass(frozen=True)
class VirtualScreenRect:
    left: int
    top: int
    width: int
    height: int


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


def enable_per_monitor_dpi_v2() -> str:
    """Enable Windows Per-Monitor DPI Awareness V2 once per process."""
    global _DPI_INITIALIZED
    if _DPI_INITIALIZED is not None:
        return _DPI_INITIALIZED

    try:
        _user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        _user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            _DPI_INITIALIZED = "PER_MONITOR_AWARE_V2"
            return _DPI_INITIALIZED
    except Exception:
        pass

    try:
        _shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        _shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        hr = _shcore.SetProcessDpiAwareness(2)
        if hr in (0, -2147024891):  # S_OK or E_ACCESSDENIED (already set)
            _DPI_INITIALIZED = f"PROCESS_PER_MONITOR_DPI_AWARE (hr={hr})"
            return _DPI_INITIALIZED
    except Exception as exc:
        _DPI_INITIALIZED = f"UNAVAILABLE ({exc})"
        return _DPI_INITIALIZED

    _DPI_INITIALIZED = "DEFAULT"
    return _DPI_INITIALIZED


def get_virtual_screen_rect() -> VirtualScreenRect:
    enable_per_monitor_dpi_v2()
    return VirtualScreenRect(
        left=int(_user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        top=int(_user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
        width=max(int(_user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)), 1),
        height=max(int(_user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)), 1),
    )


def enumerate_monitors() -> list[MonitorInfo]:
    """Return all physical monitors sorted deterministically left-to-right."""
    enable_per_monitor_dpi_v2()
    raw: list[dict] = []

    enum_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    def _callback(hmon, _hdc, _lprc, _lparam) -> bool:
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            dpi_x = wintypes.UINT(96)
            dpi_y = wintypes.UINT(96)
            _shcore.GetDpiForMonitor(
                hmon, MDT_EFFECTIVE_DPI, ctypes.byref(dpi_x), ctypes.byref(dpi_y)
            )
            r = info.rcMonitor
            raw.append(
                {
                    "device": info.szDevice,
                    "left": int(r.left),
                    "top": int(r.top),
                    "width": max(int(r.right - r.left), 1),
                    "height": max(int(r.bottom - r.top), 1),
                    "dpi_x": int(dpi_x.value),
                    "dpi_y": int(dpi_y.value),
                    "scale_pct": round(dpi_x.value / 96.0 * 100),
                    "is_primary": bool(info.dwFlags & 1),
                }
            )
        return True

    cb = enum_proc(_callback)
    _user32.EnumDisplayMonitors(None, None, cb, 0)
    raw.sort(key=lambda m: (m["left"], m["top"]))
    return [MonitorInfo(index=idx, **item) for idx, item in enumerate(raw, start=1)]


def find_monitor_for_point(monitors: list[MonitorInfo], px_x: int, px_y: int) -> MonitorInfo | None:
    for mon in monitors:
        if mon.contains(px_x, px_y):
            return mon
    return None


def resolve_monitor(
    monitors: list[MonitorInfo],
    monitor: int | str = "active",
    fg_rect: tuple[int, int, int, int] | None = None,
    cursor_pos: tuple[int, int] | None = None,
) -> MonitorInfo:
    """Resolve monitor reference ('active', 'primary', 'secondary', or 1-based index)."""
    if not monitors:
        raise ValueError("MONITOR_NOT_FOUND: No display monitors detected")

    if isinstance(monitor, int) or (isinstance(monitor, str) and monitor.strip().isdigit()):
        idx = int(monitor)
        for mon in monitors:
            if mon.index == idx:
                return mon
        raise ValueError(f"MONITOR_NOT_FOUND: Monitor {idx} out of range (1..{len(monitors)})")

    key = str(monitor).strip().lower()
    if key == "primary":
        return next((m for m in monitors if m.is_primary), monitors[0])
    if key == "secondary":
        return next((m for m in monitors if not m.is_primary), monitors[0])
    if key == "active":
        if fg_rect is not None:
            left, top, right, bottom = fg_rect
            cx = (left + right) // 2
            cy = (top + bottom) // 2
            matched = find_monitor_for_point(monitors, cx, cy)
            if matched is not None:
                return matched
        if cursor_pos is not None:
            matched = find_monitor_for_point(monitors, cursor_pos[0], cursor_pos[1])
            if matched is not None:
                return matched
        return next((m for m in monitors if m.is_primary), monitors[0])

    raise ValueError(f"MONITOR_NOT_FOUND: Unsupported monitor selector '{monitor}'")


def norm_to_physical(mon: MonitorInfo, norm_x: int, norm_y: int) -> tuple[int, int]:
    """Map monitor-relative [0..1000] coordinates into physical Win32 screen pixels."""
    if not (0 <= norm_x <= 1000 and 0 <= norm_y <= 1000):
        raise ValueError(
            f"INVALID_COORDS: Normalized coords ({norm_x}, {norm_y}) must be in [0, 1000]"
        )
    px_x = mon.left + round((norm_x / 1000.0) * max(mon.width - 1, 1))
    px_y = mon.top + round((norm_y / 1000.0) * max(mon.height - 1, 1))
    return px_x, px_y


def physical_to_norm(mon: MonitorInfo, px_x: int, px_y: int) -> tuple[int, int]:
    """Map physical screen pixel coordinates back into monitor-relative [0..1000]."""
    clamped_x = min(max(px_x, mon.left), mon.right - 1)
    clamped_y = min(max(px_y, mon.top), mon.bottom - 1)
    norm_x = round(((clamped_x - mon.left) / max(mon.width - 1, 1)) * 1000.0)
    norm_y = round(((clamped_y - mon.top) / max(mon.height - 1, 1)) * 1000.0)
    return min(max(norm_x, 0), 1000), min(max(norm_y, 0), 1000)


def physical_to_virtualdesk(px_x: int, px_y: int, v_rect: VirtualScreenRect) -> tuple[int, int]:
    """Map physical Win32 pixels to 0..65535 MOUSEEVENTF_VIRTUALDESK coordinates."""
    clamped_x = min(max(px_x, v_rect.left), v_rect.left + v_rect.width - 1)
    clamped_y = min(max(px_y, v_rect.top), v_rect.top + v_rect.height - 1)
    abs_x = round(((clamped_x - v_rect.left) * 65535) / max(v_rect.width - 1, 1))
    abs_y = round(((clamped_y - v_rect.top) * 65535) / max(v_rect.height - 1, 1))
    return min(max(abs_x, 0), 65535), min(max(abs_y, 0), 65535)
