"""Display capture and DPI utilities."""

from arisu_cua.display.monitors import (
    MonitorInfo,
    VirtualScreenRect,
    enable_per_monitor_dpi_v2,
    enumerate_monitors,
    get_virtual_screen_rect,
    norm_to_physical,
    physical_to_norm,
    physical_to_virtualdesk,
    resolve_monitor,
)

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
