"""Security guardrails: Out-of-Band SHA-256 Approval Store, Denylist, and Circuit Breaker."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from arisu_cua.config import (
    RATE_LIMIT_MAX_ACTIONS,
    RATE_LIMIT_WINDOW_SECONDS,
    REPEAT_LOOP_THRESHOLD,
    SESSION_HARD_CAP,
)
from arisu_cua.safety.killswitch import (
    ActionCapReachedError,
    InvalidApprovalTokenError,
    RateLimitExceededError,
    RepeatLoopDetectedError,
    WindowBlockedByPolicyError,
)

DEFAULT_WINDOW_DENYLIST: tuple[str, ...] = (
    "1password",
    "bitwarden",
    "keepass",
    "credential manager",
    ".env",
    "secret",
    "payroll",
    "user account control",
)

DEFAULT_BROWSER_URL_DENYLIST: tuple[str, ...] = (
    "1password",
    "bitwarden",
    "keepass",
    "lastpass",
    "credential",
    ".env",
    ".ssh",
    "id_rsa",
    "private_key",
    "payroll",
    "klikbca",
    "mybca",
    "livin",
    "brimo",
)

SENSITIVE_BUTTON_KEYWORDS: tuple[str, ...] = (
    "delete permanently",
    "hapus permanen",
    "format disk",
    "pay now",
    "checkout",
    "transfer",
    "uninstall",
)


def compute_action_hash(tool: str, target_window: str, payload: dict[str, Any]) -> str:
    """Compute a deterministic SHA-256 binding a sensitive tool call to its exact target/args."""
    canonical = json.dumps(
        {
            "tool": str(tool),
            "target": str(target_window or "").strip().lower(),
            "payload": payload,
        },
        sort_keys=True,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def check_window_denylist(
    window_titles: str | list[str],
    denylist: tuple[str, ...] = DEFAULT_WINDOW_DENYLIST,
) -> None:
    """Reject capture/UIA inspection when a sensitive window is targeted or active."""
    titles = [window_titles] if isinstance(window_titles, str) else list(window_titles)
    for title in titles:
        low = str(title or "").lower()
        if not low:
            continue
        for rule in denylist:
            if rule in low:
                raise WindowBlockedByPolicyError(str(title), rule)


def check_browser_target_denylist(
    title: str | None = None,
    url: str | None = None,
    url_denylist: tuple[str, ...] = DEFAULT_BROWSER_URL_DENYLIST,
) -> None:
    """Block CDP browser actions on sensitive titles, credential vaults, or office repos."""
    check_window_denylist([title or "", url or ""])
    raw_url = str(url or "").strip().lower()
    if raw_url:
        for rule in url_denylist:
            if rule in raw_url:
                raise WindowBlockedByPolicyError(str(url), rule)


def is_sensitive_button_name(name: str | None) -> bool:
    low = str(name or "").strip().lower()
    if not low:
        return False
    return any(kw in low for kw in SENSITIVE_BUTTON_KEYWORDS)


@dataclass(frozen=True)
class PendingApproval:
    request_id: str
    action_hash: str
    action_desc: str
    target_window: str
    expires_at: float


@dataclass(frozen=True)
class ApprovedGrant:
    token: str
    action_hash: str
    expires_at: float


class PendingApprovalStore:
    """Thread-safe Out-of-Band approval store (SHA-256 bound, TTL 60s, single-use)."""

    def __init__(
        self,
        ttl_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_s = float(ttl_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, PendingApproval] = {}
        self._approved_tokens: dict[str, ApprovedGrant] = {}
        self._latest_grant_by_hash: dict[str, str] = {}

    def _purge_expired(self, now: float) -> None:
        expired_reqs = [rid for rid, req in self._pending.items() if req.expires_at <= now]
        for rid in expired_reqs:
            self._pending.pop(rid, None)

        expired_tokens = [tok for tok, gr in self._approved_tokens.items() if gr.expires_at <= now]
        for tok in expired_tokens:
            grant = self._approved_tokens.pop(tok, None)
            if grant and self._latest_grant_by_hash.get(grant.action_hash) == tok:
                self._latest_grant_by_hash.pop(grant.action_hash, None)

    def create_request(
        self,
        action_hash: str,
        action_desc: str,
        target_window: str = "",
    ) -> PendingApproval:
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            for existing in self._pending.values():
                if existing.action_hash == action_hash:
                    return existing
            req = PendingApproval(
                request_id=f"req_{secrets.token_hex(6)}",
                action_hash=action_hash,
                action_desc=action_desc[:120],
                target_window=target_window[:80],
                expires_at=now + self.ttl_s,
            )
            self._pending[req.request_id] = req
            return req

    def get_latest_pending(self) -> PendingApproval | None:
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            if not self._pending:
                return None
            return list(self._pending.values())[-1]

    def approve_request(self, request_id: str) -> str:
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            req = self._pending.pop(request_id, None)
            if req is None:
                raise InvalidApprovalTokenError(
                    f"Approval request '{request_id}' not found or expired"
                )
            token = f"tok_{secrets.token_hex(12)}"
            grant = ApprovedGrant(
                token=token,
                action_hash=req.action_hash,
                expires_at=now + self.ttl_s,
            )
            self._approved_tokens[token] = grant
            self._latest_grant_by_hash[req.action_hash] = token
            return token

    def deny_request(self, request_id: str) -> None:
        with self._lock:
            req = self._pending.pop(request_id, None)
            if req is not None:
                tok = self._latest_grant_by_hash.pop(req.action_hash, None)
                if tok:
                    self._approved_tokens.pop(tok, None)

    def verify_and_consume(self, action_hash: str, token: str | None = None) -> bool:
        now = self._clock()
        with self._lock:
            self._purge_expired(now)
            if token is not None and token != "":
                grant = self._approved_tokens.get(token)
                if grant is None:
                    raise InvalidApprovalTokenError("Unknown or expired approval token")
                if grant.action_hash != action_hash:
                    raise InvalidApprovalTokenError(
                        "Approval token does not match requested action payload"
                    )
                self._approved_tokens.pop(token, None)
                if self._latest_grant_by_hash.get(action_hash) == token:
                    self._latest_grant_by_hash.pop(action_hash, None)
                return True

            minted_tok = self._latest_grant_by_hash.pop(action_hash, None)
            if minted_tok is not None:
                grant = self._approved_tokens.pop(minted_tok, None)
                if grant is not None and grant.expires_at > now:
                    return True
            return False


class RateLimiter:
    def __init__(
        self,
        max_actions: int = RATE_LIMIT_MAX_ACTIONS,
        window_s: float = RATE_LIMIT_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_actions = int(max_actions)
        self.window_s = float(window_s)
        self._clock = clock
        self._timestamps: deque[float] = deque()

    def check_and_record(self) -> None:
        now = self._clock()
        cutoff = now - self.window_s
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_actions:
            raise RateLimitExceededError(self.max_actions, self.window_s)
        self._timestamps.append(now)

    def reset(self) -> None:
        self._timestamps.clear()


class RepeatLoopDetector:
    def __init__(self, threshold: int = REPEAT_LOOP_THRESHOLD) -> None:
        self.threshold = max(int(threshold), 2)
        self._last_sig: str | None = None
        self._streak: int = 0

    def check_and_record(self, action_hash: str) -> None:
        if action_hash == self._last_sig:
            self._streak += 1
        else:
            self._last_sig = action_hash
            self._streak = 1
        if self._streak >= self.threshold:
            raise RepeatLoopDetectedError(self.threshold)

    def reset(self) -> None:
        self._last_sig = None
        self._streak = 0


class ActionCap:
    def __init__(self, max_actions: int = SESSION_HARD_CAP) -> None:
        self.max_actions = int(max_actions)
        self.count: int = 0

    def check_available(self) -> None:
        if self.count >= self.max_actions:
            raise ActionCapReachedError(self.max_actions)

    def increment(self) -> None:
        self.count += 1

    def check_and_increment(self) -> None:
        self.check_available()
        self.increment()

    def extend_cap(self, extra: int = 50) -> int:
        self.max_actions += max(int(extra), 1)
        return self.max_actions

    def reset(self, default_cap: int = SESSION_HARD_CAP) -> None:
        self.max_actions = int(default_cap)
        self.count = 0


class SmartCircuitBreaker:
    """Combines RateLimiter (60/min), RepeatLoopDetector (5x), and ActionCap (100)."""

    def __init__(
        self,
        max_rate: int = RATE_LIMIT_MAX_ACTIONS,
        window_s: float = RATE_LIMIT_WINDOW_SECONDS,
        repeat_threshold: int = REPEAT_LOOP_THRESHOLD,
        hard_cap: int = SESSION_HARD_CAP,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self.rate = RateLimiter(max_actions=max_rate, window_s=window_s, clock=clock)
        self.loop = RepeatLoopDetector(threshold=repeat_threshold)
        self.cap = ActionCap(max_actions=hard_cap)

    def check_and_record(self, tool: str, target_window: str, payload: dict[str, Any]) -> None:
        sig = compute_action_hash(tool, target_window, payload)
        with self._lock:
            self.cap.check_available()
            self.rate.check_and_record()
            self.loop.check_and_record(sig)
            self.cap.increment()

    def extend_cap(self, extra: int = 50) -> int:
        with self._lock:
            return self.cap.extend_cap(extra)

    def reset(self) -> None:
        with self._lock:
            self.rate.reset()
            self.loop.reset()
            self.cap.reset()
