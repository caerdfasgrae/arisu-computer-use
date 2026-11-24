"""Native Python Chrome DevTools Protocol (CDP) client on interactive Default desktop."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image
from websockets.sync.client import connect as ws_connect

from arisu_cua.config import (
    CDP_DEFAULT_PORT,
    CDP_SNAPSHOT_ELEMENTS_LIMIT,
    CDP_SNAPSHOT_TEXT_LIMIT,
    get_cdp_profile_dir,
    get_cdp_state_path,
)
from arisu_cua.display.capture import prepare_captured_image
from arisu_cua.safety.killswitch import (
    BrowserConnectionError,
    abortable_sleep,
    check_ready,
)

_BROWSER_EXE_CANDIDATES: tuple[Path, ...] = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)

_LOCATE_ELEMENT_JS = r"""
function __arisuLocate(refId, selector, matchText) {
  function isVisible(el) {
    if (!el || el.nodeType !== 1) return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }
  function labelFor(el) {
    let lbl = '';
    if (el.id) {
      try {
        const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (l) lbl = (l.innerText || l.textContent || '').trim();
      } catch (e) {}
    }
    if (!lbl) {
      const parentLabel = el.closest ? el.closest('label') : null;
      if (parentLabel) lbl = (parentLabel.innerText || parentLabel.textContent || '').trim();
    }
    return lbl || (el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim();
  }
  let el = null;
  if (refId !== null && refId !== undefined) {
    el = document.querySelector('[data-arisu-ref="' + String(refId) + '"]');
  }
  if (!el && selector) {
    try { el = document.querySelector(selector); } catch (e) { el = null; }
  }
  if (!el && matchText) {
    const q = String(matchText).trim().toLowerCase();
    const candidates = Array.from(document.querySelectorAll(
      'button, a, input, textarea, select, [role="button"], [role="link"], ' +
      '[role="tab"], [role="menuitem"], label'
    )).filter(isVisible);
    let partial = null;
    for (const c of candidates) {
      const txt = (
        c.innerText || c.textContent || c.value || labelFor(c) || c.getAttribute('title') || ''
      ).trim().toLowerCase();
      if (!txt) continue;
      if (txt === q) { el = c; break; }
      if (!partial && txt.includes(q)) partial = c;
    }
    if (!el) el = partial;
    if (el && el.tagName === 'LABEL') {
      if (el.htmlFor) {
        const target = document.getElementById(el.htmlFor);
        if (target) el = target;
      } else {
        const nested = el.querySelector('input, textarea, select');
        if (nested) el = nested;
      }
    }
  }
  return { el: el, labelText: el ? labelFor(el) : '' };
}
"""


@dataclass(frozen=True)
class CdpTabInfo:
    target_id: str
    title: str
    url: str
    ws_url: str

    def to_dict(self) -> dict[str, str]:
        return {
            "target_id": self.target_id,
            "title": self.title,
            "url": self.url,
        }


def find_system_browser_exe() -> Path | None:
    """Locate official Chrome or Edge executable installed on the Windows host."""
    for candidate in _BROWSER_EXE_CANDIDATES:
        if str(candidate) and candidate.is_file():
            return candidate
    return None


def _find_free_port(preferred: int = CDP_DEFAULT_PORT) -> int:
    for candidate in range(preferred, preferred + 16):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_json(url: str, *, method: str = "GET", timeout_s: float = 2.5) -> Any:
    if not url.startswith("http://127.0.0.1:"):
        raise BrowserConnectionError(f"Non-loopback CDP endpoint rejected: {url}")
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8", errors="replace").strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}


class CdpBrowserClient:
    """Thread-safe loopback CDP client operating on isolated Chrome profiles."""

    def __init__(self) -> None:
        self._msg_seq = 0
        self._active_target_by_port: dict[int, str] = {}

    def probe_port(self, port: int) -> dict[str, Any] | None:
        if port <= 0 or port > 65535:
            return None
        try:
            data = _http_json(f"http://127.0.0.1:{port}/json/version", timeout_s=1.2)
            if isinstance(data, dict) and ("webSocketDebuggerUrl" in data or "Browser" in data):
                return data
        except Exception:
            return None
        return None

    def discover_port(self, profile: str = "default", port: int | None = None) -> int | None:
        if port is not None and self.probe_port(port):
            return int(port)

        state_path = get_cdp_state_path()
        if state_path.is_file():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                saved_port = int(state.get("port") or 0)
                saved_profile = str(state.get("profile") or "default")
                if (
                    saved_port
                    and (saved_profile == profile or profile == "default")
                    and self.probe_port(saved_port)
                ):
                    return saved_port
            except Exception:
                pass

        active_port_file = get_cdp_profile_dir(profile) / "DevToolsActivePort"
        if active_port_file.is_file():
            try:
                first_line = active_port_file.read_text(encoding="utf-8").splitlines()[0].strip()
                parsed_port = int(first_line)
                if self.probe_port(parsed_port):
                    return parsed_port
            except Exception:
                pass

        if profile == "default" and self.probe_port(CDP_DEFAULT_PORT):
            return CDP_DEFAULT_PORT
        return None

    def launch_or_connect(
        self,
        *,
        profile: str = "default",
        url: str | None = None,
        monitor_rect: tuple[int, int, int, int] | None = None,
        port: int | None = None,
    ) -> dict[str, Any]:
        check_ready()
        profile_dir = get_cdp_profile_dir(profile)
        existing_port = self.discover_port(profile=profile, port=port)
        if existing_port is not None:
            if url:
                tab = self.resolve_tab(existing_port)
                self.navigate(tab.ws_url, url)
            return {
                "port": existing_port,
                "profile": profile,
                "profile_dir": str(profile_dir),
                "reused_existing": True,
            }

        exe = find_system_browser_exe()
        if exe is None:
            raise BrowserConnectionError(
                "No signed Google Chrome or Microsoft Edge binary found on host"
            )

        profile_dir.mkdir(parents=True, exist_ok=True)
        chosen_port = int(port) if port is not None else _find_free_port(CDP_DEFAULT_PORT)
        args = [
            str(exe),
            f"--remote-debugging-port={chosen_port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--new-window",
        ]
        if monitor_rect is not None:
            left, top, width, height = monitor_rect
            win_w = max(800, min(int(width) - 60, 1480))
            win_h = max(600, min(int(height) - 60, 860))
            args.extend(
                [
                    f"--window-position={int(left) + 20},{int(top) + 20}",
                    f"--window-size={win_w},{win_h}",
                ]
            )
        args.append(url or "about:blank")

        proc = subprocess.Popen(
            args,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.monotonic() + 6.0
        ready = False
        while time.monotonic() < deadline:
            if self.probe_port(chosen_port) is not None:
                ready = True
                break
            abortable_sleep(0.15)
        if not ready:
            raise BrowserConnectionError(
                f"Chrome/Edge (pid={proc.pid}) did not respond on 127.0.0.1:{chosen_port}"
            )

        state_path = get_cdp_state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "port": chosen_port,
                    "pid": proc.pid,
                    "profile": profile,
                    "profile_dir": str(profile_dir),
                }
            ),
            encoding="utf-8",
        )
        return {
            "port": chosen_port,
            "pid": proc.pid,
            "profile": profile,
            "profile_dir": str(profile_dir),
            "reused_existing": False,
        }

    def list_tabs(self, port: int) -> list[CdpTabInfo]:
        raw = _http_json(f"http://127.0.0.1:{port}/json/list", timeout_s=2.5)
        if not isinstance(raw, list):
            return []
        tabs: list[CdpTabInfo] = []
        for item in raw:
            if not isinstance(item, dict) or item.get("type") != "page":
                continue
            ws_url = str(item.get("webSocketDebuggerUrl") or "").strip()
            tab_url = str(item.get("url") or "")
            if not ws_url or tab_url.startswith("devtools://"):
                continue
            tabs.append(
                CdpTabInfo(
                    target_id=str(item.get("id") or ""),
                    title=str(item.get("title") or ""),
                    url=tab_url,
                    ws_url=ws_url,
                )
            )
        return tabs

    def resolve_tab(self, port: int, target_match: str | None = None) -> CdpTabInfo:
        tabs = self.list_tabs(port)
        if target_match:
            needle = target_match.strip().lower()
            for tab in tabs:
                if (
                    needle in tab.url.lower()
                    or needle in tab.title.lower()
                    or needle in tab.target_id.lower()
                ):
                    self._active_target_by_port[port] = tab.target_id
                    return tab
            raise BrowserConnectionError(f"No browser tab matches '{target_match}'")

        pinned_id = self._active_target_by_port.get(port)
        if pinned_id:
            for tab in tabs:
                if tab.target_id == pinned_id:
                    return tab

        if tabs:
            self._active_target_by_port[port] = tabs[0].target_id
            return tabs[0]

        created: Any = None
        for method in ("PUT", "GET"):
            try:
                created = _http_json(
                    f"http://127.0.0.1:{port}/json/new?about:blank",
                    method=method,
                    timeout_s=2.5,
                )
                break
            except urllib.error.HTTPError:
                continue
        if isinstance(created, dict) and created.get("webSocketDebuggerUrl"):
            tab = CdpTabInfo(
                target_id=str(created.get("id") or ""),
                title=str(created.get("title") or ""),
                url=str(created.get("url") or "about:blank"),
                ws_url=str(created.get("webSocketDebuggerUrl") or ""),
            )
            self._active_target_by_port[port] = tab.target_id
            return tab
        raise BrowserConnectionError(f"No usable page tab on CDP port {port}")

    def close_tab(self, port: int, target_id: str) -> bool:
        try:
            _http_json(f"http://127.0.0.1:{port}/json/close/{target_id}", timeout_s=2.5)
            if self._active_target_by_port.get(port) == target_id:
                self._active_target_by_port.pop(port, None)
            return True
        except Exception as exc:
            raise BrowserConnectionError(f"Failed to close tab {target_id}: {exc}") from exc

    def send_cdp(
        self,
        ws_url: str,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_s: float = 15.0,
    ) -> dict[str, Any]:
        check_ready()
        self._msg_seq += 1
        msg_id = self._msg_seq
        payload = json.dumps({"id": msg_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout_s
        try:
            with ws_connect(
                ws_url,
                open_timeout=5.0,
                close_timeout=2.0,
                max_size=16 * 1024 * 1024,
            ) as ws:
                ws.send(payload)
                while time.monotonic() < deadline:
                    check_ready()
                    try:
                        raw = ws.recv(timeout=0.25)
                    except TimeoutError:
                        continue
                    data = json.loads(raw)
                    if data.get("id") == msg_id:
                        if "error" in data and data["error"]:
                            raise BrowserConnectionError(f"CDP {method} error: {data['error']}")
                        result = data.get("result")
                        return result if isinstance(result, dict) else {}
        except BrowserConnectionError:
            raise
        except Exception as exc:
            raise BrowserConnectionError(f"CDP {method} failed: {exc}") from exc
        raise BrowserConnectionError(f"CDP {method} timed out after {timeout_s:.1f}s")

    def eval_js(
        self,
        ws_url: str,
        expression: str,
        *,
        await_promise: bool = True,
        timeout_s: float = 15.0,
    ) -> Any:
        res = self.send_cdp(
            ws_url,
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
            timeout_s=timeout_s,
        )
        exc_details = res.get("exceptionDetails")
        if exc_details:
            err_msg = str(
                exc_details.get("exception", {}).get("description")
                or exc_details.get("text")
                or "JavaScript error"
            )
            raise BrowserConnectionError(f"JS exception: {err_msg}")
        return res.get("result", {}).get("value")

    def navigate(self, ws_url: str, url: str, timeout_s: float = 20.0) -> dict[str, Any]:
        check_ready()
        self.send_cdp(ws_url, "Page.navigate", {"url": url}, timeout_s=timeout_s)
        deadline = time.monotonic() + timeout_s
        last_info: dict[str, Any] = {"ready_state": "loading", "url": url, "title": ""}
        while time.monotonic() < deadline:
            abortable_sleep(0.2)
            with contextlib.suppress(Exception):
                raw = self.eval_js(
                    ws_url,
                    "JSON.stringify({ready_state: document.readyState, "
                    "url: location.href, title: document.title})",
                    await_promise=False,
                    timeout_s=3.0,
                )
                if isinstance(raw, str):
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        last_info = parsed
                        if parsed.get("ready_state") == "complete":
                            return last_info
        return last_info

    def snapshot(
        self,
        ws_url: str,
        *,
        text_limit: int = CDP_SNAPSHOT_TEXT_LIMIT,
        elements_limit: int = CDP_SNAPSHOT_ELEMENTS_LIMIT,
    ) -> dict[str, Any]:
        js = f"""
(() => {{
  function isVis(el) {{
    if (!el || el.nodeType !== 1) return false;
    const st = window.getComputedStyle(el);
    if (!st || st.display === 'none' || st.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }}
  function cssPath(el) {{
    if (el.id) {{
      try {{ return '#' + CSS.escape(el.id); }} catch (e) {{}}
    }}
    const name = el.getAttribute('name');
    if (name) {{
      const tag = el.tagName.toLowerCase();
      return tag + '[name=' + JSON.stringify(name) + ']';
    }}
    return el.tagName.toLowerCase();
  }}
  function getLabel(el) {{
    let l = '';
    if (el.id) {{
      try {{
        const node = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (node) l = (node.innerText || node.textContent || '').trim();
      }} catch (e) {{}}
    }}
    return l || (el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim();
  }}
  const prev = document.querySelectorAll('[data-arisu-ref]');
  prev.forEach((n) => n.removeAttribute('data-arisu-ref'));
  const rawEls = Array.from(document.querySelectorAll(
    'a[href], button, input, textarea, select, [role="button"], ' +
    '[role="link"], [role="tab"], [role="checkbox"], [role="radio"], [onclick]'
  )).filter(isVis).slice(0, {int(elements_limit)});
  const elements = [];
  rawEls.forEach((el, idx) => {{
    const refId = idx + 1;
    el.setAttribute('data-arisu-ref', String(refId));
    const tag = el.tagName.toLowerCase();
    const tp = (el.getAttribute('type') || el.type || '').toLowerCase();
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
    const label = getLabel(el);
    const txt = (el.innerText || el.textContent || el.getAttribute('title') || '').trim();
    const hint = [el.name || '', el.id || '', label].join(' ').toLowerCase();
    const isPw = tp === 'password' || ac.includes('password') ||
      /\\b(password|kata\\s*sandi|passcode|pin)\\b/i.test(hint);
    elements.push({{
      ref_id: refId,
      tag: tag,
      type: tp,
      role: (el.getAttribute('role') || '').toLowerCase(),
      text: txt.slice(0, 100),
      label: label.slice(0, 80),
      selector: cssPath(el),
      is_password: isPw,
      href: tag === 'a' ? (el.getAttribute('href') || '').slice(0, 140) : '',
      value: isPw ? '[REDACTED]' : String(el.value || '').slice(0, 80),
      disabled: !!el.disabled
    }});
  }});
  const bodyText = (document.body ? (document.body.innerText || '') : '').replace(
    /\\n{{3,}}/g, '\\n\\n'
  ).trim().slice(0, {int(text_limit)});
  return JSON.stringify({{
    title: document.title || '',
    url: location.href || '',
    ready_state: document.readyState || '',
    scroll_y: Math.round(window.scrollY || 0),
    text_excerpt: bodyText,
    element_count: elements.length,
    elements: elements
  }});
}})()
"""
        raw = self.eval_js(ws_url, js, await_promise=False)
        if isinstance(raw, str):
            return dict(json.loads(raw))
        return {"title": "", "url": "", "elements": [], "element_count": 0}

    def inspect_element_security(
        self,
        ws_url: str,
        *,
        ref_id: int | None = None,
        selector: str | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        js = f"""
(() => {{
  {_LOCATE_ELEMENT_JS}
  const found = __arisuLocate(
    {json.dumps(ref_id)}, {json.dumps(selector)}, {json.dumps(text)}
  );
  const el = found.el;
  if (!el) return JSON.stringify({{ found: false, is_password: null }});
  const tag = el.tagName.toLowerCase();
  const tp = (el.getAttribute('type') || el.type || '').toLowerCase();
  const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
  const label = found.labelText || '';
  const visible = (el.innerText || el.textContent || el.value || '').trim();
  const hint = [el.name || '', el.id || '', label].join(' ').toLowerCase();
  const isPw = tp === 'password' || ac.includes('password') ||
    /\\b(password|kata\\s*sandi|passcode|pin)\\b/i.test(hint);
  return JSON.stringify({{
    found: true,
    tag: tag,
    type: tp,
    text: visible.slice(0, 120),
    label: label.slice(0, 120),
    is_password: isPw
  }});
}})()
"""
        raw = self.eval_js(ws_url, js, await_promise=False)
        if isinstance(raw, str):
            return dict(json.loads(raw))
        return {"found": False, "is_password": None}

    def click_element(
        self,
        ws_url: str,
        *,
        ref_id: int | None = None,
        selector: str | None = None,
        text: str | None = None,
    ) -> dict[str, Any]:
        js = f"""
(() => {{
  {_LOCATE_ELEMENT_JS}
  const found = __arisuLocate(
    {json.dumps(ref_id)}, {json.dumps(selector)}, {json.dumps(text)}
  );
  const el = found.el;
  if (!el) return JSON.stringify({{ clicked: false }});
  try {{ el.scrollIntoView({{ block: 'center', inline: 'center' }}); }} catch (e) {{}}
  try {{ el.focus(); }} catch (e) {{}}
  el.click();
  return JSON.stringify({{
    clicked: true,
    tag: el.tagName.toLowerCase(),
    text: (el.innerText || el.textContent || el.value || '').trim().slice(0, 100),
    url: location.href,
    title: document.title
  }});
}})()
"""
        raw = self.eval_js(ws_url, js, await_promise=False)
        if isinstance(raw, str):
            return dict(json.loads(raw))
        return {"clicked": False}

    def fill_element(
        self,
        ws_url: str,
        value: str,
        *,
        ref_id: int | None = None,
        selector: str | None = None,
        text: str | None = None,
        press_enter: bool = False,
    ) -> dict[str, Any]:
        js = f"""
(() => {{
  {_LOCATE_ELEMENT_JS}
  const found = __arisuLocate(
    {json.dumps(ref_id)}, {json.dumps(selector)}, {json.dumps(text)}
  );
  const el = found.el;
  if (!el) return JSON.stringify({{ filled: false }});
  try {{ el.scrollIntoView({{ block: 'center', inline: 'center' }}); }} catch (e) {{}}
  try {{ el.focus(); }} catch (e) {{}}
  const val = {json.dumps(value)};
  const tag = el.tagName.toLowerCase();
  if (tag === 'select') {{
    const opts = Array.from(el.options || []);
    const hit = opts.find(
      (o) => o.value === val || (o.text || '').trim().toLowerCase() === val.toLowerCase()
    );
    el.value = hit ? hit.value : val;
  }} else if (tag === 'textarea') {{
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
    if (setter) setter.call(el, val); else el.value = val;
  }} else if (tag === 'input') {{
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
    if (setter) setter.call(el, val); else el.value = val;
  }} else if (el.isContentEditable) {{
    el.innerText = val;
  }} else {{
    el.value = val;
  }}
  el.dispatchEvent(new Event('input', {{ bubbles: true }}));
  el.dispatchEvent(new Event('change', {{ bubbles: true }}));
  let submitted = false;
  if ({json.dumps(bool(press_enter))}) {{
    const evInit = {{ key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }};
    el.dispatchEvent(new KeyboardEvent('keydown', evInit));
    el.dispatchEvent(new KeyboardEvent('keyup', evInit));
    if (el.form && typeof el.form.requestSubmit === 'function') {{
      try {{ el.form.requestSubmit(); submitted = true; }} catch (e) {{}}
    }} else if (el.form && typeof el.form.submit === 'function') {{
      try {{ el.form.submit(); submitted = true; }} catch (e) {{}}
    }}
  }}
  return JSON.stringify({{
    filled: true,
    tag: tag,
    chars: val.length,
    submitted: submitted,
    url: location.href,
    title: document.title
  }});
}})()
"""
        raw = self.eval_js(ws_url, js, await_promise=False)
        if isinstance(raw, str):
            return dict(json.loads(raw))
        return {"filled": False}

    def capture_tab_webp(self, ws_url: str, monitor_index: int = 2) -> tuple[dict[str, Any], str]:
        res = self.send_cdp(ws_url, "Page.captureScreenshot", {"format": "png"})
        b64_png = res.get("data")
        if not b64_png:
            raise BrowserConnectionError("CDP Page.captureScreenshot returned empty image data")
        img = Image.open(BytesIO(base64.b64decode(b64_png)))
        shot = prepare_captured_image(img, monitor_index=monitor_index, fmt="webp", mode="clean")
        return shot.to_metadata(), base64.b64encode(shot.data).decode("ascii")


_CDP_CLIENT_SINGLETON: CdpBrowserClient | None = None


def get_cdp_client() -> CdpBrowserClient:
    global _CDP_CLIENT_SINGLETON
    if _CDP_CLIENT_SINGLETON is None:
        _CDP_CLIENT_SINGLETON = CdpBrowserClient()
    return _CDP_CLIENT_SINGLETON
