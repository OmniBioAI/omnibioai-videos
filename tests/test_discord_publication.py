from __future__ import annotations

import importlib.util
import hashlib
import hmac
import json
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "scripts" / "publish_discord_video_event.py"
SPEC = importlib.util.spec_from_file_location("publish_discord_video_event", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def item(filename: str, digest: str, visibility: str = "PUBLIC") -> dict:
    return {
        "filename": filename,
        "visibility": visibility,
        "approved_sha256": digest * 64,
        "approved_size_bytes": 123,
    }


def test_only_newly_public_hash_approved_content_generates_safe_event():
    before = json.dumps([item("already.mp4", "a"), item("review.mp4", "b", "REVIEW_REQUIRED")])
    after = json.dumps([
        item("already.mp4", "a"),
        item("review.mp4", "b", "REVIEW_REQUIRED"),
        item("new_tutorial.mp4", "c"),
    ])
    events = MODULE.new_public_video_events(before, after)
    assert len(events) == 1
    assert events[0]["event_id"] == "video-published-" + "c" * 32
    assert events[0]["event_type"] == "video.published"
    assert events[0]["destination_channel"] == "tutorials"
    assert "new_tutorial" not in str(events[0])
    assert "approved_publication" not in events[0]


def test_metadata_changes_and_removals_do_not_announce_publication():
    before = json.dumps([item("same.mp4", "d")])
    metadata_only = json.dumps([{**item("same.mp4", "d"), "title": "changed title"}])
    removed = json.dumps([])
    assert MODULE.new_public_video_events(before, metadata_only) == []
    assert MODULE.new_public_video_events(before, removed) == []


def test_invalid_public_identity_fails_closed():
    for bad in (
        {"filename": "../x.mp4", "visibility": "PUBLIC", "approved_sha256": "e" * 64, "approved_size_bytes": 1},
        {"filename": "x.mp4", "visibility": "PUBLIC", "approved_sha256": "bad", "approved_size_bytes": 1},
        {"filename": "x.mp4", "visibility": "PUBLIC", "approved_sha256": "e" * 64, "approved_size_bytes": True},
    ):
        try:
            MODULE.public_video_digests(json.dumps([bad]))
        except ValueError:
            pass
        else:
            raise AssertionError("invalid public manifest identity must be rejected")


def test_manifest_must_be_a_list_of_records():
    for content in ('{}', '[null]', 'not json'):
        try:
            MODULE.public_video_digests(content)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid publication manifest must be rejected")


def test_video_event_submission_is_signed_unapproved_and_uses_fixed_channel():
    event = MODULE.new_public_video_events("[]", json.dumps([item("new.mp4", "f")]))[0]

    class Response:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *_args): return None

    with patch.object(MODULE.urllib.request, "urlopen", return_value=Response()) as urlopen:
        status = MODULE._submit(
            event, base_url="https://events.example/api", secret="K" * 43,
            commit="a" * 40,
        )
    assert status == 202
    request = urlopen.call_args.args[0]
    payload = json.loads(request.data)
    timestamp = request.get_header("X-omni-discord-timestamp")
    nonce = request.get_header("X-omni-discord-nonce")
    signature = request.get_header("X-omni-discord-signature")
    expected = hmac.new(
        ("K" * 43).encode(),
        timestamp.encode() + b"\n" + nonce.encode() + b"\n" + request.data,
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(signature, "v1=" + expected)
    assert payload["approved_publication"] is False
    assert payload["destination_channel"] == "tutorials"


def test_video_event_rejects_unsafe_config_without_network():
    event = MODULE.new_public_video_events("[]", json.dumps([item("new.mp4", "e")]))[0]
    with patch.object(MODULE.urllib.request, "urlopen") as urlopen:
        try:
            MODULE._submit(event, base_url="http://events.example", secret="K" * 43, commit="a" * 40)
        except ValueError:
            pass
        else:
            raise AssertionError("non-TLS router must be rejected")
        urlopen.assert_not_called()
