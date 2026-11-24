"""Zero-dependency native Chrome DevTools Protocol (CDP) browser fast-lane."""

from arisu_cua.browser.cdp_client import (
    CdpBrowserClient,
    CdpTabInfo,
    find_system_browser_exe,
    get_cdp_client,
)

__all__ = [
    "CdpBrowserClient",
    "CdpTabInfo",
    "find_system_browser_exe",
    "get_cdp_client",
]
