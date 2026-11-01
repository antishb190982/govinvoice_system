import hashlib
import io
import json
from unittest.mock import patch

import pytest
from django.urls import reverse

from accounts.middleware import OTP_SESSION_KEY
from core import training
from org.models import Role

pytestmark = pytest.mark.django_db
CONTENT = b"0123456789-demo-mp4"


def login(client, user):
    client.force_login(user)
    session = client.session
    session[OTP_SESSION_KEY] = True
    session.save()


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    from pathlib import Path
    source = Path(__file__).resolve().parent.parent / "docs/training-scripts.json"
    data = json.loads(source.read_text())
    manuals = []
    for role in data["roles"]:
        manuals.append({**role, "duration_seconds": 80,
            "chapters": [{**chapter, "start_seconds": index*20} for index, chapter in enumerate(role["chapters"])],
            "assets": {kind: {
                "name": f"training/test/{role['slug']}/{kind}", "size": len(CONTENT),
                "sha256": hashlib.sha256(CONTENT).hexdigest(), "content_type": mime,
            } for kind, mime in [("video", "video/mp4"), ("webm", "video/webm"),
                ("captions", "text/vtt"), ("poster", "image/jpeg")]}})
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"notice": data["notice"], "manuals": manuals}))
    monkeypatch.setattr(training, "CATALOG", path)
    return manuals


def test_library_requires_authentication(client, catalog):
    response = client.get(reverse("core:training"))
    assert response.status_code == 302 and "/accounts/login/" in response.url
    response = client.get(reverse("core:training_media", args=["clerk", "video"]))
    assert response.status_code == 302


def test_all_roles_have_a_real_walkthrough_and_readable_checklist(client, people, catalog):
    login(client, people["clerk"])
    response = client.get(reverse("core:training"))
    assert response.status_code == 200 and len(response.context["manuals"]) == 9
    expected = set(Role.values) | {"sysadmin"}
    assert {manual["slug"] for manual in response.context["manuals"]} == expected
    assert next(manual for manual in response.context["manuals"] if manual["slug"] == "clerk")["recommended"]
    for slug in expected:
        response = client.get(reverse("core:training_detail", args=[slug]))
        assert response.status_code == 200
        assert b"<video" in response.content and b"<track" in response.content
        assert b"Transcript and checklist" in response.content
        assert b"Download video (MP4)" in response.content
        assert len(response.context["manual"]["chapters"]) == 4


def test_role_filter_unknown_roles_and_media_names(client, people, catalog):
    login(client, people["clerk"])
    page = client.get(reverse("core:training"), {"role": "finance"})
    assert [manual["slug"] for manual in page.context["manuals"]] == ["finance"]
    assert client.get(reverse("core:training"), {"role": "invalid"}).status_code == 404
    assert client.get(reverse("core:training_detail", args=["invalid"])).status_code == 404
    assert client.get(reverse("core:training_media", args=["clerk", "private-path"])).status_code == 404


@pytest.mark.parametrize("header,expected", [
    ("bytes=0-3", (0, 3)), ("bytes=3-", (3, 9)), ("bytes=-3", (7, 9)),
    ("bytes=2-999", (2, 9)), ("bytes=-100", (0, 9)),
])
def test_native_video_byte_ranges(header, expected):
    assert training.byte_range(header, 10) == expected


@pytest.mark.parametrize("header", ["bytes=10-", "bytes=4-1", "bytes=-0", "bytes=-", "bytes=0-1,3-4", "foo"])
def test_invalid_video_ranges(header):
    with pytest.raises(ValueError):
        training.byte_range(header, 10)


def test_private_media_supports_seeking_downloads_and_head(client, people, catalog):
    login(client, people["clerk"])
    url = reverse("core:training_media", args=["clerk", "video"])
    with patch.object(training.PrivateAppStorage, "open", return_value=io.BytesIO(CONTENT)):
        response = client.get(url, HTTP_RANGE="bytes=2-6")
    assert response.status_code == 206 and response.content == CONTENT[2:7]
    assert response["Content-Range"] == f"bytes 2-6/{len(CONTENT)}"
    assert response["Content-Type"] == "video/mp4"
    # The portal may strengthen private caching to no-store for signed-in pages.
    assert any(value in response["Cache-Control"] for value in ("private", "no-store"))
    with patch.object(training.PrivateAppStorage, "open") as opened:
        response = client.head(url)
        assert response.status_code == 200 and response.content == b""
        opened.assert_not_called()
        response = client.get(url, HTTP_RANGE="bytes=999-")
        assert response.status_code == 416
        opened.assert_not_called()
    with patch.object(training.PrivateAppStorage, "open", return_value=io.BytesIO(CONTENT)):
        response = client.get(url, {"download": "1"})
    assert response.status_code == 200 and "attachment" in response["Content-Disposition"]
    with patch.object(training.PrivateAppStorage, "open", return_value=io.BytesIO(CONTENT)):
        response = client.get(reverse("core:training_media", args=["clerk", "webm"]),
            HTTP_RANGE="bytes=0-3")
    assert response.status_code == 206 and response["Content-Type"] == "video/webm"


def test_storage_failure_keeps_transcript_accessible(client, people, catalog):
    login(client, people["clerk"])
    with patch.object(training.PrivateAppStorage, "open", side_effect=OSError("unavailable")):
        response = client.get(reverse("core:training_media", args=["clerk", "video"]))
    assert response.status_code == 503 and b"written manual" in response.content
    assert client.get(reverse("core:training_detail", args=["clerk"])).status_code == 200


def test_same_length_corrupted_asset_is_not_served(client, people, catalog):
    login(client, people["clerk"])
    with patch.object(training.PrivateAppStorage, "open", return_value=io.BytesIO(b"X" * len(CONTENT))):
        response = client.get(reverse("core:training_media", args=["clerk", "video"]))
    assert response.status_code == 503


def test_narrated_manuals_are_labelled_without_false_playback_errors(client, people, catalog, tmp_path, monkeypatch):
    path = tmp_path / "narrated.json"
    for manual in catalog:
        manual["narrated"] = True
    path.write_text(json.dumps({"notice": "AI-generated English narration", "manuals": catalog}))
    monkeypatch.setattr(training, "CATALOG", path)
    login(client, people["clerk"])
    library = client.get(reverse("core:training"))
    assert b"voice and captions" in library.content and b"no narration" not in library.content
    detail = client.get(reverse("core:training_detail", args=["clerk"]))
    assert b"Narrated walkthrough" in detail.content and b"volume control" in detail.content
    assert b"Your browser cannot play" not in detail.content
