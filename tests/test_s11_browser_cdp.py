"""Tests for Tahap S11 Native CDP Browser Fast-Lane & Office Isolation Guarantees."""

from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from mcp.types import ImageContent, TextContent
from PIL import Image

from arisu_cua import config, server
from arisu_cua.browser.cdp_client import CdpBrowserClient, CdpTabInfo
from arisu_cua.display.monitors import MonitorInfo


class _FakeHud:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.approval_requests: list[dict] = []

    def send_event(self, event_type: str, **kwargs):
        self.events.append((event_type, kwargs))
        return {"ok": True}

    def get_hud_status(self):
        return {"running": True, "hud_rect": None}

    def consume_approval(self, **_kw):
        return {"ok": False, "approved": False}

    def request_approval(self, **kwargs):
        self.approval_requests.append(kwargs)
        return {"ok": True, "request_id": "req_browser"}


class _FakeCdp:
    def __init__(
        self,
        *,
        is_password: bool = False,
        element_text: str = "Selanjutnya",
        tab_title: str = "Digitalent LMS",
        tab_url: str = "https://lms.sdmdigital.id/course/view.php",
    ) -> None:
        self.is_password = is_password
        self.element_text = element_text
        self.tab = CdpTabInfo(
            target_id="tab_1",
            title=tab_title,
            url=tab_url,
            ws_url="ws://127.0.0.1:9222/devtools/page/tab_1",
        )
        self.discovered_port: int | None = 9222
        self.launched_calls: list[dict] = []
        self.nav_calls: list[str] = []
        self.click_calls: list[dict] = []
        self.fill_calls: list[dict] = []

    def discover_port(self, **_kw) -> int | None:
        return self.discovered_port

    def launch_or_connect(self, **kw) -> dict:
        self.launched_calls.append(kw)
        self.discovered_port = 9223
        return {
            "port": 9223,
            "pid": 4321,
            "profile": kw.get("profile", "default"),
            "profile_dir": (
                "C:\\Users\\Test\\AppData\\Local\\arisu-computer-use\\chrome_profiles\\default"
            ),
            "reused_existing": False,
        }

    def list_tabs(self, _port: int) -> list[CdpTabInfo]:
        return [self.tab]

    def resolve_tab(self, _port: int, _match: str | None = None) -> CdpTabInfo:
        return self.tab

    def navigate(self, _ws_url: str, url: str) -> dict:
        self.nav_calls.append(url)
        return {"ready_state": "complete", "url": url, "title": self.tab.title}

    def snapshot(self, _ws_url: str) -> dict:
        return {
            "title": self.tab.title,
            "url": self.tab.url,
            "ready_state": "complete",
            "scroll_y": 0,
            "text_excerpt": "Soal Kuis 1: Pilih jawaban paling tepat.",
            "element_count": 2,
            "elements": [
                {
                    "ref_id": 1,
                    "tag": "button",
                    "type": "button",
                    "role": "button",
                    "text": self.element_text,
                    "label": "",
                    "selector": "#next-btn",
                    "is_password": False,
                    "href": "",
                    "value": "",
                    "disabled": False,
                },
                {
                    "ref_id": 2,
                    "tag": "input",
                    "type": "password" if self.is_password else "text",
                    "role": "",
                    "text": "",
                    "label": "Password" if self.is_password else "Nama",
                    "selector": "#input-main",
                    "is_password": self.is_password,
                    "href": "",
                    "value": "",
                    "disabled": False,
                },
            ],
        }

    def inspect_element_security(self, _ws_url: str, **_kw) -> dict:
        return {
            "found": True,
            "tag": "button",
            "type": "password" if self.is_password else "text",
            "text": self.element_text,
            "label": "",
            "is_password": self.is_password,
        }

    def click_element(self, _ws_url: str, **kw) -> dict:
        self.click_calls.append(kw)
        return {"clicked": True, "tag": "button", "text": self.element_text}

    def fill_element(self, _ws_url: str, value: str, **kw) -> dict:
        self.fill_calls.append({"value": value, **kw})
        return {"filled": True, "tag": "input", "chars": len(value), "submitted": True}

    def eval_js(self, _ws_url: str, expression: str, **_kw):
        return {"expr_ok": True, "len": len(expression)}

    def capture_tab_webp(self, _ws_url: str, monitor_index: int = 2) -> tuple[dict, str]:
        buf = BytesIO()
        Image.new("RGB", (640, 360), "#1C3F7A").save(buf, format="WEBP", quality=72)
        raw = buf.getvalue()
        meta = {
            "monitor": monitor_index,
            "format": "webp",
            "mime_type": "image/webp",
            "width": 640,
            "height": 360,
            "raw_bytes": len(raw),
        }
        return meta, base64.b64encode(raw).decode("ascii")


def _patch_browser_env(monkeypatch, fake_cdp: _FakeCdp):
    mon = MonitorInfo(
        index=2,
        device=r"\\.\DISPLAY1",
        left=0,
        top=0,
        width=1600,
        height=900,
        dpi_x=96,
        dpi_y=96,
        scale_pct=100,
        is_primary=True,
    )
    fake_hud = _FakeHud()
    audit_logs: list[dict] = []
    server._BREAKER.reset()
    monkeypatch.setattr(server._CORNER_FAILSAFE, "start", lambda: None)
    monkeypatch.setattr(server, "enumerate_monitors", lambda: [mon])
    monkeypatch.setattr(server, "get_cdp_client", lambda: fake_cdp)
    monkeypatch.setattr(server, "get_hud_client", lambda: fake_hud)
    monkeypatch.setattr(server, "_AUDIT", SimpleNamespace(log=lambda **kw: audit_logs.append(kw)))
    monkeypatch.setattr(server, "abortable_sleep", lambda _s: None)
    return fake_hud, audit_logs


