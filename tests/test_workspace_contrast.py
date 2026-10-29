"""Keep normal text readable on the shared workspace's supported surfaces."""
from pathlib import Path
import re


CSS = Path(__file__).resolve().parents[1] / "static/css/workspace.css"


def luminance(value):
    channels = [int(value[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    channels = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
    return sum(c * weight for c, weight in zip(channels, (.2126, .7152, .0722)))


def test_shared_text_meets_normal_text_contrast():
    tokens = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", CSS.read_text()))
    for ink in ("ink", "ink-2", "muted", "brand", "accent"):
        for surface in ("bg", "surface", "surface-2", "brand-soft"):
            light, dark = sorted((luminance(tokens[ink]), luminance(tokens[surface])), reverse=True)
            assert (light + .05) / (dark + .05) >= 4.5, (ink, surface)
    assert (1.0 + .05) / (luminance(tokens["brand"]) + .05) >= 4.5
    for status in ("ok", "warn", "danger", "info"):
        light, dark = sorted((luminance(tokens[status]), luminance(tokens[status + "-soft"])), reverse=True)
        assert (light + .05) / (dark + .05) >= 4.5, status
