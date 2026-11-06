from __future__ import annotations

import importlib.util
import hashlib
import hmac
import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

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


def test_video_event_submission_is_signed_unapproved_and_uses_fixed_channel(monkeypatch):
    monkeypatch.delenv("DISCORD_EVENT_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("DISCORD_EVENT_ACCESS_CLIENT_SECRET", raising=False)
    event = MODULE.new_public_video_events("[]", json.dumps([item("new.mp4", "f")]))[0]
    with patch.object(MODULE._OPENER, "open", return_value=Response(202, {"event_id": event["event_id"]})) as opened:
        receipt = MODULE._submit(
            event, base_url="https://events.example/api", secret="K" * 43,
            commit="a" * 40,
        )
    assert receipt == {"event_id": event["event_id"]}
    request = opened.call_args.args[0]
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
    assert request.get_header("Cf-access-client-id") is None
    assert request.get_header("User-agent") == "omnibioai-discord-publisher/1.0"


def test_video_event_rejects_unsafe_config_without_network():
    event = MODULE.new_public_video_events("[]", json.dumps([item("new.mp4", "e")]))[0]
    with patch.object(MODULE._OPENER, "open") as opened:
        try:
            MODULE._submit(event, base_url="http://events.example", secret="K" * 43, commit="a" * 40)
        except ValueError:
            pass
        else:
            raise AssertionError("non-TLS router must be rejected")
        opened.assert_not_called()


class Response:
    """Fake router response for _OPENER.open."""

    def __init__(self, status, payload=None, content_type="application/json", raw=None):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._raw = raw if raw is not None else json.dumps(payload).encode()

    def __enter__(self): return self
    def __exit__(self, *_args): return None
    def read(self): return self._raw


ACCESS_ID, ACCESS_SECRET = "access-id-test-value", "access-secret-test-value"


def _event():
    return MODULE.new_public_video_events("[]", json.dumps([item("new.mp4", "9")]))[0]


def _send(event):
    return MODULE._submit(event, base_url="https://events.example", secret="K" * 43, commit="a" * 40)


def _assert_safe(message):
    for secret in ("K" * 43, ACCESS_ID, ACCESS_SECRET):
        assert secret not in message


def test_no_redirect_handler_refuses_every_redirect():
    assert MODULE._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://login.example") is None


def test_opener_installs_the_no_redirect_handler():
    assert any(isinstance(handler, MODULE._NoRedirect) for handler in MODULE._OPENER.handlers)


def test_redirect_to_login_page_is_a_failure(monkeypatch):
    monkeypatch.delenv("DISCORD_EVENT_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("DISCORD_EVENT_ACCESS_CLIENT_SECRET", raising=False)
    redirect = urllib.error.HTTPError("https://events.example", 302, "Found", {"Location": "https://login.example"}, None)
    with patch.object(MODULE._OPENER, "open", side_effect=redirect):
        with pytest.raises(MODULE.SubmissionError, match="redirect refused: HTTP 302") as exc:
            _send(_event())
    _assert_safe(str(exc.value))


@pytest.mark.parametrize("response, message", [
    (Response(200, content_type="text/html", raw=b"<html>Sign in</html>"), "not JSON"),
    (Response(202, {"delivery_status": "pending_approval"}), "does not match"),
    (Response(202, {"event_id": "video-published-other"}), "does not match"),
    (Response(202, raw=b"not json"), "not valid JSON"),
])
def test_unaccepted_responses_fail_with_safe_message(monkeypatch, response, message):
    monkeypatch.delenv("DISCORD_EVENT_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("DISCORD_EVENT_ACCESS_CLIENT_SECRET", raising=False)
    with patch.object(MODULE._OPENER, "open", return_value=response):
        with pytest.raises(MODULE.SubmissionError, match=message) as exc:
            _send(_event())
    _assert_safe(str(exc.value))


@pytest.mark.parametrize("status", [202, 200])
def test_matching_receipt_succeeds(monkeypatch, status):
    monkeypatch.delenv("DISCORD_EVENT_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("DISCORD_EVENT_ACCESS_CLIENT_SECRET", raising=False)
    event = _event()
    with patch.object(MODULE._OPENER, "open", return_value=Response(status, {"event_id": event["event_id"]})):
        assert _send(event) == {"event_id": event["event_id"]}


def test_access_headers_are_sent_only_when_both_are_set(monkeypatch):
    monkeypatch.setenv("DISCORD_EVENT_ACCESS_CLIENT_ID", ACCESS_ID)
    monkeypatch.setenv("DISCORD_EVENT_ACCESS_CLIENT_SECRET", ACCESS_SECRET)
    event = _event()
    with patch.object(MODULE._OPENER, "open", return_value=Response(202, {"event_id": event["event_id"]})) as opened:
        _send(event)
    request = opened.call_args.args[0]
    assert request.get_header("Cf-access-client-id") == ACCESS_ID
    assert request.get_header("Cf-access-client-secret") == ACCESS_SECRET


@pytest.mark.parametrize("name", ["DISCORD_EVENT_ACCESS_CLIENT_ID", "DISCORD_EVENT_ACCESS_CLIENT_SECRET"])
def test_half_configured_access_fails_before_any_request(monkeypatch, name):
    monkeypatch.delenv("DISCORD_EVENT_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("DISCORD_EVENT_ACCESS_CLIENT_SECRET", raising=False)
    monkeypatch.setenv(name, ACCESS_ID)
    with patch.object(MODULE._OPENER, "open") as opened:
        with pytest.raises(MODULE.SubmissionError, match="must be set together") as exc:
            _send(_event())
    opened.assert_not_called()
    _assert_safe(str(exc.value))
