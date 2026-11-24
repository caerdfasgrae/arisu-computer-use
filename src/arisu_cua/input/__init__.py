"""Win32 OS input and window management primitives."""

from arisu_cua.input.win32_input import (
    execute_keyboard_action,
    execute_mouse_action,
    execute_scroll_action,
    get_cursor_pos,
    resolve_vk,
)
from arisu_cua.input.window_mgr import (
    WindowInfo,
    execute_window_action,
    find_window,
    get_foreground_window_info,
    get_foreground_window_title,
    get_window_title_at_point,
    list_windows,
    verify_expected_window,
)

__all__ = [
    "WindowInfo",
    "execute_keyboard_action",
    "execute_mouse_action",
    "execute_scroll_action",
    "execute_window_action",
    "find_window",
    "get_cursor_pos",
    "get_foreground_window_info",
    "get_foreground_window_title",
    "get_window_title_at_point",
    "list_windows",
    "resolve_vk",
    "verify_expected_window",
]
