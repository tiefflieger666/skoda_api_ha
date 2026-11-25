"""en.json and de.json must have the same keys."""

from __future__ import annotations

import json
from pathlib import Path

TRANSLATIONS = (
    Path(__file__).parent.parent / "custom_components" / "skoda_api_ha" / "translations"
)


def _keys(data: object, prefix: str = "") -> set[str]:
    if isinstance(data, dict):
        keys: set[str] = set()
        for key, value in data.items():
            keys |= _keys(value, f"{prefix}{key}.")
        return keys
    return {prefix.rstrip(".")}


def test_en_and_de_have_same_keys() -> None:
    """Every English key has a German text and vice versa."""
    en = json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))
    de = json.loads((TRANSLATIONS / "de.json").read_text(encoding="utf-8"))
    assert _keys(en) == _keys(de)


def test_no_strings_json() -> None:
    """strings.json and [%key:...%] references only work in core."""
    folder = TRANSLATIONS.parent
    assert not (folder / "strings.json").exists()
    for path in TRANSLATIONS.glob("*.json"):
        assert "[%key:" not in path.read_text(encoding="utf-8")
