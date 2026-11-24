"""Tests for Tahap S4 guardrails: Out-of-Band Approval, Denylist, Breaker, Failsafe & Audit."""

from __future__ import annotations

import json

import pytest

from arisu_cua.display.monitors import VirtualScreenRect
from arisu_cua.safety.audit import AuditLogger
from arisu_cua.safety.guardrails import (
    PendingApprovalStore,
    SmartCircuitBreaker,
    check_window_denylist,
    compute_action_hash,
    is_sensitive_button_name,
)
from arisu_cua.safety.killswitch import (
    ABORT_EVENT,
    ActionCapReachedError,
    CornerFailsafeMonitor,
    InvalidApprovalTokenError,
    RateLimitExceededError,
    RepeatLoopDetectedError,
    WindowBlockedByPolicyError,
    reset_abort,
)


def test_pending_approval_store_hash_ttl_and_single_use() -> None:
    now = [100.0]
    store = PendingApprovalStore(ttl_s=60.0, clock=lambda: now[0])

    h1 = compute_action_hash("arisu_cu_keyboard_action", "Notepad", {"keys": ["ctrl", "w"]})
    h2 = compute_action_hash("arisu_cu_keyboard_action", "Chrome", {"keys": ["ctrl", "w"]})

    # Without HUD approval, verification returns False
    assert store.verify_and_consume(h1) is False

    # Request approval -> approve via physical HUD click
    req = store.create_request(h1, "Hotkey ctrl+w", "Notepad")
    token = store.approve_request(req.request_id)
    assert token.startswith("tok_")

    # Wrong payload hash must be rejected
    with pytest.raises(InvalidApprovalTokenError):
        store.verify_and_consume(h2, token=token)

    # Matching payload hash succeeds ONCE
    assert store.verify_and_consume(h1, token=token) is True

    # Replay of same single-use token must fail
    with pytest.raises(InvalidApprovalTokenError):
        store.verify_and_consume(h1, token=token)

    # Expired grant (> 60s) must fail
    req2 = store.create_request(h1, "Hotkey ctrl+w", "Notepad")
    tok2 = store.approve_request(req2.request_id)
    now[0] += 61.0
    with pytest.raises(InvalidApprovalTokenError):
        store.verify_and_consume(h1, token=tok2)


def test_window_denylist_and_sensitive_buttons() -> None:
    check_window_denylist(["Untitled - Notepad", "Google Chrome"])
    for bad_title in ("1Password - Vault", "My .env - Visual Studio Code", "Bitwarden Desktop"):
        with pytest.raises(WindowBlockedByPolicyError) as exc:
            check_window_denylist(bad_title)
        assert exc.value.code == "WINDOW_BLOCKED_BY_POLICY"

    assert is_sensitive_button_name("Delete Permanently") is True
    assert is_sensitive_button_name("Pay Now") is True
    assert is_sensitive_button_name("Add New Tab") is False


def test_smart_circuit_breaker_rate_loop_and_cap() -> None:
    now = [0.0]
    breaker = SmartCircuitBreaker(
        max_rate=3,
        window_s=10.0,
        repeat_threshold=5,
        hard_cap=6,
        clock=lambda: now[0],
    )

    # Trigger RepeatLoopDetector (threshold=5 on distinct rate window via clock bump)
    breaker_loop = SmartCircuitBreaker(
        max_rate=20, window_s=60.0, repeat_threshold=5, hard_cap=100, clock=lambda: now[0]
    )
    for idx in range(4):
        breaker_loop.check_and_record("arisu_cu_mouse_action", "Notepad", {"x": idx * 0})
    with pytest.raises(RepeatLoopDetectedError) as exc_loop:
        breaker_loop.check_and_record("arisu_cu_mouse_action", "Notepad", {"x": 0})
    assert exc_loop.value.code == "REPEAT_LOOP_DETECTED"

    # Trigger RateLimiter
    for i in range(3):
        breaker.check_and_record("arisu_cu_mouse_action", "Notepad", {"i": i})
    with pytest.raises(RateLimitExceededError) as exc_rate:
        breaker.check_and_record("arisu_cu_mouse_action", "Notepad", {"i": 99})
    assert exc_rate.value.code == "RATE_LIMIT_EXCEEDED"

    # Trigger ActionCap and extension (+50)
    now[0] += 20.0
    for i in range(3):
        breaker.check_and_record("arisu_cu_mouse_action", "Notepad", {"i": 100 + i})
    with pytest.raises(ActionCapReachedError) as exc_cap:
        breaker.check_and_record("arisu_cu_mouse_action", "Notepad", {"i": 200})
    assert exc_cap.value.code == "ACTION_CAP_REACHED"

    assert breaker.extend_cap(50) == 56
    now[0] += 20.0
    breaker.check_and_record("arisu_cu_mouse_action", "Notepad", {"i": 201})


def test_corner_failsafe_monitor_300ms_dwell() -> None:
    reset_abort()
    vrect = VirtualScreenRect(left=-1920, top=0, width=3520, height=1080)
    cursor = [0, 0]  # Seam between monitor 1 and 2 -> must NOT trigger
    mon = CornerFailsafeMonitor(
        dwell_s=0.30,
        cursor_getter=lambda: (cursor[0], cursor[1]),
        vrect_getter=lambda: vrect,
    )
    assert mon.check_once(now=1.0) is False
    assert mon.check_once(now=2.0) is False
    assert ABORT_EVENT.is_set() is False

    cursor = [-1920, 0]  # Outer top-left corner (v_left, v_top)
    assert mon.check_once(now=3.0) is False
    assert mon.check_once(now=3.20) is False  # < 300 ms dwell
    assert ABORT_EVENT.is_set() is False
    assert mon.check_once(now=3.35) is True  # >= 300 ms dwell triggers ABORT_EVENT
    assert ABORT_EVENT.is_set() is True
    reset_abort()


def test_audit_logger_redacts_tokens_and_sensitive_text(tmp_path) -> None:
    log_path = tmp_path / "logs" / "audit.jsonl"
    logger = AuditLogger(log_path=log_path)

    logger.log(
        session_id="sess_audit",
        tool="arisu_cu_keyboard_action",
        status="ok",
        target_window="Login Window",
        latency_ms=14.25,
        args={"action": "type", "text": "my-secret-pw", "approval_token": "tok_secret123"},
        redact_text=True,
    )

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["args"]["approval_token"] == "[REDACTED]"
    assert row["args"]["text"] == "[REDACTED]"
    assert row["tool"] == "arisu_cu_keyboard_action"
