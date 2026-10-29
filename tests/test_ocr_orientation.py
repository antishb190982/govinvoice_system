"""Deterministic orientation checks, including engines without OSD support."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from ocr import engine


@pytest.mark.parametrize("angle", [90, 180, 270])
def test_orientation_uses_full_sample_before_sparse_fallback(monkeypatch, angle):
    osd = Mock(return_value=f"Rotate: {angle}\nOrientation confidence: 10.22\n")
    monkeypatch.setattr(engine, "_tesseract", lambda: SimpleNamespace(image_to_osd=osd))
    image = Image.new("RGB", (100, 200), "white")

    corrected, rotation = engine._fix_orientation(image)

    assert rotation == angle
    assert corrected.size == ((200, 100) if angle in (90, 270) else (100, 200))
    osd.assert_called_once_with(image, config="--psm 0 -c min_characters_to_try=50")


def test_sparse_page_retries_with_smaller_sample(monkeypatch):
    osd = Mock(side_effect=[
        RuntimeError("Too few characters"),
        "Rotate: 90\nOrientation confidence: 2.00\n",
    ])
    monkeypatch.setattr(engine, "_tesseract", lambda: SimpleNamespace(image_to_osd=osd))
    image = Image.new("RGB", (100, 200), "white")

    corrected, rotation = engine._fix_orientation(image)

    assert rotation == 90 and corrected.size == (200, 100)
    assert osd.call_count == 2
    assert osd.call_args.kwargs["config"].endswith("min_characters_to_try=10")


@pytest.mark.parametrize("result", [
    "Rotate: 90\nOrientation confidence: 0.92\n",
    "Rotate: 45\nOrientation confidence: 10.00\n",
    "Rotate: 90\n",
    "Unexpected engine output",
])
def test_unreliable_orientation_leaves_image_unchanged(monkeypatch, result):
    osd = Mock(return_value=result)
    monkeypatch.setattr(engine, "_tesseract", lambda: SimpleNamespace(image_to_osd=osd))
    image = Image.new("RGB", (100, 200), "white")

    corrected, rotation = engine._fix_orientation(image)

    assert corrected is image and rotation == 0


def test_upright_page_is_not_rotated(monkeypatch):
    osd = Mock(return_value="Rotate: 0\nOrientation confidence: 10.00\n")
    monkeypatch.setattr(engine, "_tesseract", lambda: SimpleNamespace(image_to_osd=osd))
    image = Image.new("RGB", (100, 200), "white")

    corrected, rotation = engine._fix_orientation(image)

    assert corrected is image and rotation == 0
    assert osd.call_count == 1


def test_unavailable_orientation_engine_leaves_image_unchanged(monkeypatch):
    osd = Mock(side_effect=RuntimeError("OSD data unavailable"))
    monkeypatch.setattr(engine, "_tesseract", lambda: SimpleNamespace(image_to_osd=osd))
    image = Image.new("RGB", (100, 200), "white")

    corrected, rotation = engine._fix_orientation(image)

    assert corrected is image and rotation == 0
