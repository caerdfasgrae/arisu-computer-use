"""Unit & integration tests for Tahap S10 Auto-Clipboard Paste and PopupHost equivalence."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from mcp.types import ImageContent, TextContent

from arisu_cua import server
from arisu_cua.display.monitors import MonitorInfo
from arisu_cua.input import win32_input
from arisu_cua.input.window_mgr import verify_expected_window
from arisu_cua.safety.killswitch import WindowBlockedByPolicyError, WindowMismatchError


def test_long_text_auto_upgrades_to_clipboard_paste_and_restores_clipboard(monkeypatch) -> None:
    clipboard_state = {"text": "user-previous-clip"}
    set_history: list[str] = []
    sent_vks: list[tuple[int, bool]] = []
    sleeps: list[float] = []

    monkeypatch.setattr(win32_input, "_get_clipboard_unicode_text", lambda: clipboard_state["text"])

    def fake_set_clip(val: str) -> bool:
        set_history.append(val)
        clipboard_state["text"] = val
        return True

    def fake_send(inputs):
        for inp in inputs:
            ki = inp.union.ki
            is_up = bool(ki.dwFlags & win32_input.KEYEVENTF_KEYUP)
            sent_vks.append((int(ki.wVk), is_up))
        return len(inputs)

    monkeypatch.setattr(win32_input, "_set_clipboard_unicode_text", fake_set_clip)
    monkeypatch.setattr(win32_input, "_send_inputs", fake_send)
    monkeypatch.setattr(win32_input, "abortable_sleep", lambda s: sleeps.append(float(s)))

    payload = "Paragraf 1.\n" + ("A" * 150)
    res = win32_input.execute_keyboard_action("type", text=payload, typing_mode="natural")

    assert res["delivery"] == "clipboard_paste"
    assert res["chars"] == len(payload)
    assert set_history == [payload, "user-previous-clip"]
    assert clipboard_state["text"] == "user-previous-clip"
    assert sent_vks == [
        (win32_input.VK_MAP["ctrl"], False),
        (ord("V"), False),
        (ord("V"), True),
        (win32_input.VK_MAP["ctrl"], True),
    ]
    assert sleeps == [0.01, 0.12]


def test_short_text_uses_sendinput_character_loop(monkeypatch) -> None:
    paste_called = False
    sent_calls = 0

    def fake_paste(_text: str) -> bool:
        nonlocal paste_called
        paste_called = True
        return True

    def fake_send(inputs):
        nonlocal sent_calls
        sent_calls += len(inputs)
        return len(inputs)

    monkeypatch.setattr(win32_input, "_paste_text_via_clipboard", fake_paste)
    monkeypatch.setattr(win32_input, "_send_inputs", fake_send)
    monkeypatch.setattr(win32_input, "abortable_sleep", lambda _s: None)

    res = win32_input.execute_keyboard_action("type", text="Short text", typing_mode="natural")
    assert res["delivery"] == "sendinput"
    assert res["chars"] == 10
    assert paste_called is False
    assert sent_calls == 20


def test_password_field_never_uses_clipboard_paste_even_when_long_or_paste_mode(
    monkeypatch,
) -> None:
    paste_called = False
    sent_calls = 0

    def fake_paste(_text: str) -> bool:
        nonlocal paste_called
        paste_called = True
        return True

    def fake_send(inputs):
        nonlocal sent_calls
        sent_calls += len(inputs)
        return len(inputs)

    monkeypatch.setattr(win32_input, "_paste_text_via_clipboard", fake_paste)
    monkeypatch.setattr(win32_input, "_send_inputs", fake_send)
    monkeypatch.setattr(win32_input, "abortable_sleep", lambda _s: None)

    res = win32_input.execute_keyboard_action(
        "type",
        text="S" * 160,
        typing_mode="paste",
        allow_clipboard=False,
    )
    assert res["delivery"] == "sendinput"
    assert res["chars"] == 160
    assert paste_called is False
    assert sent_calls == 320

    # Also verify server enforces allow_clipboard=False when typing into an approved password field
    kb_kwargs: list[dict] = []
    server._BREAKER.reset()
    monkeypatch.setattr(server._CORNER_FAILSAFE, "start", lambda: None)
    monkeypatch.setattr(server, "get_foreground_window_title", lambda: "Login - Chrome")
    monkeypatch.setattr(server, "get_foreground_window_info", lambda: (101, (0, 0, 1920, 1080)))
    monkeypatch.setattr(server, "_enforce_password_guard", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        server, "get_uia_client", lambda: SimpleNamespace(check_focused_password=lambda: True)
    )
    monkeypatch.setattr(
        server,
        "get_hud_client",
        lambda: SimpleNamespace(send_event=lambda *_a, **_kw: {"ok": True}),
    )
    monkeypatch.setattr(server, "_AUDIT", SimpleNamespace(log=lambda **_kw: None))
    monkeypatch.setattr(
        server,
        "_append_inline_verification",
        lambda res_obj, **_kw: [TextContent(type="text", text=json.dumps(res_obj))],
    )

    def fake_exec_kb(**kw):
        kb_kwargs.append(kw)
        return {"action": "type", "chars": len(kw["text"]), "delivery": "sendinput"}

    monkeypatch.setattr(server, "execute_keyboard_action", fake_exec_kb)

    out = json.loads(
        server.arisu_cu_keyboard_action(
            action="type",
            expected_window="Login",
            text="Secret" * 30,
            approval_token="tok_valid",
        )[0].text
    )
    assert out["ok"] is True
    assert kb_kwargs[0]["allow_clipboard"] is False


def test_clipboard_failure_gracefully_falls_back_to_sendinput(monkeypatch) -> None:
    sent_calls = 0

    def fake_send(inputs):
        nonlocal sent_calls
        sent_calls += len(inputs)
        return len(inputs)

    monkeypatch.setattr(win32_input, "_get_clipboard_unicode_text", lambda: None)
    monkeypatch.setattr(win32_input, "_set_clipboard_unicode_text", lambda _t: False)
    monkeypatch.setattr(win32_input, "_send_inputs", fake_send)
    monkeypatch.setattr(win32_input, "abortable_sleep", lambda _s: None)

    res = win32_input.execute_keyboard_action("type", text="B" * 130, typing_mode="fast")
    assert res["delivery"] == "sendinput"
    assert res["chars"] == 130
    assert sent_calls == 260


def test_verify_expected_window_allows_popuphost_when_foreground_matches_expected(
    monkeypatch,
) -> None:
    verify_expected_window(
        "Notepad",
        "PopupHost",
        required=False,
        fallback_title="*Saya mengenal kamu - Notepad",
    )

    with pytest.raises(WindowMismatchError, match="WINDOW_MISMATCH_BLOCKED"):
        verify_expected_window(
            "Notepad",
            "PopupHost",
            required=False,
            fallback_title="Google Chrome",
        )

    # Full mouse_action integration check: PopupHost allowed under Notepad, blocked under 1Password
    mon = MonitorInfo(
        index=1,
        device=r"\\.\DISPLAY1",
        left=0,
        top=0,
        width=1920,
        height=1080,
        dpi_x=96,
        dpi_y=96,
        scale_pct=100,
        is_primary=True,
    )
    server._BREAKER.reset()
    monkeypatch.setattr(server._CORNER_FAILSAFE, "start", lambda: None)
    monkeypatch.setattr(server, "enumerate_monitors", lambda: [mon])
    monkeypatch.setattr(server, "get_foreground_window_info", lambda: (101, (0, 0, 1920, 1080)))
    monkeypatch.setattr(server, "get_foreground_window_title", lambda: "Untitled - Notepad")
    monkeypatch.setattr(server, "get_cursor_pos", lambda: (100, 100))
    monkeypatch.setattr(server, "get_window_title_at_point", lambda _x, _y: "PopupHost")
    monkeypatch.setattr(server, "verify_pre_click_patch", lambda *_a, **_kw: {"checked": False})
    monkeypatch.setattr(
        server,
        "get_hud_client",
        lambda: SimpleNamespace(
            send_event=lambda *_a, **_kw: {"ok": True},
            send_cursor_cue=lambda *_a, **_kw: {"ok": True},
            get_hud_status=lambda: {"running": False, "hud_rect": None},
        ),
    )
    monkeypatch.setattr(server, "_AUDIT", SimpleNamespace(log=lambda **_kw: None))
    monkeypatch.setattr(
        server,
        "execute_mouse_action",
        lambda **kw: {"action": kw["action"], "px": [kw["px_x"], kw["px_y"]]},
    )
    monkeypatch.setattr(
        server,
        "_capture_payload",
        lambda **_kw: [
            TextContent(type="text", text=json.dumps({"ok": True})),
            ImageContent(type="image", data="AAAA", mimeType="image/webp"),
        ],
    )
    monkeypatch.setattr(server, "abortable_sleep", lambda _s: None)

    ok_res = json.loads(
        server.arisu_cu_mouse_action(action="click", x=260, y=441, expected_window="Notepad")[
            0
        ].text
    )
    assert ok_res["ok"] is True

    # If foreground is denylisted even while cursor points at PopupHost, must be blocked
    monkeypatch.setattr(server, "get_foreground_window_title", lambda: "1Password - Vault")
    with pytest.raises(WindowBlockedByPolicyError):
        server.check_window_denylist(["PopupHost", "1Password - Vault"])
    blocked = json.loads(server.arisu_cu_mouse_action(action="click", x=260, y=441)[0].text)
    assert blocked["ok"] is False
    assert blocked["code"] == "WINDOW_BLOCKED_BY_POLICY"
