"""Arisu Computer Use — FastMCP Stdio Server (S1-S5 Complete Core, UIA, HUD & Guardrails)."""

from __future__ import annotations

import atexit
import base64
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

# Keep MCP stdio stdout strictly clean for JSON-RPC frames
_ORIGINAL_STDOUT = sys.stdout
sys.stdout = sys.stderr

from mcp.server.fastmcp import FastMCP
from mcp.types import ImageContent, TextContent, ToolAnnotations
from pydantic import Field

from arisu_cua.browser import get_cdp_client
from arisu_cua.config import (
    DEFAULT_IMAGE_FORMAT,
    DEFAULT_MAX_SIZE,
    DEFAULT_WAIT_SECONDS,
    FOCUS_POLL_S,
    FOCUS_WAIT_MAX_S,
    MAX_WAIT_SECONDS,
    WINDOW_ENUM_LIMIT,
    WINDOW_VERIFY_DEFAULT_S,
)
from arisu_cua.display.capture import (
    capture_monitor,
    clear_capture_cache,
    verify_pre_click_patch,
)
from arisu_cua.display.monitors import (
    enable_per_monitor_dpi_v2,
    enumerate_monitors,
    get_virtual_screen_rect,
    norm_to_physical,
    physical_to_norm,
    resolve_monitor,
)
from arisu_cua.hud.broker import get_hud_client, rect_contains_point
from arisu_cua.input.win32_input import (
    BLOCKED_HOTKEYS,
    _canonical_key,
    execute_keyboard_action,
    execute_mouse_action,
    execute_scroll_action,
    get_cursor_pos,
)
from arisu_cua.input.window_mgr import (
    execute_window_action,
    find_window,
    get_foreground_window_info,
    get_foreground_window_title,
    get_hwnd_at_point,
    get_pid_for_hwnd,
    get_pid_integrity_rid,
    get_self_integrity_rid,
    get_window_title_at_point,
    is_pid_elevated_over_self,
    list_windows,
    verify_expected_window,
)
from arisu_cua.perception.uia_client import ElementBox, get_uia_client
from arisu_cua.safety.audit import AuditLogger
from arisu_cua.safety.guardrails import (
    SmartCircuitBreaker,
    check_browser_target_denylist,
    check_window_denylist,
    compute_action_hash,
    is_sensitive_button_name,
)
from arisu_cua.safety.killswitch import (
    ABORT_EVENT,
    PAUSED_EVENT,
    ArisuError,
    BrowserConnectionError,
    BrowserElementNotFoundError,
    CornerFailsafeMonitor,
    GuardrailApprovalRequiredError,
    HandoffCancelledError,
    HandoffTimeoutError,
    InputIntegrityBlockedError,
    InvalidApprovalTokenError,
    InvalidParamComboError,
    PasswordCheckUnavailableError,
    abortable_sleep,
    check_ready,
    reset_abort,
    set_paused,
    trigger_abort,
)

sys.stdout = _ORIGINAL_STDOUT


@dataclass
class SessionState:
    session_id: str = field(default_factory=lambda: f"sess_{uuid.uuid4().hex[:8]}")
    active: bool = False
    task_label: str = ""
    mutation_count: int = 0


_SESSION = SessionState()
_BREAKER = SmartCircuitBreaker()
_AUDIT = AuditLogger()
_CORNER_FAILSAFE = CornerFailsafeMonitor()
atexit.register(_CORNER_FAILSAFE.stop)
mcp = FastMCP("arisu-computer-use")


def _err_json(code: str, message: str) -> str:
    return json.dumps({"ok": False, "code": code, "error": message})


def _check_monitor_denylist(target_mon, monitors) -> None:
    titles = [
        w.title
        for w in list_windows(monitors, limit=25)
        if w.monitor_index == target_mon.index and not w.is_minimized
    ]
    check_window_denylist(titles)


def _enforce_out_of_band_approval(
    tool: str,
    target_window: str,
    action_desc: str,
    payload: dict[str, Any],
    approval_token: str | None,
) -> None:
    action_hash = compute_action_hash(tool, target_window, payload)
    hud = get_hud_client()
    res = hud.consume_approval(action_hash=action_hash, approval_token=approval_token)
    if res.get("code") == "INVALID_APPROVAL_TOKEN":
        raise InvalidApprovalTokenError(str(res.get("error") or "Invalid approval token"))
    if res.get("ok") and res.get("approved"):
        return
    if approval_token:
        raise InvalidApprovalTokenError(
            "HUD approval bridge offline or token invalid; approve physically on HUD"
        )
    hud.request_approval(
        action_hash=action_hash,
        action_desc=action_desc,
        target_window=target_window,
    )
    hud.send_event(
        "state",
        state="shy",
        text=f"Butuh izinmu: {action_desc[:32]}?",
        mood="shy",
    )
    raise GuardrailApprovalRequiredError(action_desc)


def _notify_hud_error(err_code: str) -> None:
    if err_code in (
        "GUARDRAIL_APPROVAL_REQUIRED",
        "USER_MOUSE_CONTENTION_YIELD",
        "INPUT_INTEGRITY_BLOCKED",
    ):
        return
    get_hud_client().send_event(
        "state",
        state="error",
        text=f"Ditahan: {err_code}",
        mood="surprised",
    )


def _check_uipi_for_hwnd(hwnd: int, target_title: str) -> None:
    """Reject GUI input when the target HWND runs at higher Windows integrity RID."""
    if not hwnd:
        return
    pid = get_pid_for_hwnd(hwnd)
    if pid <= 0:
        return
    if is_pid_elevated_over_self(pid) is True:
        target_rid = get_pid_integrity_rid(pid) or 0x3000
        self_rid = get_self_integrity_rid() or 0x2000
        raise InputIntegrityBlockedError(target_title, target_rid, self_rid)


def _check_target_uipi_at_point(px_x: int, px_y: int, target_title: str) -> None:
    if (
        getattr(get_window_title_at_point, "__module__", "") != "arisu_cua.input.window_mgr"
        and getattr(get_hwnd_at_point, "__module__", "") == "arisu_cua.input.window_mgr"
    ):
        return
    hwnd = get_hwnd_at_point(px_x, px_y)
    if hwnd:
        _check_uipi_for_hwnd(hwnd, target_title)


def _enforce_password_guard(
    is_pw: bool | None,
    *,
    tool: str,
    target_window: str,
    payload: dict[str, Any],
    approval_token: str | None,
) -> bool:
    """Fail closed on unverifiable UIA password status or require HUD approval."""
    if is_pw is None:
        raise PasswordCheckUnavailableError()
    if is_pw is True:
        _enforce_out_of_band_approval(
            tool=tool,
            target_window=target_window,
            action_desc="Ketik ke kolom password",
            payload=payload,
            approval_token=approval_token,
        )
        return True
    return False


def _resolve_hwnd_on_monitor(
    target_mon,
    monitors,
    fg_hwnd: int,
    fg_rect: tuple[int, int, int, int] | None,
) -> int | None:
    if fg_hwnd > 0 and fg_rect is not None:
        cx = (fg_rect[0] + fg_rect[2]) // 2
        cy = (fg_rect[1] + fg_rect[3]) // 2
        if target_mon.contains(cx, cy):
            return fg_hwnd
    for w in list_windows(monitors, limit=WINDOW_ENUM_LIMIT):
        if w.monitor_index == target_mon.index and not w.is_minimized:
            return w.hwnd
    return None


