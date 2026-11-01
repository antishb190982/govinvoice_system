import importlib.util
import io
from pathlib import Path
import struct
import wave

import pytest


def narration_module():
    server = Path(__file__).resolve().parents[1]
    source = server.parent / "scripts" / "narrate-training.py"
    if not source.exists():
        source = server / "scripts" / "narrate-training.py"
    spec = importlib.util.spec_from_file_location("training_narration_build", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_streaming_wav_placeholder_is_measured_from_actual_pcm_frames():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\x01\x00" * 44000)
    raw = bytearray(stream.getvalue())
    raw[4:8] = struct.pack("<I", 0xffffffff)
    raw[40:44] = struct.pack("<I", 0xffffffff)
    fixed, seconds = narration_module().canonical_wav(bytes(raw))
    assert seconds == 5.5
    with wave.open(io.BytesIO(fixed), "rb") as recording:
        assert recording.getnframes() == 44000
        assert recording.getnframes() / recording.getframerate() == 5.5


def test_empty_narration_is_rejected_instead_of_creating_a_silent_manual():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"")
    with pytest.raises(ValueError, match="empty"):
        narration_module().canonical_wav(stream.getvalue())


def test_standalone_repository_layout_is_supported(tmp_path):
    source = Path(narration_module().__file__)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    standalone = scripts / source.name
    standalone.write_text(source.read_text())
    spec = importlib.util.spec_from_file_location("standalone_narration", standalone)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.ROOT == tmp_path
    assert module.SOURCE == tmp_path / "docs/training-scripts.json"
