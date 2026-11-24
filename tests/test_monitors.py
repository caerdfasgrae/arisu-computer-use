"""Unit tests for Dual-Monitor DPI coordinate math and monitor resolution."""

import pytest

from arisu_cua.display.monitors import (
    MonitorInfo,
    VirtualScreenRect,
    norm_to_physical,
    physical_to_norm,
    physical_to_virtualdesk,
    resolve_monitor,
)


def _sample_monitors() -> list[MonitorInfo]:
    return [
        MonitorInfo(
            index=1,
            device=r"\\.\DISPLAY2",
            left=-1920,
            top=0,
            width=1920,
            height=1080,
            dpi_x=120,
            dpi_y=120,
            scale_pct=125,
            is_primary=False,
        ),
        MonitorInfo(
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
        ),
    ]


def test_norm_to_physical_negative_x_and_primary() -> None:
    m_left, m_right = _sample_monitors()
    assert norm_to_physical(m_left, 0, 0) == (-1920, 0)
    assert norm_to_physical(m_left, 1000, 1000) == (-1, 1079)
    assert norm_to_physical(m_left, 500, 500) == (-960, 540)

    assert norm_to_physical(m_right, 0, 0) == (0, 0)
    assert norm_to_physical(m_right, 1000, 1000) == (1599, 899)
    assert norm_to_physical(m_right, 500, 500) == (800, 450)


def test_norm_roundtrip_quantization_tolerance() -> None:
    m_left, m_right = _sample_monitors()
    for mon in (m_left, m_right):
        for nx, ny in ((0, 0), (123, 456), (500, 500), (875, 210), (1000, 1000)):
            px_x, px_y = norm_to_physical(mon, nx, ny)
            rx, ry = physical_to_norm(mon, px_x, px_y)
            assert abs(rx - nx) <= 1
            assert abs(ry - ny) <= 1


def test_norm_out_of_bounds_raises() -> None:
    mon = _sample_monitors()[0]
    with pytest.raises(ValueError, match="INVALID_COORDS"):
        norm_to_physical(mon, -1, 500)
    with pytest.raises(ValueError, match="INVALID_COORDS"):
        norm_to_physical(mon, 500, 1001)


def test_physical_to_virtualdesk_edges_and_seam() -> None:
    vrect = VirtualScreenRect(left=-1920, top=0, width=3520, height=1080)
    assert physical_to_virtualdesk(-1920, 0, vrect) == (0, 0)
    assert physical_to_virtualdesk(1599, 1079, vrect) == (65535, 65535)
    seam_x, seam_y = physical_to_virtualdesk(0, 0, vrect)
    assert seam_x == round((1920 * 65535) / 3519)
    assert seam_y == 0


def test_resolve_monitor_selectors() -> None:
    mons = _sample_monitors()
    assert resolve_monitor(mons, "1").index == 1
    assert resolve_monitor(mons, 2).index == 2
    assert resolve_monitor(mons, "primary").index == 2
    assert resolve_monitor(mons, "secondary").index == 1
    assert resolve_monitor(mons, "active", fg_rect=(-1800, 50, -200, 800)).index == 1
    assert resolve_monitor(mons, "active", fg_rect=None, cursor_pos=(400, 300)).index == 2
    with pytest.raises(ValueError, match="MONITOR_NOT_FOUND"):
        resolve_monitor(mons, "3")
