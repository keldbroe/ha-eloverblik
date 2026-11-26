"""Repository metadata, translations, and the integration icon."""

from __future__ import annotations

import json
import struct
from pathlib import Path

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "eloverblik_dk"


def _keys(value: object, prefix: str = "") -> set[str]:
    if isinstance(value, dict):
        found: set[str] = set()
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, dict):
                found |= _keys(item, path)
            else:
                found.add(path)
        return found
    return {prefix}


def test_translation_files_are_valid_and_share_keys() -> None:
    """English and Danish translations cover every string in strings.json."""
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (COMPONENT / "translations" / "en.json").read_text(encoding="utf-8")
    )
    danish = json.loads(
        (COMPONENT / "translations" / "da.json").read_text(encoding="utf-8")
    )
    assert _keys(strings) == _keys(english) == _keys(danish)
    assert strings["entity"]["sensor"]["day_total"]["name"]
    assert danish["config"]["error"]["invalid_auth"]
    assert "Forbrug" not in strings["config"]["step"]["user"]["title"]
    description = strings["config"]["step"]["user"]["description"]
    assert "{url}" in description
    assert "{site}" in description
    assert "https://" not in description
    assert "https://" not in danish["config"]["step"]["user"]["description"]


def test_manifest_and_hacs_metadata() -> None:
    """Marketplace metadata uses the dedicated domain and the current version."""
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == "eloverblik_dk"
    assert manifest["name"] == "ElOverblik (DK)"
    assert manifest["version"] == "0.2.1"
    assert manifest["codeowners"] == ["@keldbroe"]
    assert manifest["config_flow"] is True
    assert manifest["categories"] == ["energy.meters_monitoring"]
    keys = list(manifest)
    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])
    assert manifest["iot_class"] == "cloud_polling"
    assert manifest["documentation"] == "https://github.com/keldbroe/ha-eloverblik"
    assert hacs == {
        "name": "ElOverblik (DK)",
        "homeassistant": "2025.1.0",
        "country": ["DK"],
        "render_readme": True,
    }


def test_brand_icon_is_a_square_png() -> None:
    """The Marketplace icon is a square PNG with an alpha channel."""
    for relative in ("brand/icon.png", "icon.png"):
        path = COMPONENT / relative
        data = path.read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert data[12:16] == b"IHDR"
        width, height, bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
        assert width == height == 256
        assert bit_depth == 8
        assert color_type == 6
