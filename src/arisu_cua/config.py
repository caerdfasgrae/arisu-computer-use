"""Configuration paths and defaults for arisu-computer-use."""

from __future__ import annotations

import os
import re
from pathlib import Path

APP_NAME = "arisu-computer-use"
DEFAULT_IMAGE_FORMAT = "webp"
DEFAULT_MAX_SIZE = 1024
DEFAULT_IMAGE_QUALITY = 72
DEFAULT_WAIT_SECONDS = 0.8
MAX_WAIT_SECONDS = 5.0
UIA_TIMEOUT_SECONDS = 2.0
WINDOW_VERIFY_DEFAULT_S = 0.35
FOCUS_WAIT_MAX_S = 0.9
FOCUS_POLL_S = 0.06
WINDOW_ENUM_LIMIT = 80
CLIPBOARD_PASTE_THRESHOLD = 120
CDP_DEFAULT_PORT = 9222
CDP_SNAPSHOT_TEXT_LIMIT = 4000
CDP_SNAPSHOT_ELEMENTS_LIMIT = 80

RATE_LIMIT_MAX_ACTIONS = 60
RATE_LIMIT_WINDOW_SECONDS = 60.0
REPEAT_LOOP_THRESHOLD = 5
SESSION_HARD_CAP = 100


def get_app_data_dir() -> Path:
    """Return user-space writable AppData directory without admin privileges."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_NAME
    return Path.home() / "AppData" / "Local" / APP_NAME


def get_config_path() -> Path:
    return get_app_data_dir() / "config.json"


def get_hud_bridge_path() -> Path:
    return get_app_data_dir() / "hud_bridge.json"


def get_audit_log_path() -> Path:
    return get_app_data_dir() / "logs" / "audit.jsonl"


def get_cdp_state_path() -> Path:
    return get_app_data_dir() / "cdp_browser.json"


def get_cdp_profile_dir(profile: str = "default") -> Path:
    """Return isolated user-space Chrome profile directory (never touches main User Data)."""
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", (profile or "default").strip()).strip("_") or "default"
    if clean != "default":
        agent_browser_dir = Path.home() / ".agent-browser" / "profiles" / clean
        if agent_browser_dir.exists():
            return agent_browser_dir
    return get_app_data_dir() / "chrome_profiles" / clean
