"""Tests for S2 persistent killable UIA subprocess, SoM registry, and [1..N] overlay."""

import time
from io import BytesIO

import pytest
from PIL import Image, ImageChops

from arisu_cua.display.capture import prepare_captured_image
from arisu_cua.display.monitors import MonitorInfo
from arisu_cua.perception.uia_client import ElementBox, UIASubprocessClient
from arisu_cua.safety.killswitch import ElementNotFoundError, UIATimeoutError


def test_uia_client_ping_and_inspect() -> None:
    client = UIASubprocessClient(timeout_s=2.0)
    try:
        pong = client.ping()
        assert pong["ok"] is True
        assert isinstance(pong["pid"], int) and pong["pid"] > 0

        mon = MonitorInfo(
            index=1,
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
        root_name, elements = client.inspect_tree(
            mon,
            max_depth=2,
            max_items=10,
            interactive_only=True,
        )
        assert isinstance(root_name, str)
        assert isinstance(elements, list)
    finally:
        client.close()


def test_uia_client_timeout_kill_and_auto_respawn() -> None:
    client = UIASubprocessClient(timeout_s=0.6)
    try:
        first_pong = client.ping()
        old_pid = first_pong["pid"]

        t0 = time.perf_counter()
        with pytest.raises(UIATimeoutError) as exc_info:
            client.request({"cmd": "simulate_hang"}, timeout_s=0.6)
        elapsed = time.perf_counter() - t0

        assert exc_info.value.code == "UIA_TIMEOUT"
        assert elapsed < 2.5

        recovery_pong = client.ping(timeout_s=2.0)
        assert recovery_pong["ok"] is True
        assert recovery_pong["pid"] != old_pid
    finally:
        client.close()


def test_som_registry_and_overlay_rendering() -> None:
    client = UIASubprocessClient()
    el1 = ElementBox(
        element_id=1,
        name="Save",
        control_type="ButtonControl",
        automation_id="btnSave",
        bbox_px=(100, 100, 260, 150),
        center_px=(180, 125),
        bbox_norm=(100, 100, 260, 150),
        center_norm=(180, 125),
        is_password=False,
        monitor_index=1,
        window_title="Demo Window",
    )
    el2 = ElementBox(
        element_id=2,
        name="Password",
        control_type="EditControl",
        automation_id="txtPass",
        bbox_px=(100, 220, 420, 280),
        center_px=(260, 250),
        bbox_norm=(100, 220, 420, 280),
        center_norm=(260, 250),
        is_password=True,
        monitor_index=1,
        window_title="Demo Window",
    )

    client.update_som_registry([el1, el2])
    assert client.get_som_element(1) == el1
    assert client.get_som_element(2).is_password is True

    with pytest.raises(ElementNotFoundError) as exc_info:
        client.get_som_element(99)
    assert exc_info.value.code == "ELEMENT_NOT_FOUND"

    base_img = Image.new("RGB", (640, 480), (240, 244, 250))
    clean_shot = prepare_captured_image(
        base_img,
        monitor_index=1,
        fmt="webp",
        max_size=640,
        mode="clean",
    )
    som_shot = prepare_captured_image(
        base_img,
        monitor_index=1,
        fmt="webp",
        max_size=640,
        mode="som",
        som_elements=[el1, el2],
    )

    assert som_shot.mime_type == "image/webp"
    with (
        Image.open(BytesIO(clean_shot.data)) as img_clean,
        Image.open(BytesIO(som_shot.data)) as img_som,
    ):
        diff = ImageChops.difference(img_clean.convert("RGB"), img_som.convert("RGB"))
        assert diff.getbbox() is not None

    client.clear_som_registry()
    with pytest.raises(ElementNotFoundError):
        client.get_som_element(1)
