"""Supervisor client for the killable UIA worker subprocess and SoM registry."""

from __future__ import annotations

import atexit
import json
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from arisu_cua.config import FOCUS_POLL_S, FOCUS_WAIT_MAX_S, UIA_TIMEOUT_SECONDS
from arisu_cua.display.monitors import MonitorInfo
from arisu_cua.safety.killswitch import ElementNotFoundError, UIATimeoutError, abortable_sleep


@dataclass(frozen=True)
class ElementBox:
    """Structured UI Automation element with physical and normalized 0..1000 bounds."""

    element_id: int
    name: str
    control_type: str
    automation_id: str
    bbox_px: tuple[int, int, int, int]
    center_px: tuple[int, int]
    bbox_norm: tuple[int, int, int, int]
    center_norm: tuple[int, int]
    is_password: bool
    monitor_index: int
    window_title: str

    def to_dict(self) -> dict:
        return {
            "id": self.element_id,
            "name": self.name,
            "type": self.control_type,
            "automation_id": self.automation_id,
            "bbox_px": list(self.bbox_px),
            "center_px": list(self.center_px),
            "bbox_norm": list(self.bbox_norm),
            "center_norm": list(self.center_norm),
            "is_password": self.is_password,
            "monitor": self.monitor_index,
            "window_title": self.window_title,
        }


