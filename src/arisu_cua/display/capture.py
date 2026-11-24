"""In-memory multi-monitor screen capture, coordinate grid overlay, and encoding."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from typing import TYPE_CHECKING, Any, Literal

import mss
from PIL import Image, ImageDraw, ImageFont

from arisu_cua.config import DEFAULT_IMAGE_FORMAT, DEFAULT_IMAGE_QUALITY, DEFAULT_MAX_SIZE
from arisu_cua.display.monitors import MonitorInfo
from arisu_cua.safety.killswitch import StaleScreenRegionError

if TYPE_CHECKING:
    from arisu_cua.perception.uia_client import ElementBox

_LAST_MONITOR_CAPTURES: dict[int, tuple[float, MonitorInfo, Image.Image]] = {}
_MSS_TLS = threading.local()


def _grab_mss_rgb(grab_rect: dict[str, int]) -> Image.Image:
    """Grab raw screen rectangle using a thread-local mss.MSS() handle."""
    for _ in range(2):
        sct = getattr(_MSS_TLS, "sct", None)
        if sct is None:
            sct = mss.MSS()
            _MSS_TLS.sct = sct
        try:
            shot = sct.grab(grab_rect)
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except Exception:
            with contextlib.suppress(Exception):
                sct.close()
            _MSS_TLS.sct = None
    with mss.MSS() as sct:
        shot = sct.grab(grab_rect)
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def record_monitor_capture(mon: MonitorInfo, rgb: Image.Image, ts: float | None = None) -> None:
    """Cache the latest clean RGB monitor frame in memory for pre-click TOCTOU checks."""
    _LAST_MONITOR_CAPTURES[mon.index] = (
        time.monotonic() if ts is None else float(ts),
        mon,
        rgb.copy(),
    )


def clear_capture_cache() -> None:
    """Invalidate cached monitor captures and any cached UIA SoM element registry."""
    _LAST_MONITOR_CAPTURES.clear()
    with contextlib.suppress(Exception):
        from arisu_cua.perception import uia_client

        if uia_client._CLIENT_SINGLETON is not None:
            uia_client._CLIENT_SINGLETON.clear_som_registry()


@dataclass(frozen=True)
class CapturedImage:
    data: bytes
    mime_type: str
    fmt: str
    width: int
    height: int
    monitor_index: int
    source_width: int
    source_height: int
    crop_norm_box: tuple[int, int, int, int]

    def to_metadata(self) -> dict:
        return {
            "monitor": self.monitor_index,
            "format": self.fmt,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "raw_bytes": len(self.data),
            "crop_norm_box": list(self.crop_norm_box),
        }


def _normalize_crop_box(region: list[int] | tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    if len(region) != 4:
        raise ValueError("INVALID_REGION: region must be [x1, y1, x2, y2] in 0..1000")
    x1, y1, x2, y2 = (int(v) for v in region)
    x1 = min(max(x1, 0), 999)
    y1 = min(max(y1, 0), 999)
    x2 = min(max(x2, x1 + 1), 1000)
    y2 = min(max(y2, y1 + 1), 1000)
    return x1, y1, x2, y2


def _draw_coordinate_grid(
    img: Image.Image, crop_norm_box: tuple[int, int, int, int]
) -> Image.Image:
    """Overlay crisp 100-unit monitor coordinate lines and Moonlit Sapphire edge labels."""
    base = img.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
    w, h = base.size
    nx1, ny1, nx2, ny2 = crop_norm_box
    span_x = max(nx2 - nx1, 1)
    span_y = max(ny2 - ny1, 1)

    step = 50 if max(span_x, span_y) <= 400 else 100
    first_x = ((nx1 // step) + 1) * step
    for gx in range(first_x, nx2, step):
        px = round(((gx - nx1) / span_x) * (w - 1))
        is_major = gx % 500 == 0
        line_col = (28, 63, 122, 165) if is_major else (186, 194, 220, 105)
        draw.line([(px, 0), (px, h - 1)], fill=line_col, width=2 if is_major else 1)
        label = str(gx)
        draw.rectangle([px + 2, 2, px + 26, 15], fill=(28, 63, 122, 210))
        draw.text((px + 4, 3), label, fill=(255, 255, 255, 255), font=font)

    first_y = ((ny1 // step) + 1) * step
    for gy in range(first_y, ny2, step):
        py = round(((gy - ny1) / span_y) * (h - 1))
        is_major = gy % 500 == 0
        line_col = (28, 63, 122, 165) if is_major else (186, 194, 220, 105)
        draw.line([(0, py), (w - 1, py)], fill=line_col, width=2 if is_major else 1)
        label = str(gy)
        draw.rectangle([2, py + 2, 26, py + 15], fill=(28, 63, 122, 210))
        draw.text((4, py + 3), label, fill=(255, 255, 255, 255), font=font)

    return Image.alpha_composite(base, overlay).convert("RGB")


def _draw_som_overlay(
    img: Image.Image,
    elements: list[ElementBox],
    crop_norm_box: tuple[int, int, int, int],
) -> Image.Image:
    """Draw Moonlit Sapphire Set-of-Mark [1..N] bounding boxes and badges."""
    base = img.convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
    w, h = base.size
    nx1, ny1, nx2, ny2 = crop_norm_box
    span_x = max(nx2 - nx1, 1)
    span_y = max(ny2 - ny1, 1)

    for el in elements:
        ex1, ey1, ex2, ey2 = el.bbox_norm
        if ex2 < nx1 or ex1 > nx2 or ey2 < ny1 or ey1 > ny2:
            continue

        bx1 = max(0, min(w - 3, round(((ex1 - nx1) / span_x) * (w - 1))))
        by1 = max(0, min(h - 3, round(((ey1 - ny1) / span_y) * (h - 1))))
        bx2 = max(bx1 + 2, min(w - 1, round(((ex2 - nx1) / span_x) * (w - 1))))
        by2 = max(by1 + 2, min(h - 1, round(((ey2 - ny1) / span_y) * (h - 1))))

        outer_col = (252, 227, 230, 240) if el.is_password else (224, 232, 245, 220)
        inner_col = (186, 48, 72, 240) if el.is_password else (28, 63, 122, 235)
        badge_bg = (186, 48, 72, 245) if el.is_password else (28, 63, 122, 245)

        draw.rectangle(
            [max(0, bx1 - 1), max(0, by1 - 1), min(w - 1, bx2 + 1), min(h - 1, by2 + 1)],
            outline=outer_col,
            width=1,
        )
        draw.rectangle([bx1, by1, bx2, by2], outline=inner_col, width=2)

        tag = f"[{el.element_id}]"
        tb = draw.textbbox((0, 0), tag, font=font)
        tw = (tb[2] - tb[0]) + 6
        th = (tb[3] - tb[1]) + 4
        tag_y1 = max(0, by1 - th)
        tag_x2 = min(w - 1, bx1 + tw)
        draw.rectangle([bx1, tag_y1, tag_x2, tag_y1 + th], fill=badge_bg)
        draw.text((bx1 + 3, tag_y1 + 1), tag, fill=(255, 255, 255, 255), font=font)

    return Image.alpha_composite(base, overlay).convert("RGB")


def prepare_captured_image(
    img: Image.Image,
    monitor_index: int,
    fmt: Literal["webp", "jpeg"] = DEFAULT_IMAGE_FORMAT,
    max_size: int = DEFAULT_MAX_SIZE,
    mode: Literal["clean", "grid", "som"] = "clean",
    region: list[int] | tuple[int, int, int, int] | None = None,
    quality: int = DEFAULT_IMAGE_QUALITY,
    som_elements: list[ElementBox] | None = None,
) -> CapturedImage:
    """Crop, annotate (grid/som), downscale, and encode an RGB image strictly in-memory."""
    if fmt not in ("webp", "jpeg"):
        raise ValueError(f"UNSUPPORTED_FORMAT: '{fmt}' (use 'webp' or 'jpeg')")
    if max_size < 128 or max_size > 3840:
        raise ValueError("INVALID_MAX_SIZE: max_size must be between 128 and 3840")

    src_w, src_h = img.size
    crop_box = (0, 0, 1000, 1000)
    work = img.convert("RGB")

    if region is not None:
        crop_box = _normalize_crop_box(region)
        nx1, ny1, nx2, ny2 = crop_box
        px1 = round((nx1 / 1000.0) * src_w)
        py1 = round((ny1 / 1000.0) * src_h)
        px2 = max(px1 + 1, round((nx2 / 1000.0) * src_w))
        py2 = max(py1 + 1, round((ny2 / 1000.0) * src_h))
        work = work.crop((px1, py1, px2, py2))

    longest = max(work.width, work.height)
    if longest > max_size:
        scale = max_size / longest
        new_size = (max(1, round(work.width * scale)), max(1, round(work.height * scale)))
        work = work.resize(new_size, Image.Resampling.LANCZOS)

    if mode == "grid":
        work = _draw_coordinate_grid(work, crop_box)
    elif mode == "som":
        work = _draw_coordinate_grid(work, crop_box)
        if som_elements:
            work = _draw_som_overlay(work, som_elements, crop_box)

    buf = BytesIO()
    if fmt == "webp":
        work.save(buf, format="WEBP", quality=quality, method=4)
        mime_type = "image/webp"
    else:
        work.save(buf, format="JPEG", quality=quality, optimize=True)
        mime_type = "image/jpeg"

    return CapturedImage(
        data=buf.getvalue(),
        mime_type=mime_type,
        fmt=fmt,
        width=work.width,
        height=work.height,
        monitor_index=monitor_index,
        source_width=src_w,
        source_height=src_h,
        crop_norm_box=crop_box,
    )


def capture_monitor(
    mon: MonitorInfo,
    fmt: Literal["webp", "jpeg"] = DEFAULT_IMAGE_FORMAT,
    max_size: int = DEFAULT_MAX_SIZE,
    mode: Literal["clean", "grid", "som"] = "clean",
    region: list[int] | tuple[int, int, int, int] | None = None,
    som_elements: list[ElementBox] | None = None,
) -> CapturedImage:
    """Capture a single physical monitor via mss.MSS() strictly in-memory."""
    grab_rect = {
        "left": mon.left,
        "top": mon.top,
        "width": mon.width,
        "height": mon.height,
    }
    rgb = _grab_mss_rgb(grab_rect)
    record_monitor_capture(mon, rgb)
    return prepare_captured_image(
        rgb,
        monitor_index=mon.index,
        fmt=fmt,
        max_size=max_size,
        mode=mode,
        region=region,
        som_elements=som_elements,
    )


def verify_pre_click_patch(
    px_x: int,
    px_y: int,
    mon: MonitorInfo | None = None,
    min_age_s: float = 0.8,
    max_age_s: float = 30.0,
    threshold: float = 42.0,
    patch_grabber: Callable[[dict[str, int]], Image.Image] | None = None,
) -> dict[str, Any]:
    """Compare a 64x64 live patch around (px_x, px_y) against the latest captured frame."""
    entry: tuple[float, MonitorInfo, Image.Image] | None = None
    if mon is not None:
        entry = _LAST_MONITOR_CAPTURES.get(mon.index)
    if entry is None:
        for candidate in _LAST_MONITOR_CAPTURES.values():
            if candidate[1].contains(px_x, px_y):
                entry = candidate
                break
    if entry is None:
        return {"checked": False, "mean_diff": 0.0}

    cap_ts, cap_mon, cap_rgb = entry
    age = time.monotonic() - cap_ts
    if age < min_age_s or age > max_age_s:
        return {"checked": False, "mean_diff": 0.0}

    half = 32
    box_left = max(cap_mon.left, min(px_x - half, cap_mon.right - 64))
    box_top = max(cap_mon.top, min(px_y - half, cap_mon.bottom - 64))
    box_right = min(cap_mon.right, box_left + 64)
    box_bottom = min(cap_mon.bottom, box_top + 64)
    w_box = max(1, box_right - box_left)
    h_box = max(1, box_bottom - box_top)

    rel_left = box_left - cap_mon.left
    rel_top = box_top - cap_mon.top
    ref_patch = cap_rgb.crop((rel_left, rel_top, rel_left + w_box, rel_top + h_box)).convert("RGB")

    grab_rect = {"left": box_left, "top": box_top, "width": w_box, "height": h_box}
    if patch_grabber is not None:
        live_patch = patch_grabber(grab_rect).convert("RGB")
    else:
        live_patch = _grab_mss_rgb(grab_rect)

    if live_patch.size != ref_patch.size:
        live_patch = live_patch.resize(ref_patch.size)

    ref_bytes = ref_patch.tobytes()
    live_bytes = live_patch.tobytes()
    mean_diff = sum(abs(a - b) for a, b in zip(ref_bytes, live_bytes, strict=True)) / max(
        len(ref_bytes), 1
    )
    if mean_diff > threshold:
        raise StaleScreenRegionError(mean_diff=mean_diff, threshold=threshold)
    return {"checked": True, "mean_diff": round(mean_diff, 2)}
