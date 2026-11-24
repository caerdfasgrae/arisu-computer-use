"""Persistent killable Windows UI Automation (UIA) worker subprocess.

Communicates exclusively via single-line JSON frames over stdin/stdout so the
parent MCP server can enforce hard timeouts via ``proc.kill()`` without risking
COM/RPC deadlocks inside its own process.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from collections import deque

# Redirect stdout before importing COM/UIA packages so accidental prints go to stderr.
_PROTO_OUT = sys.stdout
sys.stdout = sys.stderr

import comtypes
import uiautomation as auto

from arisu_cua.display.monitors import enable_per_monitor_dpi_v2

INTERACTIVE_CONTROL_TYPES = frozenset(
    {
        "ButtonControl",
        "SplitButtonControl",
        "DocumentControl",
        "EditControl",
        "ComboBoxControl",
        "CheckBoxControl",
        "RadioButtonControl",
        "HyperlinkControl",
        "MenuItemControl",
        "TabItemControl",
        "ListItemControl",
        "TreeItemControl",
        "SliderControl",
    }
)

_user32 = ctypes.WinDLL("user32", use_last_error=True)


def _emit(payload: dict) -> None:
    _PROTO_OUT.write(json.dumps(payload, ensure_ascii=True) + "\n")
    _PROTO_OUT.flush()


def _is_password_control(ctrl) -> bool:
    try:
        val = getattr(ctrl, "IsPassword", False)
        if callable(val):
            val = val()
        if bool(val):
            return True
    except Exception:
        pass
    try:
        ctype = str(ctrl.ControlTypeName or "")
        if ctype == "EditControl":
            haystack = f"{ctrl.Name or ''} {ctrl.AutomationId or ''}".lower()
            if "password" in haystack:
                return True
    except Exception:
        pass
    return False


def _is_offscreen(ctrl) -> bool:
    try:
        val = getattr(ctrl, "IsOffscreen", False)
        if callable(val):
            val = val()
        return bool(val)
    except Exception:
        return False


def _is_enabled(ctrl) -> bool:
    try:
        val = getattr(ctrl, "IsEnabled", True)
        if callable(val):
            val = val()
        return bool(val)
    except Exception:
        return True


def _resolve_root_control(hwnd: int | None, window_title: str | None):
    if hwnd is not None and int(hwnd) > 0:
        return auto.ControlFromHandle(int(hwnd))

    if window_title:
        needle = str(window_title).strip().lower()
        if needle:
            try:
                root = auto.GetRootControl()
                if root is not None:
                    for child in root.GetChildren():
                        try:
                            cname = str(child.Name or "")
                            if needle in cname.lower():
                                return child
                        except Exception:
                            continue
            except Exception:
                pass

    fg_hwnd = int(_user32.GetForegroundWindow() or 0)
    if fg_hwnd <= 0:
        return None
    try:
        return auto.ControlFromHandle(fg_hwnd)
    except Exception:
        return auto.GetForegroundControl()


def _to_norm(px_x: int, px_y: int, monitor_rect: list[int] | None) -> tuple[int, int]:
    if not monitor_rect or len(monitor_rect) != 4:
        return 0, 0
    m_left, m_top, m_w, m_h = (int(v) for v in monitor_rect)
    m_w = max(m_w, 1)
    m_h = max(m_h, 1)
    clamped_x = min(max(px_x, m_left), m_left + m_w - 1)
    clamped_y = min(max(px_y, m_top), m_top + m_h - 1)
    nx = round(((clamped_x - m_left) / max(m_w - 1, 1)) * 1000.0)
    ny = round(((clamped_y - m_top) / max(m_h - 1, 1)) * 1000.0)
    return min(max(nx, 0), 1000), min(max(ny, 0), 1000)


def _walk_tree(
    root_ctrl,
    monitor_rect: list[int] | None,
    max_depth: int,
    max_items: int,
    interactive_only: bool,
) -> dict:
    if root_ctrl is None:
        return {
            "ok": True,
            "pid": os.getpid(),
            "root_name": "",
            "root_type": "None",
            "elements": [],
        }

    try:
        root_name = str(root_ctrl.Name or "")
    except Exception:
        root_name = ""
    try:
        root_type = str(root_ctrl.ControlTypeName or "")
    except Exception:
        root_type = ""

    depth_limit = min(max(int(max_depth), 1), 12)
    item_limit = min(max(int(max_items), 1), 150)

    mon_area = 0
    if monitor_rect and len(monitor_rect) == 4:
        m_left, m_top, m_w, m_h = (int(v) for v in monitor_rect)
        mon_area = max(m_w, 1) * max(m_h, 1)
    else:
        m_left, m_top, m_w, m_h = 0, 0, 0, 0

    elements: list[dict] = []
    seen_keys: set[tuple[str, str, int, int]] = set()
    queue: deque[tuple[object, int]] = deque([(root_ctrl, 0)])

    while queue and len(elements) < item_limit:
        ctrl, depth = queue.popleft()

        if depth > 0:
            try:
                ctype = str(ctrl.ControlTypeName or "")
                cname = str(ctrl.Name or "")[:120]
                rect = ctrl.BoundingRectangle
                if (
                    rect is not None
                    and rect.right > rect.left
                    and rect.bottom > rect.top
                    and not _is_offscreen(ctrl)
                ):
                    w = int(rect.right - rect.left)
                    h = int(rect.bottom - rect.top)
                    if w >= 6 and h >= 6:
                        cx = int((rect.left + rect.right) // 2)
                        cy = int((rect.top + rect.bottom) // 2)

                        inside_monitor = True
                        too_large = False
                        if mon_area > 0:
                            inside_monitor = (m_left <= cx < m_left + m_w) and (
                                m_top <= cy < m_top + m_h
                            )
                            too_large = (w * h) > (mon_area * 0.90)

                        keep = True
                        if interactive_only:
                            keep = (
                                inside_monitor
                                and not too_large
                                and (ctype in INTERACTIVE_CONTROL_TYPES)
                            )

                        if keep:
                            dedup_key = (ctype, cname, cx // 6, cy // 6)
                            if dedup_key not in seen_keys:
                                seen_keys.add(dedup_key)
                                nx1, ny1 = _to_norm(int(rect.left), int(rect.top), monitor_rect)
                                nx2, ny2 = _to_norm(
                                    int(rect.right) - 1, int(rect.bottom) - 1, monitor_rect
                                )
                                ncx, ncy = _to_norm(cx, cy, monitor_rect)
                                try:
                                    auto_id = str(ctrl.AutomationId or "")[:80]
                                except Exception:
                                    auto_id = ""
                                try:
                                    class_name = str(ctrl.ClassName or "")[:80]
                                except Exception:
                                    class_name = ""

                                elements.append(
                                    {
                                        "element_id": len(elements) + 1,
                                        "name": cname,
                                        "control_type": ctype,
                                        "automation_id": auto_id,
                                        "class_name": class_name,
                                        "depth": depth,
                                        "is_enabled": _is_enabled(ctrl),
                                        "is_password": _is_password_control(ctrl),
                                        "bbox_px": [
                                            int(rect.left),
                                            int(rect.top),
                                            int(rect.right),
                                            int(rect.bottom),
                                        ],
                                        "center_px": [cx, cy],
                                        "bbox_norm": [nx1, ny1, nx2, ny2],
                                        "center_norm": [ncx, ncy],
                                    }
                                )
            except Exception:
                pass

        if depth < depth_limit and len(elements) < item_limit:
            try:
                children = ctrl.GetChildren()
                if children:
                    for child in children:
                        queue.append((child, depth + 1))
            except Exception:
                pass

    return {
        "ok": True,
        "pid": os.getpid(),
        "root_name": root_name,
        "root_type": root_type,
        "elements": elements,
    }


def _get_fg_title(fg_hwnd: int) -> str:
    if fg_hwnd <= 0:
        return ""
    length = int(_user32.GetWindowTextLengthW(fg_hwnd))
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(fg_hwnd, buf, length + 1)
    return buf.value.strip()[:120]


def _check_focused_password() -> dict:
    fg_hwnd = int(_user32.GetForegroundWindow() or 0)
    fg_title = _get_fg_title(fg_hwnd)
    if fg_hwnd <= 0:
        return {
            "ok": True,
            "is_password": False,
            "is_editable": False,
            "has_focus": False,
            "window_title": "",
            "name": "",
            "control_type": "None",
        }
    try:
        focused = auto.GetFocusedControl()
        if focused is None:
            return {
                "ok": True,
                "is_password": False,
                "is_editable": False,
                "has_focus": False,
                "window_title": fg_title,
                "name": "",
                "control_type": "None",
            }
        ctype = str(focused.ControlTypeName or "")
        return {
            "ok": True,
            "is_password": _is_password_control(focused),
            "is_editable": ctype in ("EditControl", "ComboBoxControl", "DocumentControl"),
            "has_focus": True,
            "window_title": fg_title,
            "name": str(focused.Name or "")[:80],
            "control_type": ctype,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc)[:120],
            "has_focus": False,
            "window_title": fg_title,
        }


def main() -> int:
    try:
        _PROTO_OUT.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    enable_per_monitor_dpi_v2()
    comtypes.CoInitialize()
    auto.SetGlobalSearchTimeout(0.8)
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError as exc:
                _emit({"ok": False, "pid": os.getpid(), "error": f"INVALID_JSON: {exc}"})
                continue

            cmd = req.get("cmd")
            try:
                if cmd == "ping":
                    _emit({"ok": True, "pid": os.getpid()})
                elif cmd == "inspect_tree":
                    t0 = time.perf_counter()
                    root_ctrl = _resolve_root_control(
                        hwnd=req.get("hwnd"),
                        window_title=req.get("window_title"),
                    )
                    res = _walk_tree(
                        root_ctrl=root_ctrl,
                        monitor_rect=req.get("monitor_rect"),
                        max_depth=int(req.get("max_depth", 4)),
                        max_items=int(req.get("max_items", 50)),
                        interactive_only=bool(req.get("interactive_only", False)),
                    )
                    res["elapsed_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
                    _emit(res)
                elif cmd == "check_focused_password":
                    _emit(_check_focused_password())
                elif cmd == "simulate_hang":
                    time.sleep(3600)
                else:
                    _emit({"ok": False, "pid": os.getpid(), "error": f"UNKNOWN_CMD: {cmd}"})
            except Exception as exc:
                _emit({"ok": False, "pid": os.getpid(), "error": f"WORKER_ERROR: {exc}"})
    finally:
        comtypes.CoUninitialize()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
