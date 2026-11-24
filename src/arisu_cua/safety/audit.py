"""Structured JSON-lines audit logger with sensitive input redaction and 5 MB rotation."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arisu_cua.config import get_audit_log_path

MAX_AUDIT_BYTES = 5 * 1024 * 1024
_SENSITIVE_KEYS = frozenset({"token", "approval_token", "password", "secret"})


def _redact_args(args: dict[str, Any], redact_text: bool = False) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for k, v in args.items():
        if k in _SENSITIVE_KEYS:
            cleaned[k] = "[REDACTED]" if v is not None else None
        elif k == "text" and redact_text and v is not None:
            cleaned[k] = "[REDACTED]"
        elif isinstance(v, str) and len(v) > 200:
            cleaned[k] = v[:200] + "…"
        else:
            cleaned[k] = v
    return cleaned


class AuditLogger:
    """Thread-safe rotating JSONL logger writing to %LOCALAPPDATA%\\arisu-computer-use\\logs."""

    def __init__(
        self,
        log_path: Path | None = None,
        max_bytes: int = MAX_AUDIT_BYTES,
    ) -> None:
        self.log_path = log_path or get_audit_log_path()
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()

    def _rotate_if_needed(self) -> None:
        if not self.log_path.exists():
            return
        try:
            if self.log_path.stat().st_size >= self.max_bytes:
                rotated = self.log_path.with_suffix(".jsonl.1")
                if rotated.exists():
                    rotated.unlink()
                self.log_path.replace(rotated)
        except Exception:
            pass

    def log(
        self,
        *,
        session_id: str,
        tool: str,
        status: str,
        monitor: int | str | None = None,
        target_window: str = "",
        code: str = "",
        latency_ms: float = 0.0,
        args: dict[str, Any] | None = None,
        redact_text: bool = False,
    ) -> None:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "session_id": session_id,
            "tool": tool,
            "monitor": monitor,
            "target_window": target_window[:80],
            "status": status,
            "code": code,
            "latency_ms": round(float(latency_ms), 2),
            "args": _redact_args(args or {}, redact_text=redact_text),
        }
        line = json.dumps(entry, ensure_ascii=True) + "\n"
        with self._lock:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                with self.log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            except Exception:
                pass
