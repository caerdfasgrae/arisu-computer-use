"""Single-instance Windows Named Mutex and local 127.0.0.1 WebSocket HUD broker."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import ctypes
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection, serve
from websockets.sync.client import connect as ws_sync_connect

from arisu_cua.config import get_hud_bridge_path
from arisu_cua.safety.guardrails import PendingApprovalStore
from arisu_cua.safety.killswitch import InvalidApprovalTokenError, set_paused, trigger_abort

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

DEFAULT_MUTEX_NAME = r"Local\ArisuCuaHudSingleInstance"
ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
UOI_NAME = 2
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
HUD_SPAWN_CREATION_FLAGS = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
_kernel32.ReleaseMutex.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD

_user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
_user32.GetThreadDesktop.restype = wintypes.HANDLE
_user32.GetUserObjectInformationW.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_user32.GetUserObjectInformationW.restype = wintypes.BOOL


class SingleInstanceMutex:
    """Windows Named Mutex enforcing a single live Arisu HUD instance."""

    def __init__(self, name: str = DEFAULT_MUTEX_NAME) -> None:
        self.name = name
        self._handle: int | None = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        ctypes.set_last_error(0)
        handle = _kernel32.CreateMutexW(None, True, self.name)
        err = ctypes.get_last_error()
        if not handle:
            return False
        if err == ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(handle)
            return False
        self._handle = int(handle)
        return True

    def release(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(Exception):
                _kernel32.ReleaseMutex(wintypes.HANDLE(self._handle))
            with contextlib.suppress(Exception):
                _kernel32.CloseHandle(wintypes.HANDLE(self._handle))
            self._handle = None

    def __enter__(self) -> SingleInstanceMutex:
        if not self.acquire():
            raise RuntimeError(f"SINGLE_INSTANCE_ACTIVE: Mutex '{self.name}' already held")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD(0)
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return int(code.value) == STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)


def _is_on_default_desktop() -> bool:
    hdesk = _user32.GetThreadDesktop(_kernel32.GetCurrentThreadId())
    if not hdesk:
        return False
    buf = ctypes.create_unicode_buffer(128)
    needed = wintypes.DWORD(0)
    ok = _user32.GetUserObjectInformationW(
        hdesk, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)
    )
    return bool(ok) and buf.value.strip().lower() == "default"


def rect_contains_point(
    rect: list[int] | tuple[int, int, int, int] | None, px_x: int, px_y: int
) -> bool:
    if not rect or len(rect) != 4:
        return False
    x1, y1, x2, y2 = (int(v) for v in rect)
    return x1 <= px_x <= x2 and y1 <= px_y <= y2


class HudBridgeServer:
    """Localhost authenticated WebSocket broker owned by the HUD overlay process."""

    def __init__(
        self,
        bridge_path: Path | None = None,
        port: int = 0,
        token: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.bridge_path = bridge_path or get_hud_bridge_path()
        self._requested_port = int(port)
        self.port: int = 0
        self.token = token or secrets.token_hex(16)
        self._on_event = on_event

        self.hwnd: int = 0
        self.monitor: int = 1
        self.hud_rect: tuple[int, int, int, int] = (0, 0, 280, 360)
        self.current_state: str = "idle"
        self.paused: bool = False
        self.force_click_through: bool = False
        self.sessions: dict[str, dict[str, Any]] = {}
        self.approval_store = PendingApprovalStore(ttl_s=60.0)
        self._handoff_decisions: dict[str, str] = {}

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_async: asyncio.Event | None = None
        self._auth_clients: set[ServerConnection] = set()
        self._lock = threading.Lock()

    def _write_bridge_file(self) -> None:
        payload = {
            "pid": os.getpid(),
            "host": "127.0.0.1",
            "port": self.port,
            "token": self.token,
            "hwnd": self.hwnd,
            "monitor": self.monitor,
            "hud_rect": list(self.hud_rect),
            "state": self.current_state,
            "paused": self.paused,
            "updated_at": round(time.time(), 3),
        }
        self.bridge_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.bridge_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.bridge_path)

    def update_geometry(
        self,
        hwnd: int,
        monitor: int,
        hud_rect: tuple[int, int, int, int],
    ) -> None:
        with self._lock:
            self.hwnd = int(hwnd)
            self.monitor = int(monitor)
            self.hud_rect = (
                int(hud_rect[0]),
                int(hud_rect[1]),
                int(hud_rect[2]),
                int(hud_rect[3]),
            )
            if self.port > 0:
                self._write_bridge_file()

    def _dispatch_event_callback(self, req: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        with contextlib.suppress(Exception):
            self._on_event(req)

    async def _handle_client(self, ws: ServerConnection) -> None:
        try:
            async for raw in ws:
                try:
                    req = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send(json.dumps({"ok": False, "code": "BAD_JSON"}))
                    continue

                if not isinstance(req, dict) or req.get("token") != self.token:
                    await ws.send(
                        json.dumps(
                            {
                                "ok": False,
                                "code": "UNAUTHORIZED",
                                "error": "Invalid HUD bridge token",
                            }
                        )
                    )
                    continue

                self._auth_clients.add(ws)
                ev_type = str(req.get("type") or "")

                if ev_type == "subscribe":
                    await ws.send(
                        json.dumps(
                            {
                                "ok": True,
                                "type": "subscribed",
                                "state": self.current_state,
                                "paused": self.paused,
                                "monitor": self.monitor,
                                "hud_rect": list(self.hud_rect),
                            }
                        )
                    )
                elif ev_type == "get_status":
                    await ws.send(
                        json.dumps(
                            {
                                "ok": True,
                                "running": True,
                                "pid": os.getpid(),
                                "hwnd": self.hwnd,
                                "monitor": self.monitor,
                                "hud_rect": list(self.hud_rect),
                                "state": self.current_state,
                                "paused": self.paused,
                                "force_click_through": self.force_click_through,
                            }
                        )
                    )
                elif ev_type == "session_start":
                    sid = str(req.get("session_id") or f"sess_{secrets.token_hex(4)}")
                    self.sessions[sid] = {
                        "task_label": str(req.get("task_label") or ""),
                        "started_at": time.time(),
                    }
                    self.current_state = "working"
                    self.paused = False
                    self._write_bridge_file()
                    self._dispatch_event_callback(req)
                    await ws.send(json.dumps({"ok": True, "session_id": sid}))
                elif ev_type == "session_pause":
                    self.paused = bool(req.get("paused", False))
                    self._write_bridge_file()
                    self._dispatch_event_callback(req)
                    await ws.send(json.dumps({"ok": True, "paused": self.paused}))
                elif ev_type == "session_stop":
                    sid = str(req.get("session_id") or "")
                    if sid:
                        self.sessions.pop(sid, None)
                    self.current_state = "idle"
                    self.paused = False
                    self._write_bridge_file()
                    self._dispatch_event_callback(req)
                    await ws.send(json.dumps({"ok": True, "state": "idle"}))
                elif ev_type == "state":
                    self.current_state = str(req.get("state") or self.current_state)
                    self._write_bridge_file()
                    self._dispatch_event_callback(req)
                    await ws.send(json.dumps({"ok": True, "state": self.current_state}))
                elif ev_type == "set_click_through":
                    self.force_click_through = bool(req.get("enabled", False))
                    self._dispatch_event_callback(req)
                    await ws.send(
                        json.dumps({"ok": True, "force_click_through": self.force_click_through})
                    )
                elif ev_type == "request_approval":
                    pending = self.approval_store.create_request(
                        action_hash=str(req.get("action_hash") or ""),
                        action_desc=str(req.get("action_desc") or "Aksi sensitif"),
                        target_window=str(req.get("target_window") or ""),
                    )
                    self._dispatch_event_callback(
                        {
                            "type": "approval_request",
                            "request_id": pending.request_id,
                            "action_hash": pending.action_hash,
                            "action_desc": pending.action_desc,
                            "target_window": pending.target_window,
                        }
                    )
                    await ws.send(
                        json.dumps(
                            {
                                "ok": True,
                                "request_id": pending.request_id,
                                "action_hash": pending.action_hash,
                            }
                        )
                    )
                elif ev_type == "consume_approval":
                    action_hash = str(req.get("action_hash") or "")
                    approval_token = req.get("approval_token")
                    try:
                        approved = self.approval_store.verify_and_consume(
                            action_hash=action_hash,
                            token=None if approval_token is None else str(approval_token),
                        )
                        await ws.send(json.dumps({"ok": True, "approved": approved}))
                    except InvalidApprovalTokenError as exc:
                        await ws.send(
                            json.dumps({"ok": False, "code": exc.code, "error": exc.message})
                        )
                elif ev_type == "handoff_request":
                    hid = f"ho_{secrets.token_hex(4)}"
                    instruction = str(
                        req.get("instruction")
                        or "Selesaikan aksi manual di layar, lalu klik LANJUT"
                    )
                    with self._lock:
                        self._handoff_decisions[hid] = "pending"
                    self._dispatch_event_callback(
                        {
                            "type": "handoff_request",
                            "handoff_id": hid,
                            "instruction": instruction,
                        }
                    )
                    await ws.send(json.dumps({"ok": True, "handoff_id": hid, "status": "pending"}))
                elif ev_type == "get_handoff_status":
                    hid = str(req.get("handoff_id") or "")
                    with self._lock:
                        status = self._handoff_decisions.get(hid, "unknown")
                    await ws.send(
                        json.dumps(
                            {
                                "ok": status != "unknown",
                                "handoff_id": hid,
                                "status": status,
                            }
                        )
                    )
                elif ev_type == "cursor_cue":
                    self._dispatch_event_callback(req)
                    await ws.send(json.dumps({"ok": True}))
                else:
                    self._dispatch_event_callback(req)
                    await ws.send(json.dumps({"ok": True}))
        except Exception:
            pass
        finally:
            self._auth_clients.discard(ws)

    async def _async_broadcast(self, raw_payload: str) -> None:
        stale: list[ServerConnection] = []
        for ws in list(self._auth_clients):
            try:
                await ws.send(raw_payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self._auth_clients.discard(ws)

    def _broadcast(self, payload: dict[str, Any]) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(self._async_broadcast(json.dumps(payload)), self._loop)

    def trigger_hud_stop(self, reason: str = "Stopped via Arisu HUD STOP button") -> None:
        self.current_state = "idle"
        self.paused = False
        trigger_abort(reason)
        with self._lock:
            if self.port > 0:
                self._write_bridge_file()
        self._broadcast({"type": "control", "action": "stop", "reason": reason})

    def trigger_hud_pause(self, paused: bool) -> None:
        self.paused = bool(paused)
        set_paused(self.paused)
        with self._lock:
            if self.port > 0:
                self._write_bridge_file()
        self._broadcast({"type": "control", "action": "pause", "paused": self.paused})

    def trigger_hud_approval_decision(self, request_id: str, approved: bool) -> str | None:
        """Mint single-use token or deny a pending request solely from physical HUD click."""
        if approved:
            token = self.approval_store.approve_request(request_id)
            self._broadcast(
                {
                    "type": "control",
                    "action": "approval_decision",
                    "request_id": request_id,
                    "approved": True,
                    "token": token,
                }
            )
            return token
        self.approval_store.deny_request(request_id)
        self._broadcast(
            {
                "type": "control",
                "action": "approval_decision",
                "request_id": request_id,
                "approved": False,
            }
        )
        return None

    def trigger_hud_handoff_decision(self, handoff_id: str, resume: bool) -> None:
        """Record user Co-Pilot hand-off decision (LANJUT / BATAL) from physical HUD control."""
        hid = str(handoff_id)
        with self._lock:
            self._handoff_decisions[hid] = "resumed" if resume else "cancelled"
        self._broadcast(
            {
                "type": "control",
                "action": "handoff_decision",
                "handoff_id": hid,
                "resume": bool(resume),
            }
        )

    def start(self, timeout_s: float = 3.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._stop_async = asyncio.Event()

            async def _serve() -> None:
                async with serve(self._handle_client, "127.0.0.1", self._requested_port) as srv:
                    sock = srv.sockets[0]
                    self.port = int(sock.getsockname()[1])
                    with self._lock:
                        self._write_bridge_file()
                    self._ready.set()
                    assert self._stop_async is not None
                    await self._stop_async.wait()

            try:
                loop.run_until_complete(_serve())
            finally:
                with contextlib.suppress(Exception):
                    loop.close()

        self._thread = threading.Thread(target=_runner, daemon=True, name="arisu-hud-ws-broker")
        self._thread.start()
        if not self._ready.wait(timeout=timeout_s):
            raise TimeoutError("HUD WebSocket broker failed to bind on 127.0.0.1")

    def stop(self) -> None:
        loop = self._loop
        stop_ev = self._stop_async
        if loop is not None and stop_ev is not None and not loop.is_closed():
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(stop_ev.set)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            if self.bridge_path.exists():
                info = json.loads(self.bridge_path.read_text(encoding="utf-8"))
                if int(info.get("pid", 0)) == os.getpid():
                    self.bridge_path.unlink(missing_ok=True)
        except Exception:
            pass


class HudBridgeClient:
    """MCP-side non-blocking client to send state updates and listen for HUD STOP/PAUSE."""

    def __init__(self, bridge_path: Path | None = None, timeout_s: float = 0.45) -> None:
        self.bridge_path = bridge_path or get_hud_bridge_path()
        self._timeout_s = float(timeout_s)
        self._lock = threading.Lock()
        self._stop_listener = threading.Event()
        self._subscribed_event = threading.Event()
        self._listener_thread: threading.Thread | None = None
        self._sub_ws: Any = None
        self._sub_endpoint: tuple[int, str] | None = None

    def _read_bridge_info(self) -> dict[str, Any] | None:
        if not self.bridge_path.exists():
            return None
        try:
            info = json.loads(self.bridge_path.read_text(encoding="utf-8"))
            pid = int(info.get("pid", 0))
            if not _is_pid_alive(pid):
                return None
            if not info.get("port") or not info.get("token"):
                return None
            return info
        except Exception:
            return None

    def get_hud_status(self) -> dict[str, Any]:
        info = self._read_bridge_info()
        if info is None:
            return {
                "running": False,
                "monitor": None,
                "hud_rect": None,
                "state": "offline",
                "paused": False,
            }
        return {
            "running": True,
            "pid": int(info["pid"]),
            "monitor": info.get("monitor"),
            "hud_rect": info.get("hud_rect"),
            "state": str(info.get("state") or "idle"),
            "paused": bool(info.get("paused", False)),
        }

    def start_listener(self) -> None:
        info = self._read_bridge_info()
        if info is None:
            return
        endpoint = (int(info["port"]), str(info["token"]))
        with self._lock:
            if (
                self._listener_thread is not None
                and self._listener_thread.is_alive()
                and self._sub_endpoint == endpoint
            ):
                return
            self._stop_listener.set()
            if self._sub_ws is not None:
                with contextlib.suppress(Exception):
                    self._sub_ws.close()
            self._stop_listener = threading.Event()
            self._subscribed_event.clear()
            self._sub_endpoint = endpoint

            def _listen() -> None:
                url = f"ws://127.0.0.1:{endpoint[0]}"
                try:
                    with ws_sync_connect(url, open_timeout=1.5, close_timeout=0.5) as ws:
                        self._sub_ws = ws
                        ws.send(json.dumps({"type": "subscribe", "token": endpoint[1]}))
                        while not self._stop_listener.is_set():
                            try:
                                raw = ws.recv(timeout=0.25)
                            except TimeoutError:
                                continue
                            msg = json.loads(raw)
                            mtype = msg.get("type")
                            if mtype == "subscribed":
                                self._subscribed_event.set()
                            elif mtype == "control":
                                action = msg.get("action")
                                if action == "stop":
                                    trigger_abort(str(msg.get("reason") or "Stopped via Arisu HUD"))
                                elif action == "pause":
                                    set_paused(bool(msg.get("paused", False)))
                except Exception:
                    pass
                finally:
                    self._sub_ws = None

            self._listener_thread = threading.Thread(
                target=_listen, daemon=True, name="arisu-hud-ws-listener"
            )
            self._listener_thread.start()

    def wait_subscribed(self, timeout_s: float = 2.0) -> bool:
        return self._subscribed_event.wait(timeout=timeout_s)

    def send_event(
        self,
        event_type: str,
        token_override: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        info = self._read_bridge_info()
        if info is None:
            return {"ok": False, "code": "HUD_NOT_RUNNING"}

        url = f"ws://{info.get('host', '127.0.0.1')}:{int(info['port'])}"
        token = token_override if token_override is not None else str(info["token"])
        payload = {"type": event_type, "token": token, **fields}
        try:
            with ws_sync_connect(
                url, open_timeout=self._timeout_s, close_timeout=self._timeout_s
            ) as ws:
                ws.send(json.dumps(payload))
                raw = ws.recv(timeout=self._timeout_s)
                return json.loads(raw)
        except Exception as exc:
            return {"ok": False, "code": "HUD_WS_ERROR", "error": str(exc)}

    def ensure_hud_running(self, timeout_s: float = 2.5) -> dict[str, Any]:
        status = self.get_hud_status()
        if status["running"]:
            self.start_listener()
            return status
        if not _is_on_default_desktop():
            return status

        subprocess.Popen(
            [sys.executable, "-m", "arisu_cua.hud.overlay"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=HUD_SPAWN_CREATION_FLAGS,
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self.get_hud_status()
            if status["running"]:
                self.start_listener()
                return status
            time.sleep(0.06)
        return self.get_hud_status()

    def request_approval(
        self, action_hash: str, action_desc: str, target_window: str = ""
    ) -> dict[str, Any]:
        return self.send_event(
            "request_approval",
            action_hash=action_hash,
            action_desc=action_desc,
            target_window=target_window,
        )

    def consume_approval(
        self, action_hash: str, approval_token: str | None = None
    ) -> dict[str, Any]:
        return self.send_event(
            "consume_approval",
            action_hash=action_hash,
            approval_token=approval_token,
        )

    def request_handoff(self, instruction: str) -> dict[str, Any]:
        return self.send_event("handoff_request", instruction=instruction)

    def get_handoff_status(self, handoff_id: str) -> dict[str, Any]:
        return self.send_event("get_handoff_status", handoff_id=handoff_id)

    def send_cursor_cue(
        self, action: str, target_px: tuple[int, int], duration_ms: int = 180
    ) -> dict[str, Any]:
        return self.send_event(
            "cursor_cue",
            action=action,
            x=int(target_px[0]),
            y=int(target_px[1]),
            duration_ms=int(duration_ms),
        )

    def close(self) -> None:
        self._stop_listener.set()
        if self._sub_ws is not None:
            with contextlib.suppress(Exception):
                self._sub_ws.close()
        if self._listener_thread is not None:
            self._listener_thread.join(timeout=1.5)
            self._listener_thread = None


_HUD_CLIENT_SINGLETON: HudBridgeClient | None = None
_HUD_CLIENT_LOCK = threading.Lock()


def get_hud_client() -> HudBridgeClient:
    global _HUD_CLIENT_SINGLETON
    with _HUD_CLIENT_LOCK:
        if _HUD_CLIENT_SINGLETON is None:
            client = HudBridgeClient()
            atexit.register(client.close)
            _HUD_CLIENT_SINGLETON = client
        return _HUD_CLIENT_SINGLETON
