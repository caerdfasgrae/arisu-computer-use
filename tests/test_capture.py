"""Unit tests for in-memory capture preparation, WebP/JPEG encoding, crop, and grid."""

from io import BytesIO

import pytest
from PIL import Image

from arisu_cua.display.capture import prepare_captured_image


def test_prepare_webp_and_jpeg_downscale() -> None:
    img = Image.new("RGB", (1920, 1080), "#1C3F7A")
    res_webp = prepare_captured_image(img, monitor_index=1, fmt="webp", max_size=1280, mode="clean")
    assert res_webp.mime_type == "image/webp"
    assert (res_webp.width, res_webp.height) == (1280, 720)
    assert (res_webp.source_width, res_webp.source_height) == (1920, 1080)
    decoded = Image.open(BytesIO(res_webp.data))
    assert decoded.format == "WEBP"

    res_jpeg = prepare_captured_image(img, monitor_index=1, fmt="jpeg", max_size=1024, mode="clean")
    assert res_jpeg.mime_type == "image/jpeg"
    assert res_jpeg.width == 1024
    assert Image.open(BytesIO(res_jpeg.data)).format == "JPEG"


def test_prepare_region_zoom_and_grid_overlay() -> None:
    img = Image.new("RGB", (1600, 900), "#E0E8F5")
    res = prepare_captured_image(
        img,
        monitor_index=2,
        fmt="webp",
        max_size=1280,
        mode="grid",
        region=[250, 250, 750, 750],
    )
    assert res.crop_norm_box == (250, 250, 750, 750)
    assert res.width == 800
    assert res.height == 450
    assert len(res.data) > 0


def test_prepare_validation_errors() -> None:
    img = Image.new("RGB", (400, 300), "#FFFFFF")
    with pytest.raises(ValueError, match="UNSUPPORTED_FORMAT"):
        prepare_captured_image(img, monitor_index=1, fmt="bmp")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="INVALID_MAX_SIZE"):
        prepare_captured_image(img, monitor_index=1, max_size=64)
    with pytest.raises(ValueError, match="INVALID_REGION"):
        prepare_captured_image(img, monitor_index=1, region=[0, 10])
