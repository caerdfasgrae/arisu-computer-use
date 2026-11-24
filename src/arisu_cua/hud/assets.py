"""Asset extractor and canonical state mapping for Arisu 2D HUD."""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ASSETS_DIR = PROJECT_ROOT / "assets" / "arisu-2d"
SOURCE_ZIP_PATH = Path(os.environ.get("ARISU_ASSETS_ZIP", ""))

MOOD_TO_CARD_PNG: dict[str, str] = {
    "smile": "arisu_smile.png",
    "wink": "arisu_wink.png",
    "laugh": "arisu_laugh.png",
    "shy": "arisu_shy.png",
    "surprised": "arisu_surprised.png",
    "focused": "arisu_focused.png",
    "neutral": "arisu_neutral.png",
    "sleepy": "arisu_sleepy.png",
}

STATE_TO_CARD_PNG: dict[str, str] = {
    "idle": "arisu_smile.png",
    "listening": "arisu_wink.png",
    "thinking": "arisu_focused.png",
    "searching": "arisu_wink.png",
    "working": "arisu_smile.png",
    "speaking": "arisu_laugh.png",
    "success": "arisu_laugh.png",
    "error": "arisu_surprised.png",
    "shy": "arisu_shy.png",
    "sleepy": "arisu_sleepy.png",
}

STATE_DEFAULT_LABELS: dict[str, str] = {
    "idle": "",
    "listening": "Mendengarkan…",
    "thinking": "Arisu berpikir…",
    "searching": "Mengecek layar dulu~",
    "working": "Sedang dikerjakan!",
    "speaking": "Arisu bicara~",
    "success": "Selesai dikerjakan!",
    "error": "Ups, ada kendala!",
    "shy": "Butuh izinmu dulu nih…",
    "sleepy": "Arisu standby santai…",
}


def ensure_assets_extracted(
    assets_dir: Path = ASSETS_DIR,
    zip_path: Path = SOURCE_ZIP_PATH,
) -> Path:
    """Extract curated 2D assets from Arisu Design Kit zip if not yet present."""
    watch_dir = assets_dir / "watch"
    layers_dir = assets_dir / "layers"

    if (assets_dir / "palette.json").exists() and (watch_dir / "arisu_neutral.png").exists():
        return assets_dir

    if not zip_path.exists():
        raise FileNotFoundError(f"Arisu design kit zip not found: {zip_path}")

    assets_dir.mkdir(parents=True, exist_ok=True)
    watch_dir.mkdir(parents=True, exist_ok=True)
    layers_dir.mkdir(parents=True, exist_ok=True)

    direct_files = {
        "arisu-design-kit/palette.json": assets_dir / "palette.json",
        "arisu-design-kit/HUD_PET_SPEC.md": assets_dir / "HUD_PET_SPEC.md",
        "arisu-design-kit/2d/LAYER_MAP.md": assets_dir / "LAYER_MAP.md",
    }

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            if member in direct_files:
                direct_files[member].write_bytes(zf.read(member))
            elif member.startswith("arisu-design-kit/watch/assets/default.w432-s/") and (
                member.endswith(".png")
            ):
                fname = Path(member).name
                (watch_dir / fname).write_bytes(zf.read(member))
            elif member.startswith("arisu-design-kit/2d/layers/") and member.endswith(".png"):
                fname = Path(member).name
                (layers_dir / fname).write_bytes(zf.read(member))

    return assets_dir


def load_palette(assets_dir: Path = ASSETS_DIR) -> dict:
    """Load Moonlit Sapphire palette.json from curated assets."""
    ensure_assets_extracted(assets_dir=assets_dir)
    return json.loads((assets_dir / "palette.json").read_text(encoding="utf-8"))