def _capture_payload(
    monitor: int | str = "active",
    mode: Literal["clean", "grid", "som"] = "clean",
    fmt: Literal["webp", "jpeg"] = DEFAULT_IMAGE_FORMAT,
    max_size: int = DEFAULT_MAX_SIZE,
    region: list[int] | None = None,
) -> list[TextContent | ImageContent]:
    t0 = time.perf_counter()
    monitors = enumerate_monitors()
    fg_hwnd, fg_rect = get_foreground_window_info()
    cursor_px = get_cursor_pos()
    target_mon = resolve_monitor(monitors, monitor, fg_rect=fg_rect, cursor_pos=cursor_px)
    _check_monitor_denylist(target_mon, monitors)

    effective_mode: Literal["clean", "grid", "som"] = mode
    som_elements: list[ElementBox] = []
    uia_warning: str | None = None

    if mode == "som":
        get_hud_client().send_event(
            "state",
            state="searching",
            text=f"Memindai UI Monitor {target_mon.index}…",
            mood="focused",
        )
        uia_client = get_uia_client()
        target_hwnd = _resolve_hwnd_on_monitor(target_mon, monitors, fg_hwnd, fg_rect)
        try:
            _, som_elements = uia_client.inspect_tree(
                target_mon,
                hwnd=target_hwnd,
                max_depth=7,
                max_items=40,
                interactive_only=True,
            )
            uia_client.update_som_registry(som_elements)
        except (ArisuError, RuntimeError) as exc:
            uia_client.clear_som_registry()
            som_elements = []
            uia_warning = getattr(exc, "code", None) or "UIA_WORKER_CRASHED"
            effective_mode = "clean"
    else:
        get_hud_client().send_event(
            "state",
            state="searching",
            text=f"Potret layar Monitor {target_mon.index}~",
            mood="wink",
        )

    shot = capture_monitor(
        target_mon,
        fmt=fmt,
        max_size=max_size,
        mode=effective_mode,
        region=region,
        som_elements=som_elements if effective_mode == "som" else None,
    )
    meta = shot.to_metadata()
    meta["ok"] = True
    meta["mode"] = effective_mode
    meta["mode_requested"] = mode
    meta["mode_effective"] = effective_mode
    meta["monitor_bounds"] = target_mon.to_dict()
    fg_title = get_foreground_window_title()
    meta["foreground_window_title"] = fg_title
    if mode == "som":
        meta["som_count"] = len(som_elements)
        meta["som_elements"] = [el.to_dict() for el in som_elements]
        if uia_warning is not None:
            meta["som_unavailable_reason"] = uia_warning
        elif not som_elements:
            meta["som_unavailable_reason"] = "NO_ELEMENTS"
    if uia_warning is not None:
        meta["uia_warning"] = uia_warning
    meta["vision_notice"] = (
        "CRITICAL: If Antigravity offloads this image to file:///.../media_0.webp, "
        "you MUST call read_file on that .webp path before taking any action."
    )
    _AUDIT.log(
        session_id=_SESSION.session_id,
        tool="arisu_cu_capture_screen",
        status="ok",
        monitor=target_mon.index,
        target_window=fg_title,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
        args={"mode": effective_mode, "fmt": fmt},
    )
    b64 = base64.b64encode(shot.data).decode("ascii")
    return [
        TextContent(type="text", text=json.dumps(meta)),
        ImageContent(type="image", data=b64, mimeType=shot.mime_type),
    ]


def _err_contents(code: str, message: str) -> list[TextContent | ImageContent]:
    return [TextContent(type="text", text=_err_json(code, message))]


def _append_inline_verification(
    action_res: dict[str, Any],
    monitor: int | str,
    wait_after_s: float,
    verify_mode: Literal["none", "clean", "grid", "som"],
    default_wait_s: float = 0.6,
) -> list[TextContent | ImageContent]:
    """Wait and capture an inline verification screenshot while preserving execution status."""
    action_res["executed"] = True
    if verify_mode == "none":
        return [TextContent(type="text", text=json.dumps(action_res))]

    wait_s = (
        default_wait_s
        if wait_after_s <= 0.0
        else min(max(float(wait_after_s), 0.1), MAX_WAIT_SECONDS)
    )
    try:
        abortable_sleep(wait_s)
        capture_items = _capture_payload(monitor=monitor, mode=verify_mode, fmt="webp")
        first = capture_items[0]
        cap_meta = json.loads(first.text) if isinstance(first, TextContent) else {}
        action_res["inline_verify"] = cap_meta
        action_res["foreground_window_title"] = cap_meta.get(
            "foreground_window_title", action_res.get("foreground_window_title", "")
        )
        action_res["vision_notice"] = cap_meta.get("vision_notice", "")
        return [
            TextContent(type="text", text=json.dumps(action_res)),
            *capture_items[1:],
        ]
    except ArisuError as exc:
        action_res["inline_verify"] = {"ok": False, "code": exc.code, "error": exc.message}
        action_res.setdefault("warnings", []).append("INLINE_VERIFY_FAILED")
        return [TextContent(type="text", text=json.dumps(action_res))]
    except Exception as exc:
        action_res["inline_verify"] = {
            "ok": False,
            "code": "VERIFY_CAPTURE_ERROR",
            "error": str(exc),
        }
        action_res.setdefault("warnings", []).append("INLINE_VERIFY_FAILED")
        return [TextContent(type="text", text=json.dumps(action_res))]