class UIASubprocessClient:
    """Manages a persistent killable UIA worker subprocess with hard timeouts."""

    def __init__(self, timeout_s: float = UIA_TIMEOUT_SECONDS) -> None:
        self._default_timeout_s = float(timeout_s)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[dict] = queue.Queue()
        self._som_registry: dict[int, ElementBox] = {}
        self._som_lock = threading.Lock()

    def _spawn(self) -> None:
        msg_queue: queue.Queue[dict] = queue.Queue()
        proc = subprocess.Popen(
            [sys.executable, "-m", "arisu_cua.perception.uia_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

        def _read_stdout() -> None:
            assert proc.stdout is not None
            try:
                for line in proc.stdout:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        msg_queue.put(json.loads(stripped))
                    except json.JSONDecodeError:
                        continue
            finally:
                msg_queue.put({"ok": False, "error": "UIA_WORKER_EOF"})

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            try:
                for _ in proc.stderr:
                    pass
            except Exception:
                pass

        threading.Thread(target=_read_stdout, daemon=True, name="uia-stdout-reader").start()
        threading.Thread(target=_drain_stderr, daemon=True, name="uia-stderr-drain").start()

        self._proc = proc
        self._queue = msg_queue

    def _terminate_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=3.0)
        except Exception:
            pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:
                pass

    def _kill_and_respawn(self) -> None:
        self._terminate_proc()
        self._spawn()

    def close(self) -> None:
        with self._lock:
            self._terminate_proc()

    def request(self, payload: dict, timeout_s: float | None = None) -> dict:
        effective_timeout = self._default_timeout_s if timeout_s is None else float(timeout_s)
        line = json.dumps(payload, ensure_ascii=False) + "\n"

        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._terminate_proc()
                self._spawn()

            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

            assert self._proc is not None and self._proc.stdin is not None
            try:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._kill_and_respawn()
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write(line)
                self._proc.stdin.flush()

            try:
                resp = self._queue.get(timeout=effective_timeout)
            except queue.Empty as exc:
                self._kill_and_respawn()
                raise UIATimeoutError(effective_timeout) from exc

            if resp.get("error") == "UIA_WORKER_EOF":
                self._kill_and_respawn()
                raise RuntimeError("UIA_WORKER_CRASHED: worker exited unexpectedly and respawned")
            return resp

    def ping(self, timeout_s: float | None = None) -> dict:
        return self.request({"cmd": "ping"}, timeout_s=timeout_s)

    def inspect_tree(
        self,
        mon: MonitorInfo,
        window_title: str | None = None,
        hwnd: int | None = None,
        max_depth: int = 4,
        max_items: int = 50,
        interactive_only: bool = False,
        timeout_s: float | None = None,
    ) -> tuple[str, list[ElementBox]]:
        resp = self.request(
            {
                "cmd": "inspect_tree",
                "hwnd": hwnd,
                "window_title": window_title,
                "monitor_rect": [mon.left, mon.top, mon.width, mon.height],
                "max_depth": int(max_depth),
                "max_items": int(max_items),
                "interactive_only": bool(interactive_only),
            },
            timeout_s=timeout_s,
        )
        root_name = str(resp.get("root_name") or "")
        boxes: list[ElementBox] = []
        for raw in resp.get("elements") or []:
            bbox_px = tuple(int(v) for v in raw["bbox_px"])
            center_px = tuple(int(v) for v in raw["center_px"])
            bbox_norm = tuple(int(v) for v in raw["bbox_norm"])
            center_norm = tuple(int(v) for v in raw["center_norm"])
            boxes.append(
                ElementBox(
                    element_id=int(raw["element_id"]),
                    name=str(raw.get("name") or ""),
                    control_type=str(raw.get("control_type") or ""),
                    automation_id=str(raw.get("automation_id") or ""),
                    bbox_px=(bbox_px[0], bbox_px[1], bbox_px[2], bbox_px[3]),
                    center_px=(center_px[0], center_px[1]),
                    bbox_norm=(bbox_norm[0], bbox_norm[1], bbox_norm[2], bbox_norm[3]),
                    center_norm=(center_norm[0], center_norm[1]),
                    is_password=bool(raw.get("is_password", False)),
                    monitor_index=mon.index,
                    window_title=root_name,
                )
            )
        return root_name, boxes

    def check_focused_password(self, timeout_s: float = 1.0) -> bool | None:
        """Fail-closed tri-state check for password focus (True, False, or None if unavailable)."""
        try:
            resp = self.request({"cmd": "check_focused_password"}, timeout_s=timeout_s)
            if not resp.get("ok"):
                return None
            return bool(resp.get("is_password", False))
        except Exception:
            return None

    def await_focused_control(
        self,
        timeout_s: float = FOCUS_WAIT_MAX_S,
        poll_s: float = FOCUS_POLL_S,
    ) -> dict | None:
        """Bounded UIA poll returning focused control info (or None if UIA is unavailable)."""
        deadline = time.monotonic() + max(float(timeout_s), 0.05)
        last_valid: dict | None = None
        while True:
            try:
                remaining = deadline - time.monotonic()
                req_timeout = min(0.6, max(remaining, 0.2))
                resp = self.request({"cmd": "check_focused_password"}, timeout_s=req_timeout)
                if resp.get("ok"):
                    last_valid = resp
                    ctype = str(resp.get("control_type") or "")
                    if resp.get("is_editable") or (
                        resp.get("has_focus") and ctype not in ("", "None")
                    ):
                        return resp
            except Exception:
                pass
            if time.monotonic() >= deadline:
                return last_valid
            abortable_sleep(min(float(poll_s), max(0.01, deadline - time.monotonic())))

    def update_som_registry(self, elements: list[ElementBox]) -> None:
        with self._som_lock:
            self._som_registry = {el.element_id: el for el in elements}

    def get_som_element(self, element_id: int) -> ElementBox:
        with self._som_lock:
            el = self._som_registry.get(int(element_id))
        if el is None:
            raise ElementNotFoundError(int(element_id))
        return el

    def clear_som_registry(self) -> None:
        with self._som_lock:
            self._som_registry.clear()


_CLIENT_SINGLETON: UIASubprocessClient | None = None
_SINGLETON_LOCK = threading.Lock()


def get_uia_client() -> UIASubprocessClient:
    """Return the process-wide lazy singleton UIASubprocessClient."""
    global _CLIENT_SINGLETON
    with _SINGLETON_LOCK:
        if _CLIENT_SINGLETON is None:
            client = UIASubprocessClient()
            atexit.register(client.close)
            _CLIENT_SINGLETON = client
        return _CLIENT_SINGLETON
