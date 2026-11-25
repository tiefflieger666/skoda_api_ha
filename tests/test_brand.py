"""Brand images shipped with the integration (HA 2026.3+ brand/ folder)."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

BRAND = Path(__file__).parent.parent / "custom_components" / "skoda_api_ha" / "brand"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()[:24]
    assert data[:8] == PNG_SIGNATURE
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


@pytest.mark.parametrize(("name", "size"), [("icon.png", 256), ("icon@2x.png", 512)])
def test_brand_icon_size(name: str, size: int) -> None:
    """Icons are square PNGs of the size the brands guidelines require."""
    assert _png_size(BRAND / name) == (size, size)