@mcp.tool(
    name="arisu_cu_get_system_context",
    description=(
        "Inspect connected monitors (bounds, DPI scale), visible top-level windows "
        "with monitor assignment, normalized cursor position, and session state."
    ),
    structured_output=False,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def arisu_cu_get_system_context() -> str:
    t0 = time.perf_counter()
    dpi_mode = enable_per_monitor_dpi_v2()
    monitors = enumerate_monitors()
    vrect = get_virtual_screen_rect()
    fg_hwnd, fg_rect = get_foreground_window_info()
    cursor_px = get_cursor_pos()
    active_mon = resolve_monitor(monitors, "active", fg_rect=fg_rect, cursor_pos=cursor_px)
    cursor_norm = physical_to_norm(active_mon, cursor_px[0], cursor_px[1])
    raw_windows = list_windows(monitors, limit=WINDOW_ENUM_LIMIT + 1)
    windows_truncated = len(raw_windows) > WINDOW_ENUM_LIMIT
    windows = [w.to_dict() for w in raw_windows[:WINDOW_ENUM_LIMIT]]
    get_hud_client().send_event(
        "state", state="searching", text="Mengecek layar & jendela…", mood="smile"
    )
    _AUDIT.log(
        session_id=_SESSION.session_id,
        tool="arisu_cu_get_system_context",
        status="ok",
        monitor=active_mon.index,
        target_window=get_foreground_window_title(),
        latency_ms=(time.perf_counter() - t0) * 1000.0,
    )

    return json.dumps(
        {
            "ok": True,
            "dpi_mode": dpi_mode,
            "self_integrity_rid": get_self_integrity_rid(),
            "virtual_screen": {
                "left": vrect.left,
                "top": vrect.top,
                "width": vrect.width,
                "height": vrect.height,
            },
            "monitors": [m.to_dict() for m in monitors],
            "active_monitor": active_mon.index,
            "foreground_hwnd": fg_hwnd,
            "foreground_window_title": get_foreground_window_title(),
            "cursor_px": list(cursor_px),
            "cursor_norm": list(cursor_norm),
            "windows": windows,
            "windows_truncated": windows_truncated,
            "session": {
                "session_id": _SESSION.session_id,
                "active": _SESSION.active,
                "task_label": _SESSION.task_label,
                "paused": PAUSED_EVENT.is_set(),
                "aborted": ABORT_EVENT.is_set(),
                "mutation_count": _SESSION.mutation_count,
            },
            "hud": get_hud_client().get_hud_status(),
        }
    )


@mcp.tool(
    name="arisu_cu_capture_screen",
    description=(
        "Capture a physical monitor as WebP/JPEG with optional 0..1000 grid, "
        "Set-of-Mark [1..N] overlay, or zoom region."
    ),
    structured_output=False,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def arisu_cu_capture_screen(
    monitor: Annotated[
        str, Field(description="Monitor: 'active', 'primary', 'secondary', '1'/'2'")
    ] = "active",
    mode: Annotated[
        Literal["clean", "grid", "som"],
        Field(description="Overlay mode: 'clean', 'grid', or 'som'"),
    ] = "clean",
    fmt: Annotated[Literal["webp", "jpeg"], Field(description="Format: 'webp' or 'jpeg'")] = "webp",
    max_size: Annotated[int, Field(description="Max edge pixels (128..3840)")] = DEFAULT_MAX_SIZE,
    region: Annotated[
        list[int] | None,
        Field(description="Zoom box [x1, y1, x2, y2] in 0..1000"),
    ] = None,
) -> list[TextContent | ImageContent]:
    try:
        return _capture_payload(
            monitor=monitor,
            mode=mode,
            fmt=fmt,
            max_size=max_size,
            region=region,
        )
    except ArisuError as exc:
        _notify_hud_error(exc.code)
        _AUDIT.log(
            session_id=_SESSION.session_id,
            tool="arisu_cu_capture_screen",
            status="blocked",
            monitor=monitor,
            code=exc.code,
        )
        return [TextContent(type="text", text=_err_json(exc.code, exc.message))]
    except Exception as exc:
        _notify_hud_error("CAPTURE_ERROR")
        return [TextContent(type="text", text=_err_json("CAPTURE_ERROR", str(exc)))]


@mcp.tool(
    name="arisu_cu_inspect_ui_tree",
    description=(
        "Inspect the Windows UI Automation (UIA) accessibility hierarchy in a killable "
        "subprocess (2.0 s timeout) returning structured bounds, control types, and [1..N] IDs."
    ),
    structured_output=False,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def arisu_cu_inspect_ui_tree(
    window_title: Annotated[
        str | None, Field(description="Window title filter (default foreground)")
    ] = None,
    monitor: Annotated[
        str, Field(description="Monitor: 'active', 'primary', 'secondary', '1'/'2'")
    ] = "active",
    max_depth: Annotated[int, Field(description="Max UIA tree depth (1..12)")] = 7,
    max_items: Annotated[int, Field(description="Max UI elements returned (1..100)")] = 50,
    interactive_only: Annotated[bool, Field(description="Return only interactive controls")] = True,
) -> str:
    t0 = time.perf_counter()
    try:
        if window_title:
            check_window_denylist(window_title)
        monitors = enumerate_monitors()
        fg_hwnd, fg_rect = get_foreground_window_info()
        cursor_px = get_cursor_pos()
        target_mon = resolve_monitor(monitors, monitor, fg_rect=fg_rect, cursor_pos=cursor_px)
        if not window_title:
            _check_monitor_denylist(target_mon, monitors)
        target_hwnd = (
            None
            if window_title
            else _resolve_hwnd_on_monitor(target_mon, monitors, fg_hwnd, fg_rect)
        )
        get_hud_client().send_event(
            "state",
            state="searching",
            text=f"Memeriksa UI tree Monitor {target_mon.index}…",
            mood="focused",
        )
        uia_client = get_uia_client()
        root_name, elements = uia_client.inspect_tree(
            target_mon,
            window_title=window_title,
            hwnd=target_hwnd,
            max_depth=max_depth,
            max_items=min(max(int(max_items), 1), 100),
            interactive_only=interactive_only,
        )
        check_window_denylist(root_name)
        if interactive_only:
            uia_client.update_som_registry(elements)
        _AUDIT.log(
            session_id=_SESSION.session_id,
            tool="arisu_cu_inspect_ui_tree",
            status="ok",
            monitor=target_mon.index,
            target_window=root_name,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            args={"interactive_only": interactive_only, "count": len(elements)},
        )
        return json.dumps(
            {
                "ok": True,
                "root_window_title": root_name,
                "monitor": target_mon.index,
                "interactive_only": interactive_only,
                "count": len(elements),
                "elements": [el.to_dict() for el in elements],
            }
        )
    except ArisuError as exc:
        _notify_hud_error(exc.code)
        _AUDIT.log(
            session_id=_SESSION.session_id,
            tool="arisu_cu_inspect_ui_tree",
            status="blocked",
            monitor=monitor,
            code=exc.code,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )
        return _err_json(exc.code, exc.message)
    except Exception as exc:
        _notify_hud_error("UIA_ERROR")
        return _err_json("UIA_ERROR", str(exc))


@mcp.tool(
    name="arisu_cu_mouse_action",
    description=(
        "Execute mouse click, double_click, right_click, move, or drag using "
        "Set-of-Mark element_id or monitor-relative 0..1000 coordinates."
    ),
    structured_output=False,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def arisu_cu_mouse_action(
    action: Annotated[
        Literal["click", "double_click", "right_click", "move", "drag"],
        Field(description="Mouse operation type"),
    ],
    x: Annotated[int, Field(description="Horizontal coordinate (0..1000)")] = 0,
    y: Annotated[int, Field(description="Vertical coordinate (0..1000)")] = 0,
    monitor: Annotated[
        str, Field(description="Monitor: 'active', 'primary', 'secondary', '1'/'2'")
    ] = "active",
    end_x: Annotated[int | None, Field(description="Drag target X (0..1000)")] = None,
    end_y: Annotated[int | None, Field(description="Drag target Y (0..1000)")] = None,
    expected_window: Annotated[
        str | None, Field(description="Expected target window substring or '*'")
    ] = None,
    element_id: Annotated[int | None, Field(description="Set-of-Mark ID [1..N]")] = None,
    type_text: Annotated[str | None, Field(description="Text to type after click")] = None,
    press_enter: Annotated[bool, Field(description="Press Enter 120ms after click/type")] = False,
    approval_token: Annotated[
        str | None, Field(description="Single-use HUD approval token")
    ] = None,
    wait_after_s: Annotated[
        float, Field(description="Post-action wait before capture (0..5s)")
    ] = 0.0,
    verify_mode: Annotated[
        Literal["none", "clean", "grid", "som"],
        Field(description="Inline capture mode (default 'grid')"),
    ] = "grid",
) -> list[TextContent | ImageContent]:
    t0 = time.perf_counter()
    redact = False
    try:
        _CORNER_FAILSAFE.start()
        if (type_text is not None or press_enter) and action not in ("click", "double_click"):
            raise InvalidParamComboError(
                "type_text and press_enter are only supported with action='click' or 'double_click'"
            )

        monitors = enumerate_monitors()
        _, fg_rect = get_foreground_window_info()
        cursor_px = get_cursor_pos()

        matched_el: ElementBox | None = None
        if element_id is not None:
            matched_el = get_uia_client().get_som_element(element_id)
            mon = resolve_monitor(
                monitors,
                str(matched_el.monitor_index),
                fg_rect=fg_rect,
                cursor_pos=cursor_px,
            )
            px_x, px_y = matched_el.center_px
            x, y = matched_el.center_norm
            target_title = (
                get_window_title_at_point(px_x, px_y)
                or matched_el.window_title
                or get_foreground_window_title()
            )
        else:
            mon = resolve_monitor(monitors, monitor, fg_rect=fg_rect, cursor_pos=cursor_px)
            px_x, px_y = norm_to_physical(mon, x, y)
            target_title = get_window_title_at_point(px_x, px_y) or get_foreground_window_title()

        fg_title_now = get_foreground_window_title()
        verify_expected_window(
            expected_window,
            target_title,
            required=False,
            fallback_title=fg_title_now,
        )
        check_window_denylist([target_title, fg_title_now])
        _check_target_uipi_at_point(px_x, px_y, target_title)
        patch_check: dict[str, Any] = {"checked": False}
        if action in ("click", "double_click", "right_click"):
            patch_check = verify_pre_click_patch(px_x, px_y, mon=mon)

        end_px_x, end_px_y = (
            norm_to_physical(mon, end_x, end_y)
            if (end_x is not None and end_y is not None)
            else (None, None)
        )
        if matched_el is not None and is_sensitive_button_name(matched_el.name):
            _enforce_out_of_band_approval(
                tool="arisu_cu_mouse_action",
                target_window=target_title,
                action_desc=f"Click '{matched_el.name}'",
                payload={"action": action, "px": [px_x, px_y], "name": matched_el.name},
                approval_token=approval_token,
            )

        _BREAKER.check_and_record(
            "arisu_cu_mouse_action",
            target_title,
            {
                "action": action,
                "px": [px_x, px_y],
                "end_px": [end_px_x, end_px_y],
                "type_text": type_text,
                "press_enter": press_enter,
            },
        )

        hud = get_hud_client()
        mood = "wink" if action in ("click", "double_click", "right_click") else "smile"
        bubble = (
            f"Klik di Monitor {mon.index}~"
            if mood == "wink"
            else f"Geser kursor di Monitor {mon.index}…"
        )
        hud.send_event("state", state="working", text=bubble, mood=mood)
        hud.send_cursor_cue(action=action, target_px=(px_x, px_y), duration_ms=200)
        hud_status = hud.get_hud_status()
        hud_rect = hud_status.get("hud_rect")
        dodged = False
        if (
            hud_status.get("running")
            and hud_rect
            and (
                rect_contains_point(hud_rect, px_x, px_y)
                or (
                    end_px_x is not None
                    and end_px_y is not None
                    and rect_contains_point(hud_rect, end_px_x, end_px_y)
                )
            )
        ):
            hud.send_event("set_click_through", enabled=True)
            dodged = True
        try:
            res = execute_mouse_action(
                action=action,
                px_x=px_x,
                px_y=px_y,
                end_px_x=end_px_x,
                end_px_y=end_px_y,
                on_contention=lambda: hud.send_event(
                    "state",
                    state="shy",
                    text="Kamu gerakin mouse dulu ya~",
                    mood="surprised",
                ),
            )
        finally:
            clear_capture_cache()
            if dodged:
                hud.send_event("set_click_through", enabled=False)

        if (type_text is not None or press_enter) and action in ("click", "double_click"):
            uia_client = get_uia_client()
            try:
                focus_info: dict[str, Any] | None = None
                if hasattr(uia_client, "await_focused_control"):
                    focus_info = uia_client.await_focused_control(
                        timeout_s=FOCUS_WAIT_MAX_S,
                        poll_s=FOCUS_POLL_S,
                    )
                    is_pw: bool | None = (
                        None if focus_info is None else bool(focus_info.get("is_password", False))
                    )
                else:
                    abortable_sleep(0.15)
                    is_pw = uia_client.check_focused_password()

                if type_text:
                    if focus_info and focus_info.get("window_title"):
                        check_window_denylist(str(focus_info["window_title"]))
                    check_window_denylist(get_foreground_window_title())
                    if is_pw is None or is_pw is True:
                        redact = True
                    _enforce_password_guard(
                        is_pw,
                        tool="arisu_cu_mouse_action",
                        target_window=target_title,
                        payload={
                            "action": action,
                            "px": [px_x, px_y],
                            "password_type": True,
                        },
                        approval_token=approval_token,
                    )
                    hud.send_event(
                        "state",
                        state="typing",
                        text=f"Mengetik ({len(type_text)} kar)~",
                        mood="focused",
                    )
                    kb_extra: dict[str, Any] = {"allow_clipboard": False} if redact else {}
                    type_res = execute_keyboard_action(
                        action="type", text=type_text, typing_mode="natural", **kb_extra
                    )
                    res["typed_chars"] = type_res.get("chars", len(type_text))
                    if "delivery" in type_res:
                        res["delivery"] = type_res["delivery"]
                if press_enter:
                    abortable_sleep(0.12)
                    execute_keyboard_action(action="press", keys=["enter"])
                    res["pressed_enter"] = True
            finally:
                clear_capture_cache()

        _SESSION.mutation_count += 1
        res.update(
            {
                "ok": True,
                "monitor": mon.index,
                "norm": [x, y],
                "target_window_title": target_title,
                "foreground_window_title": get_foreground_window_title(),
            }
        )
        if patch_check.get("checked"):
            res["patch_mean_diff"] = patch_check["mean_diff"]
        if matched_el is not None:
            res["element_id"] = matched_el.element_id
            res["element_name"] = matched_el.name
            res["element_type"] = matched_el.control_type
        _AUDIT.log(
            session_id=_SESSION.session_id,
            tool="arisu_cu_mouse_action",
            status="ok",
            monitor=mon.index,
            target_window=target_title,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            args={
                "action": action,
                "norm": [x, y],
                "type_text": "[REDACTED]" if redact and type_text is not None else type_text,
                "press_enter": press_enter,
                "approval_token": approval_token,
            },
            redact_text=redact,
        )
        default_wait_s = (
            1.5 if (press_enter or (type_text is not None and "\n" in type_text)) else 0.6
        )
        return _append_inline_verification(
            res,
            monitor=str(mon.index),
            wait_after_s=wait_after_s,
            verify_mode=verify_mode,
            default_wait_s=default_wait_s,
        )
    except ArisuError as exc:
        _notify_hud_error(exc.code)
        _AUDIT.log(
            session_id=_SESSION.session_id,
            tool="arisu_cu_mouse_action",
            status="blocked",
            monitor=monitor,
            code=exc.code,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            args={
                "action": action,
                "type_text": "[REDACTED]" if redact and type_text is not None else type_text,
                "press_enter": press_enter,
                "approval_token": approval_token,
            },
            redact_text=redact,
        )
        return _err_contents(exc.code, exc.message)
    except Exception as exc:
        _notify_hud_error("MOUSE_ERROR")
        return _err_contents("MOUSE_ERROR", str(exc))


@mcp.tool(
    name="arisu_cu_keyboard_action",
    description=(
        "Send Unicode text typing, single key press, or modifier hotkey combination "
        "to the active Windows focus (requires expected_window lock)."
    ),
    structured_output=False,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def arisu_cu_keyboard_action(
    action: Annotated[
        Literal["type", "press", "hotkey"], Field(description="Keyboard operation type")
    ],
    expected_window: Annotated[
        str, Field(description="Required foreground window title substring")
    ],
    text: Annotated[str | None, Field(description="Text to type when action='type'")] = None,
    keys: Annotated[list[str] | None, Field(description="Key names for press/hotkey")] = None,
    approval_token: Annotated[
        str | None, Field(description="Single-use HUD approval token")
    ] = None,
    typing_mode: Annotated[
        Literal["natural", "fast", "paste"],
        Field(description="Typing mode: 'natural', 'fast', or 'paste' (auto >=120 chars)"),
    ] = "natural",
    press_enter: Annotated[bool, Field(description="Press Enter 120ms after typing")] = False,
    wait_after_s: Annotated[
        float, Field(description="Post-action wait before capture (0..5s)")
    ] = 0.0,
    verify_mode: Annotated[
        Literal["none", "clean", "grid", "som"],
        Field(description="Inline capture mode (default 'grid')"),
    ] = "grid",
) -> list[TextContent | ImageContent]:
    t0 = time.perf_counter()
    redact = False
    try:
        _CORNER_FAILSAFE.start()
        fg_title = get_foreground_window_title()
        verify_expected_window(expected_window, fg_title, required=True)
        check_window_denylist(fg_title)
        if (
            getattr(get_foreground_window_info, "__module__", "") == "arisu_cua.input.window_mgr"
            or getattr(get_pid_for_hwnd, "__module__", "") != "arisu_cua.input.window_mgr"
        ):
            fg_hwnd, _ = get_foreground_window_info()
            if fg_hwnd:
                _check_uipi_for_hwnd(fg_hwnd, fg_title)

        bypass_hotkey_block = False
        if action == "type":
            is_pw = get_uia_client().check_focused_password()
            if is_pw is None or is_pw is True:
                redact = True
            _enforce_password_guard(
                is_pw,
                tool="arisu_cu_keyboard_action",
                target_window=fg_title,
                payload={"action": action, "expected_window": expected_window},
                approval_token=approval_token,
            )
        elif (
            action == "hotkey"
            and keys
            and frozenset(_canonical_key(k) for k in keys) in BLOCKED_HOTKEYS
        ):
            _enforce_out_of_band_approval(
                tool="arisu_cu_keyboard_action",
                target_window=fg_title,
                action_desc=f"Hotkey {'+'.join(keys)}",
                payload={"action": action, "keys": list(keys)},
                approval_token=approval_token,
            )
            bypass_hotkey_block = True

        _BREAKER.check_and_record(
            "arisu_cu_keyboard_action",
            fg_title,
            {"action": action, "text": text, "keys": keys, "press_enter": press_enter},
        )

        mood = "smile" if action == "type" else "wink"
        bubble = (
            f"Mengetik di {expected_window[:20]}…"
            if action == "type"
            else f"Pintasan keyboard di {expected_window[:20]}~"
        )
        get_hud_client().send_event("state", state="working", text=bubble, mood=mood)
        try:
            kb_extra: dict[str, Any] = {"allow_clipboard": False} if redact else {}
            res = execute_keyboard_action(
                action=action,
                text=text,
                keys=keys,
                bypass_hotkey_block=bypass_hotkey_block,
                typing_mode=typing_mode,
                **kb_extra,
            )
            if action == "type" and press_enter:
                abortable_sleep(0.12)
                execute_keyboard_action(action="press", keys=["enter"])
                res["pressed_enter"] = True
        finally:
            clear_capture_cache()
        _SESSION.mutation_count += 1
        res["ok"] = True
        res["foreground_window_title"] = fg_title
        _AUDIT.log(
            session_id=_SESSION.session_id,
            tool="arisu_cu_keyboard_action",
            status="ok",
            target_window=fg_title,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            args={
                "action": action,
                "keys": keys,
                "text": text,
                "press_enter": press_enter,
                "approval_token": approval_token,
            },
            redact_text=redact,
        )
        default_wait_s = (
            1.5
            if (
                press_enter
                or (text is not None and "\n" in text)
                or (keys is not None and "enter" in [k.lower() for k in keys])
            )
            else 0.6
        )
        return _append_inline_verification(
            res,
            monitor="active",
            wait_after_s=wait_after_s,
            verify_mode=verify_mode,
            default_wait_s=default_wait_s,
        )
    except ArisuError as exc:
        _notify_hud_error(exc.code)
        _AUDIT.log(
            session_id=_SESSION.session_id,
            tool="arisu_cu_keyboard_action",
            status="blocked",
            target_window=expected_window,
            code=exc.code,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            args={"action": action, "keys": keys, "text": text, "approval_token": approval_token},
            redact_text=redact,
        )
        return _err_contents(exc.code, exc.message)
    except Exception as exc:
        _notify_hud_error("KEYBOARD_ERROR")
        return _err_contents("KEYBOARD_ERROR", str(exc))


@mcp.tool(
    name="arisu_cu_scroll_action",
    description="Scroll vertically or horizontally at monitor-relative 0..1000 coordinates.",
    structured_output=False,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def arisu_cu_scroll_action(
    x: Annotated[int, Field(description="Horizontal coordinate (0..1000)")],
    y: Annotated[int, Field(description="Vertical coordinate (0..1000)")],
    direction: Annotated[
        Literal["up", "down", "left", "right"], Field(description="Scroll direction")
    ] = "down",
    amount: Annotated[int, Field(description="Wheel notches (1..50)")] = 3,
    monitor: Annotated[
        str, Field(description="Monitor: 'active', 'primary', 'secondary', '1'/'2'")
    ] = "active",
    wait_after_s: Annotated[
        float, Field(description="Post-action wait before capture (0..5s)")
    ] = 0.0,
    verify_mode: Annotated[
        Literal["none", "clean", "grid", "som"],
        Field(description="Inline capture mode (default 'grid')"),
    ] = "grid",
) -> list[TextContent | ImageContent]:
    t0 = time.perf_counter()
    try:
        _CORNER_FAILSAFE.start()
        monitors = enumerate_monitors()
        _, fg_rect = get_foreground_window_info()
        cursor_px = get_cursor_pos()
        mon = resolve_monitor(monitors, monitor, fg_rect=fg_rect, cursor_pos=cursor_px)
        px_x, px_y = norm_to_physical(mon, x, y)
        fg_title = get_foreground_window_title()
        target_at_pt = get_window_title_at_point(px_x, px_y)
        check_window_denylist([target_at_pt or fg_title])
        _check_target_uipi_at_point(px_x, px_y, target_at_pt or fg_title)
        _BREAKER.check_and_record(
            "arisu_cu_scroll_action",
            fg_title,
            {"direction": direction, "amount": amount, "px": [px_x, px_y]},
        )
        get_hud_client().send_event(
            "state",
            state="working",
            text=f"Scroll {direction} di Monitor {mon.index}…",
            mood="smile",
        )
        try:
            res = execute_scroll_action(px_x=px_x, px_y=px_y, direction=direction, amount=amount)
        finally:
            clear_capture_cache()
        _SESSION.mutation_count += 1
        res.update(
            {
                "ok": True,
                "monitor": mon.index,
                "foreground_window_title": get_foreground_window_title(),
            }
        )
        _AUDIT.log(
            session_id=_SESSION.session_id,
            tool="arisu_cu_scroll_action",
            status="ok",
            monitor=mon.index,
            target_window=fg_title,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            args={"direction": direction, "amount": amount},
        )
        return _append_inline_verification(
            res,
            monitor=str(mon.index),
            wait_after_s=wait_after_s,
            verify_mode=verify_mode,
            default_wait_s=0.6,
        )
    except ArisuError as exc:
        _notify_hud_error(exc.code)
        return _err_contents(exc.code, exc.message)
    except Exception as exc:
        _notify_hud_error("SCROLL_ERROR")
        return _err_contents("SCROLL_ERROR", str(exc))


@mcp.tool(
    name="arisu_cu_window_action",
    description=(
        "Focus, minimize, maximize, restore, move across monitors, or launch an allowlisted app."
    ),
    structured_output=False,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def arisu_cu_window_action(
    action: Annotated[
        Literal["focus", "minimize", "maximize", "restore", "move_to_monitor", "launch"],
        Field(description="Window management action or launch"),
    ],
    title_or_hwnd: Annotated[str, Field(description="Title substring, HWND, or allowlisted app")],
    target_monitor: Annotated[
        str | None, Field(description="Target monitor ('1', '2', 'primary', 'secondary')")
    ] = None,
    wait_after_s: Annotated[
        float, Field(description="Post-action wait before capture (0..5s)")
    ] = 0.0,
    verify_mode: Annotated[
        Literal["none", "clean", "grid", "som"],
        Field(description="Inline capture mode (default 'grid'; 'none' on minimize)"),
    ] = "grid",
) -> list[TextContent | ImageContent]:
    t0 = time.perf_counter()
    try:
        _CORNER_FAILSAFE.start()
        monitors = enumerate_monitors()
        check_window_denylist(title_or_hwnd)
        if (
            action != "launch"
            and getattr(execute_window_action, "__module__", "") == "arisu_cua.input.window_mgr"
        ):
            preview = find_window(monitors, title_or_hwnd)
            check_window_denylist(preview.title)

        _BREAKER.check_and_record(
            "arisu_cu_window_action",
            title_or_hwnd,
            {"action": action, "target_monitor": target_monitor},
        )
        get_hud_client().send_event(
            "state",
            state="working",
            text=f"Atur jendela {title_or_hwnd[:24]}!",
            mood="laugh",
        )
        try:
            res = execute_window_action(
                action=action,
                title_or_hwnd=title_or_hwnd,
                target_monitor=target_monitor,
                monitors=monitors,
            )
        finally:
            clear_capture_cache()
        check_window_denylist(str(res.get("title") or ""))

        _SESSION.mutation_count += 1
        res["ok"] = True
        res["foreground_window_title"] = get_foreground_window_title()

        warnings = res.setdefault("warnings", [])
        if (
            action in ("focus", "maximize", "restore", "move_to_monitor", "launch")
            and res.get("focused") is False
        ):
            warnings.append("FOCUS_NOT_CONFIRMED")
        if action == "maximize" and res.get("is_maximized") is False:
            warnings.append("MAXIMIZE_BLOCKED_BY_UIPI")
        if not warnings:
            res.pop("warnings", None)

        _AUDIT.log(
            session_id=_SESSION.session_id,
            tool="arisu_cu_window_action",
            status="ok",
            monitor=target_monitor or res.get("monitor"),
            target_window=str(res.get("title") or title_or_hwnd),
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            args={"action": action, "title_or_hwnd": title_or_hwnd},
        )
        effective_verify = "none" if action == "minimize" else verify_mode
        verify_mon = str(target_monitor or res.get("monitor") or "active")
        return _append_inline_verification(
            res,
            monitor=verify_mon,
            wait_after_s=wait_after_s,
            verify_mode=effective_verify,
            default_wait_s=WINDOW_VERIFY_DEFAULT_S,
        )
    except ArisuError as exc:
        _notify_hud_error(exc.code)
        _AUDIT.log(
            session_id=_SESSION.session_id,
            tool="arisu_cu_window_action",
            status="blocked",
            target_window=title_or_hwnd,
            code=exc.code,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
        )
        return _err_contents(exc.code, exc.message)
    except Exception as exc:
        _notify_hud_error("WINDOW_ERROR")
        return _err_contents("WINDOW_ERROR", str(exc))


def _browser_wait(wait_after_s: float, default_s: float) -> None:
    delay = default_s if wait_after_s <= 0.0 else float(wait_after_s)
    clamped = min(max(delay, 0.0), MAX_WAIT_SECONDS)
    if clamped > 0.0:
        abortable_sleep(clamped)


@mcp.tool(
    name="arisu_cu_browser_action",
    description=(
        "Fast DOM browser control over isolated Chrome CDP "
        "(launch, navigate, snapshot, click, fill, select, eval, screenshot)."
    ),
    structured_output=False,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def arisu_cu_browser_action(
    action: Annotated[
        Literal[
            "launch",
            "connect",
            "tabs",
            "switch_tab",
            "close_tab",
            "navigate",
            "snapshot",
            "click",
            "fill",
            "select",
            "eval",
            "screenshot",
        ],
        Field(description="CDP browser fast-lane operation"),
    ],
    url: Annotated[str | None, Field(description="Target URL for launch or navigate")] = None,
    target_match: Annotated[
        str | None, Field(description="Tab URL or title substring filter")
    ] = None,
    ref_id: Annotated[int | None, Field(description="Element ref_id [1..N] from snapshot")] = None,
    selector: Annotated[
        str | None, Field(description="CSS selector for click, fill, or select")
    ] = None,
    text: Annotated[
        str | None, Field(description="Visible text/label match for click or fill")
    ] = None,
    value: Annotated[
        str | None, Field(description="Input text or option value for fill/select")
    ] = None,
    expression: Annotated[
        str | None, Field(description="JavaScript expression for action='eval'")
    ] = None,
    profile: Annotated[
        str, Field(description="Isolated profile name ('default' or 'digitalent')")
    ] = "default",
    monitor: Annotated[
        str, Field(description="Target monitor on launch ('1', '2', 'active')")
    ] = "2",
    press_enter: Annotated[bool, Field(description="Submit/press Enter after fill")] = False,
    snapshot_after: Annotated[
        bool, Field(description="Include DOM snapshot after navigate/click/fill")
    ] = True,
    approval_token: Annotated[
        str | None, Field(description="Single-use HUD approval token")
    ] = None,
    wait_after_s: Annotated[
        float, Field(description="Post-action wait before snapshot (0..5s)")
    ] = 0.0,
) -> list[TextContent | ImageContent]:
    t0 = time.perf_counter()
    redact = False
    try:
        _CORNER_FAILSAFE.start()
        check_ready()
        if url is not None:
            check_browser_target_denylist(None, url)
        if target_match is not None:
            check_browser_target_denylist(target_match, target_match)

        cdp = get_cdp_client()
        hud = get_hud_client()

        if action == "launch":
            monitors = enumerate_monitors()
            mon = resolve_monitor(monitors, monitor)
            mon_rect = (mon.left, mon.top, mon.width, mon.height)
            _BREAKER.check_and_record(
                "arisu_cu_browser_action",
                url or profile,
                {"action": "launch", "profile": profile, "url": url},
            )
            hud.send_event("state", state="working", text=f"Chrome CDP ({profile})~", mood="wink")
            launch_res = cdp.launch_or_connect(profile=profile, url=url, monitor_rect=mon_rect)
            tab = cdp.resolve_tab(int(launch_res["port"]), target_match)
            check_browser_target_denylist(tab.title, tab.url)
            _SESSION.mutation_count += 1
            out: dict[str, Any] = {
                "ok": True,
                "action": "launch",
                **launch_res,
                "tab": tab.to_dict(),
            }
            if url and snapshot_after:
                out["snapshot"] = cdp.snapshot(tab.ws_url)
            _AUDIT.log(
                session_id=_SESSION.session_id,
                tool="arisu_cu_browser_action",
                status="ok",
                target_window=tab.title or tab.url,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                args={"action": "launch", "profile": profile, "url": url},
            )
            return [TextContent(type="text", text=json.dumps(out))]

        if action == "connect":
            port = cdp.discover_port(profile=profile)
            if port is None:
                raise BrowserConnectionError(
                    "No active CDP browser found; call arisu_cu_browser_action(action='launch')"
                )
            tabs = cdp.list_tabs(port)
            safe_tabs = []
            for t in tabs:
                check_browser_target_denylist(t.title, t.url)
                safe_tabs.append(t.to_dict())
            _AUDIT.log(
                session_id=_SESSION.session_id,
                tool="arisu_cu_browser_action",
                status="ok",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                args={"action": "connect", "profile": profile},
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"ok": True, "action": "connect", "port": port, "tabs": safe_tabs}
                    ),
                )
            ]

        port = cdp.discover_port(profile=profile)
        if port is None and action == "navigate":
            monitors = enumerate_monitors()
            mon = resolve_monitor(monitors, monitor)
            mon_rect = (mon.left, mon.top, mon.width, mon.height)
            launch_res = cdp.launch_or_connect(profile=profile, url=None, monitor_rect=mon_rect)
            port = int(launch_res["port"])
        if port is None:
            raise BrowserConnectionError(
                "No active CDP browser found; call arisu_cu_browser_action(action='launch')"
            )

        if action == "tabs":
            tabs = cdp.list_tabs(port)
            safe_tabs = []
            for t in tabs:
                check_browser_target_denylist(t.title, t.url)
                safe_tabs.append(t.to_dict())
            _AUDIT.log(
                session_id=_SESSION.session_id,
                tool="arisu_cu_browser_action",
                status="ok",
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                args={"action": "tabs", "port": port},
            )
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {"ok": True, "action": "tabs", "port": port, "tabs": safe_tabs}
                    ),
                )
            ]

        tab = cdp.resolve_tab(port, target_match)
        check_browser_target_denylist(tab.title, tab.url)
        res_out: dict[str, Any] = {"ok": True, "action": action, "port": port, "tab": tab.to_dict()}

        if action == "switch_tab":
            hud.send_event("state", state="working", text="Pindah tab Chrome~", mood="smile")
            if snapshot_after:
                res_out["snapshot"] = cdp.snapshot(tab.ws_url)
        elif action == "close_tab":
            _BREAKER.check_and_record(
                "arisu_cu_browser_action",
                tab.title or tab.url,
                {"action": "close_tab", "target_id": tab.target_id},
            )
            cdp.close_tab(port, tab.target_id)
            _SESSION.mutation_count += 1
        elif action == "navigate":
            if not url:
                raise InvalidParamComboError("url is required for action='navigate'")
            _BREAKER.check_and_record(
                "arisu_cu_browser_action",
                tab.title or tab.url,
                {"action": "navigate", "url": url},
            )
            hud.send_event("state", state="searching", text=f"Buka {url[:26]}~", mood="focused")
            nav_res = cdp.navigate(tab.ws_url, url)
            check_browser_target_denylist(
                str(nav_res.get("title") or ""), str(nav_res.get("url") or url)
            )
            _browser_wait(wait_after_s, 0.0)
            res_out["navigation"] = nav_res
            if snapshot_after:
                snap = cdp.snapshot(tab.ws_url)
                check_browser_target_denylist(
                    str(snap.get("title") or ""), str(snap.get("url") or url)
                )
                res_out["snapshot"] = snap
            _SESSION.mutation_count += 1
        elif action == "snapshot":
            _browser_wait(wait_after_s, 0.0)
            hud.send_event("state", state="searching", text="Baca DOM halaman~", mood="smile")
            snap = cdp.snapshot(tab.ws_url)
            check_browser_target_denylist(
                str(snap.get("title") or tab.title), str(snap.get("url") or tab.url)
            )
            res_out["snapshot"] = snap
        elif action == "click":
            if ref_id is None and not selector and not text:
                raise InvalidParamComboError(
                    "ref_id, selector, or text is required for action='click'"
                )
            sec = cdp.inspect_element_security(
                tab.ws_url, ref_id=ref_id, selector=selector, text=text
            )
            if not sec.get("found"):
                raise BrowserElementNotFoundError(str(ref_id or selector or text))
            if (
                is_sensitive_button_name(sec.get("text"))
                or is_sensitive_button_name(sec.get("label"))
                or is_sensitive_button_name(text)
            ):
                label_desc = str(sec.get("text") or sec.get("label") or text or selector or ref_id)
                _enforce_out_of_band_approval(
                    tool="arisu_cu_browser_action",
                    target_window=tab.title or tab.url,
                    action_desc=f"Browser click '{label_desc}'",
                    payload={
                        "action": "click",
                        "ref_id": ref_id,
                        "selector": selector,
                        "text": text,
                    },
                    approval_token=approval_token,
                )
            _BREAKER.check_and_record(
                "arisu_cu_browser_action",
                tab.title or tab.url,
                {"action": "click", "ref_id": ref_id, "selector": selector, "text": text},
            )
            cue = str(sec.get("text") or text or selector or ref_id)[:22]
            hud.send_event("state", state="working", text=f"Klik DOM '{cue}'~", mood="wink")
            act = cdp.click_element(tab.ws_url, ref_id=ref_id, selector=selector, text=text)
            if not act.get("clicked"):
                raise BrowserElementNotFoundError(str(ref_id or selector or text))
            res_out["executed"] = True
            res_out["click"] = act
            _browser_wait(wait_after_s, 0.35)
            if snapshot_after:
                snap = cdp.snapshot(tab.ws_url)
                check_browser_target_denylist(
                    str(snap.get("title") or ""), str(snap.get("url") or "")
                )
                res_out["snapshot"] = snap
            _SESSION.mutation_count += 1
        elif action in ("fill", "select"):
            if (ref_id is None and not selector and not text) or value is None:
                raise InvalidParamComboError(
                    "ref_id/selector/text and value are required for fill or select"
                )
            sec = cdp.inspect_element_security(
                tab.ws_url, ref_id=ref_id, selector=selector, text=text
            )
            if not sec.get("found"):
                raise BrowserElementNotFoundError(str(ref_id or selector or text))
            is_pw = sec.get("is_password")
            if is_pw is None or is_pw is True:
                redact = True
            _enforce_password_guard(
                is_pw,
                tool="arisu_cu_browser_action",
                target_window=tab.title or tab.url,
                payload={"action": action, "ref_id": ref_id, "selector": selector, "text": text},
                approval_token=approval_token,
            )
            _BREAKER.check_and_record(
                "arisu_cu_browser_action",
                tab.title or tab.url,
                {
                    "action": action,
                    "ref_id": ref_id,
                    "selector": selector,
                    "text": text,
                    "press_enter": press_enter,
                },
            )
            hud.send_event(
                "state", state="typing", text=f"Isi DOM ({len(value)} kar)~", mood="focused"
            )
            act = cdp.fill_element(
                tab.ws_url,
                value,
                ref_id=ref_id,
                selector=selector,
                text=text,
                press_enter=press_enter,
            )
            if not act.get("filled"):
                raise BrowserElementNotFoundError(str(ref_id or selector or text))
            res_out["executed"] = True
            res_out["fill"] = act
            _browser_wait(wait_after_s, 0.9 if press_enter else 0.35)
            if snapshot_after:
                snap = cdp.snapshot(tab.ws_url)
                check_browser_target_denylist(
                    str(snap.get("title") or ""), str(snap.get("url") or "")
                )
                res_out["snapshot"] = snap
            _SESSION.mutation_count += 1
        elif action == "eval":
            if not expression:
                raise InvalidParamComboError("expression is required for action='eval'")
            _BREAKER.check_and_record(
                "arisu_cu_browser_action",
                tab.title or tab.url,
                {"action": "eval", "expr_len": len(expression)},
            )
            hud.send_event("state", state="working", text="Eksekusi skrip DOM~", mood="focused")
            res_out["executed"] = True
            res_out["result"] = cdp.eval_js(tab.ws_url, expression, await_promise=True)
            _SESSION.mutation_count += 1
        elif action == "screenshot":
            _browser_wait(wait_after_s, 0.0)
            hud.send_event("state", state="searching", text="Potret tab Chrome~", mood="wink")
            monitors = enumerate_monitors()
            mon = resolve_monitor(monitors, monitor)
            shot_meta, b64 = cdp.capture_tab_webp(tab.ws_url, monitor_index=mon.index)
            res_out.update(shot_meta)
            res_out["vision_notice"] = (
                "CRITICAL: If Antigravity offloads this image to file:///.../media_0.webp, "
                "you MUST call read_file on that .webp path before taking any action."
            )
            _AUDIT.log(
                session_id=_SESSION.session_id,
                tool="arisu_cu_browser_action",
                status="ok",
                target_window=tab.title or tab.url,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                args={"action": "screenshot", "target_match": target_match},
            )
            return [
                TextContent(type="text", text=json.dumps(res_out)),
                ImageContent(type="image", data=b64, mimeType="image/webp"),
            ]

        _AUDIT.log(
            session_id=_SESSION.session_id,
            tool="arisu_cu_browser_action",
            status="ok",
            target_window=tab.title or tab.url,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            args={
                "action": action,
                "url": url,
                "ref_id": ref_id,
                "selector": selector,
                "text": text,
                "value": "[REDACTED]" if redact and value is not None else value,
                "press_enter": press_enter,
                "approval_token": approval_token,
            },
            redact_text=redact,
        )
        return [TextContent(type="text", text=json.dumps(res_out))]
    except ArisuError as exc:
        _notify_hud_error(exc.code)
        _AUDIT.log(
            session_id=_SESSION.session_id,
            tool="arisu_cu_browser_action",
            status="blocked",
            target_window=str(url or target_match or profile),
            code=exc.code,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            args={
                "action": action,
                "url": url,
                "value": "[REDACTED]" if redact and value is not None else value,
                "approval_token": approval_token,
            },
            redact_text=redact,
        )
        return _err_contents(exc.code, exc.message)
    except Exception as exc:
        _notify_hud_error("BROWSER_ERROR")
        return _err_contents("BROWSER_ERROR", str(exc))


@mcp.tool(
    name="arisu_cu_wait_and_verify",
    description="Wait and capture a verification screenshot or zoomed region.",
    structured_output=False,
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def arisu_cu_wait_and_verify(
    seconds: Annotated[float, Field(description="Wait seconds (0.1..5.0)")] = DEFAULT_WAIT_SECONDS,
    monitor: Annotated[
        str, Field(description="Monitor: 'active', 'primary', 'secondary', '1'/'2'")
    ] = "active",
    mode: Annotated[
        Literal["clean", "grid", "som"],
        Field(description="Overlay mode: 'clean', 'grid', or 'som'"),
    ] = "grid",
    fmt: Annotated[Literal["webp", "jpeg"], Field(description="Format: 'webp' or 'jpeg'")] = "webp",
    region: Annotated[
        list[int] | None,
        Field(description="Optional zoom box [x1, y1, x2, y2] in 0..1000"),
    ] = None,
) -> list[TextContent | ImageContent]:
    try:
        get_hud_client().send_event(
            "state", state="searching", text="Memverifikasi tampilan…", mood="smile"
        )
        clamped = min(max(float(seconds), 0.1), MAX_WAIT_SECONDS)
        abortable_sleep(clamped)
        return _capture_payload(monitor=monitor, mode=mode, fmt=fmt, region=region)
    except ArisuError as exc:
        _notify_hud_error(exc.code)
        return [TextContent(type="text", text=_err_json(exc.code, exc.message))]
    except Exception as exc:
        _notify_hud_error("WAIT_VERIFY_ERROR")
        return [TextContent(type="text", text=_err_json("WAIT_VERIFY_ERROR", str(exc)))]


@mcp.tool(
    name="arisu_cu_session_control",
    description="Control Arisu session lifecycle (start, pause, resume, handoff, extend, stop).",
    structured_output=False,
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def arisu_cu_session_control(
    action: Annotated[
        Literal["start", "pause", "resume", "stop", "handoff", "extend"],
        Field(description="Session lifecycle command"),
    ],
    task_label: Annotated[str | None, Field(description="Task label or hand-off prompt")] = None,
) -> str:
    hud = get_hud_client()
    extra_fields: dict[str, Any] = {}
    if action == "start":
        reset_abort()
        set_paused(False)
        clear_capture_cache()
        _BREAKER.reset()
        _SESSION.session_id = f"sess_{uuid.uuid4().hex[:8]}"
        _SESSION.active = True
        _SESSION.task_label = (task_label or "").strip()[:80]
        _SESSION.mutation_count = 0
        _CORNER_FAILSAFE.start()
        hud.ensure_hud_running()
        hud.send_event(
            "session_start",
            session_id=_SESSION.session_id,
            task_label=_SESSION.task_label or "Siap bantu!",
            mood="wink",
        )
    elif action == "extend":
        new_cap = _BREAKER.extend_cap(50)
        extra_fields["max_actions"] = new_cap
        hud.send_event(
            "state",
            state="working",
            text=f"Batas aksi ditambah -> {new_cap}~",
            mood="smile",
        )
    elif action == "pause":
        set_paused(True)
        hud.send_event("session_pause", paused=True)
    elif action == "resume":
        reset_abort()
        set_paused(False)
        hud.send_event("session_pause", paused=False)
    elif action == "handoff":
        instruction = (task_label or "").strip()[
            :80
        ] or "Selesaikan login/verifikasi manual, lalu klik LANJUT di HUD"
        hud_status = hud.ensure_hud_running()
        if not hud_status.get("running"):
            return _err_json(
                "HUD_NOT_RUNNING",
                "Arisu HUD is offline; cannot prompt interactive Co-Pilot hand-off",
            )
        req = hud.request_handoff(instruction)
        hid = str(req.get("handoff_id") or "")
        if not req.get("ok") or not hid:
            return _err_json("HUD_WS_ERROR", "Failed to register Co-Pilot hand-off on HUD")
        deadline = time.monotonic() + 180.0
        try:
            while time.monotonic() < deadline:
                abortable_sleep(0.25)
                st_res = hud.get_handoff_status(hid)
                st = str(st_res.get("status") or "")
                if st == "resumed":
                    clear_capture_cache()
                    _BREAKER.reset()
                    _AUDIT.log(
                        session_id=_SESSION.session_id,
                        tool="arisu_cu_session_control",
                        status="ok",
                        args={"action": "handoff", "status": "resumed"},
                    )
                    return json.dumps(
                        {
                            "ok": True,
                            "action": "handoff",
                            "status": "resumed",
                            "session_id": _SESSION.session_id,
                            "active": _SESSION.active,
                            "hud": hud.get_hud_status(),
                            "notice": (
                                "User finished manual step; capture the screen before "
                                "taking the next action."
                            ),
                        }
                    )
                if st == "cancelled":
                    raise HandoffCancelledError()
            raise HandoffTimeoutError(180.0)
        except ArisuError as exc:
            _notify_hud_error(exc.code)
            return _err_json(exc.code, exc.message)
    elif action == "stop":
        trigger_abort("Stopped via arisu_cu_session_control")
        _CORNER_FAILSAFE.stop()
        clear_capture_cache()
        _SESSION.active = False
        set_paused(False)
        hud.send_event("session_stop", session_id=_SESSION.session_id)
    else:
        return _err_json("INVALID_SESSION_ACTION", f"Unsupported action '{action}'")

    _AUDIT.log(
        session_id=_SESSION.session_id,
        tool="arisu_cu_session_control",
        status="ok",
        args={"action": action, "task_label": _SESSION.task_label},
    )
    return json.dumps(
        {
            "ok": True,
            "action": action,
            "session_id": _SESSION.session_id,
            "active": _SESSION.active,
            "task_label": _SESSION.task_label,
            "paused": PAUSED_EVENT.is_set(),
            "aborted": ABORT_EVENT.is_set(),
            "mutation_count": _SESSION.mutation_count,
            "hud": hud.get_hud_status(),
            **extra_fields,
        }
    )


def main() -> None:
    enable_per_monitor_dpi_v2()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