def test_isolated_profile_dir_never_touches_user_default_chrome_profile(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalApp"))
    default_prof = config.get_cdp_profile_dir("default")
    assert "arisu-computer-use" in str(default_prof)
    assert "chrome_profiles" in str(default_prof)
    assert "Google\\Chrome\\User Data" not in str(default_prof)

    # Named profile falls back cleanly inside isolated app dir if .agent-browser profile absent
    custom_prof = config.get_cdp_profile_dir("clean_test_profile/../evil")
    assert "arisu-computer-use" in str(custom_prof)
    assert ".." not in str(custom_prof)


def test_cdp_port_discovery_and_launch_reuses_existing_endpoint(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    state_file = config.get_cdp_state_path()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"port": 9231, "profile": "default"}), encoding="utf-8")

    client = CdpBrowserClient()
    monkeypatch.setattr(
        client,
        "probe_port",
        lambda p: {"Browser": "Chrome/130"} if p == 9231 else None,
    )
    assert client.discover_port("default") == 9231

    launch_res = client.launch_or_connect(profile="default")
    assert launch_res["port"] == 9231
    assert launch_res["reused_existing"] is True


def test_browser_action_navigate_and_snapshot_returns_dom_elements(monkeypatch) -> None:
    fake_cdp = _FakeCdp()
    _patch_browser_env(monkeypatch, fake_cdp)

    res = server.arisu_cu_browser_action(
        action="navigate",
        url="https://lms.sdmdigital.id/course/view.php",
    )
    assert len(res) == 1 and isinstance(res[0], TextContent)
    data = json.loads(res[0].text)
    assert data["ok"] is True
    assert data["navigation"]["ready_state"] == "complete"
    assert data["snapshot"]["element_count"] == 2
    assert fake_cdp.nav_calls == ["https://lms.sdmdigital.id/course/view.php"]


def test_browser_action_click_and_fill_without_moving_physical_mouse(monkeypatch) -> None:
    fake_cdp = _FakeCdp()
    _patch_browser_env(monkeypatch, fake_cdp)
    monkeypatch.setattr(
        server,
        "execute_mouse_action",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("Win32 mouse must not move!")),
    )

    click_out = json.loads(server.arisu_cu_browser_action(action="click", ref_id=1)[0].text)
    assert click_out["ok"] is True
    assert click_out["executed"] is True
    assert fake_cdp.click_calls[0]["ref_id"] == 1

    fill_out = json.loads(
        server.arisu_cu_browser_action(
            action="fill",
            selector="#input-main",
            value="Abyan",
            press_enter=True,
        )[0].text
    )
    assert fill_out["ok"] is True
    assert fill_out["executed"] is True
    assert fake_cdp.fill_calls[0]["value"] == "Abyan"
    assert fake_cdp.fill_calls[0]["press_enter"] is True


def test_browser_action_blocks_denylisted_urls_and_sensitive_files(monkeypatch) -> None:
    fake_cdp = _FakeCdp()
    _patch_browser_env(monkeypatch, fake_cdp)

    for bad_url in (
        "file:///C:/Users/user/project/.env",
        "https://vault.1password.com/signin",
        "https://ibank.klikbca.com/",
        "https://livin.bankmandiri.co.id/",
    ):
        err = json.loads(server.arisu_cu_browser_action(action="navigate", url=bad_url)[0].text)
        assert err["ok"] is False
        assert err["code"] == "WINDOW_BLOCKED_BY_POLICY"
    assert fake_cdp.nav_calls == []


def test_browser_action_fill_password_requires_hud_approval_and_redacts_audit(
    monkeypatch,
) -> None:
    fake_cdp = _FakeCdp(is_password=True, element_text="")
    fake_hud, audit_logs = _patch_browser_env(monkeypatch, fake_cdp)

    err = json.loads(
        server.arisu_cu_browser_action(
            action="fill",
            selector="#password",
            value="TopSecretOfficePass",
        )[0].text
    )
    assert err["ok"] is False
    assert err["code"] == "GUARDRAIL_APPROVAL_REQUIRED"
    assert fake_cdp.fill_calls == []
    assert len(fake_hud.approval_requests) == 1
    assert audit_logs[-1]["redact_text"] is True
    assert audit_logs[-1]["args"]["value"] == "[REDACTED]"


def test_browser_action_click_sensitive_button_requires_hud_approval(monkeypatch) -> None:
    fake_cdp = _FakeCdp(is_password=False, element_text="Transfer & Pay Now")
    fake_hud, _audits = _patch_browser_env(monkeypatch, fake_cdp)

    err = json.loads(server.arisu_cu_browser_action(action="click", ref_id=1)[0].text)
    assert err["ok"] is False
    assert err["code"] == "GUARDRAIL_APPROVAL_REQUIRED"
    assert fake_cdp.click_calls == []
    assert len(fake_hud.approval_requests) == 1


def test_browser_action_screenshot_returns_webp_image_content(monkeypatch) -> None:
    fake_cdp = _FakeCdp()
    _patch_browser_env(monkeypatch, fake_cdp)

    items = server.arisu_cu_browser_action(action="screenshot", monitor="2")
    assert len(items) == 2
    assert isinstance(items[0], TextContent)
    assert isinstance(items[1], ImageContent)
    assert items[1].mimeType == "image/webp"
    meta = json.loads(items[0].text)
    assert meta["ok"] is True
    assert meta["format"] == "webp"
    assert "vision_notice" in meta
