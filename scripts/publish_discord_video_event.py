#!/usr/bin/env python3
"""Queue newly public, hash-approved video releases for central approval."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Statuses the central router's ingest handler returns for accepted or
# duplicate events: 200 once delivered, 202 otherwise.
_ACCEPTED_STATUSES = frozenset({200, 202})


class SubmissionError(ValueError):
    """The router did not durably accept the event; messages carry no secrets."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so an Access login page can never count as accepted."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _access_headers() -> dict[str, str]:
    client_id = os.environ.get("DISCORD_EVENT_ACCESS_CLIENT_ID", "").strip()
    client_secret = os.environ.get("DISCORD_EVENT_ACCESS_CLIENT_SECRET", "").strip()
    if not client_id and not client_secret:
        return {}
    if not client_id or not client_secret:
        raise SubmissionError("DISCORD_EVENT_ACCESS_CLIENT_ID and DISCORD_EVENT_ACCESS_CLIENT_SECRET must be set together")
    return {"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": client_secret}


def _check_receipt(status: int, content_type: str, raw: bytes, event_id: str) -> dict:
    """The single success rule for router submissions."""
    if status not in _ACCEPTED_STATUSES:
        raise SubmissionError(f"router did not accept the event: HTTP {status}")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise SubmissionError(f"router response is not JSON (HTTP {status})")
    try:
        receipt = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SubmissionError(f"router response is not valid JSON (HTTP {status})") from None
    if not isinstance(receipt, dict) or receipt.get("event_id") != event_id:
        raise SubmissionError(f"router receipt does not match the submitted event (HTTP {status})")
    return receipt


def public_video_digests(raw: str | bytes) -> set[str]:
    try:
        records = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("video publication manifest is invalid") from exc
    if not isinstance(records, list):
        raise ValueError("video publication manifest must be a list")
    digests: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("video publication record is invalid")
        if record.get("visibility") != "PUBLIC":
            continue
        digest, size = record.get("approved_sha256"), record.get("approved_size_bytes")
        filename = record.get("filename")
        if (
            not isinstance(filename, str) or not filename or "/" in filename or "\\" in filename
            or not isinstance(digest, str) or not _SHA256.fullmatch(digest)
            or not isinstance(size, int) or isinstance(size, bool) or size <= 0
        ):
            raise ValueError("public video record lacks a safe reviewed identity")
        digests.add(digest)
    return digests


def new_public_video_events(previous: str | bytes, current: str | bytes) -> list[dict[str, str]]:
    added = public_video_digests(current) - public_video_digests(previous)
    return [
        {
            "event_id": f"video-published-{digest[:32]}",
            "event_type": "video.published",
            "source_service": "video",
            "destination_channel": "tutorials",
            "title": "New OmniBioAI video tutorial published",
            "message": "A newly approved video tutorial is available in the public OmniBioAI library.",
            "correlation_id": f"video-published-{digest[:32]}",
        }
        for digest in sorted(added)
    ]


def _previous_manifest() -> str:
    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0", "HEAD^"],
        check=False, capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return "[]"
    tag = result.stdout.strip()
    if tag.startswith("-") or "\n" in tag:
        raise ValueError("previous video release tag is invalid")
    previous = subprocess.run(
        ["git", "show", f"{tag}:content/videos.json"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    return previous.stdout


def _submit(event: dict[str, str], *, base_url: str, secret: str, commit: str) -> dict:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Discord event router URL must be an HTTPS origin/path")
    if len(secret) < 43 or not secret.isascii() or not re.fullmatch(r"[A-Za-z0-9_-]+", secret):
        raise ValueError("Discord event source key is invalid")
    payload = {
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "source_service": "video",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "destination_channel": "tutorials",
        "approved_publication": False,
        "safe_message_payload": {
            "title": event["title"], "message": event["message"], "color": "info",
            "fields": {"Revision": commit[:12]},
        },
        "correlation_id": event["correlation_id"],
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    timestamp, nonce = str(int(time.time())), secrets.token_urlsafe(18)
    signature = hmac.new(secret.encode(), timestamp.encode() + b"\n" + nonce.encode() + b"\n" + body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json", "X-Omni-Discord-Source": "video",
        "X-Omni-Discord-Timestamp": timestamp, "X-Omni-Discord-Nonce": nonce,
        "X-Omni-Discord-Signature": "v1=" + signature,
    }
    headers.update(_access_headers())
    request = urllib.request.Request(
        base_url.rstrip("/") + "/integrations/discord/events", data=body, method="POST", headers=headers,
    )
    try:
        with _OPENER.open(request, timeout=8) as response:
            status, content_type, raw = response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise SubmissionError(f"router redirect refused: HTTP {exc.code}") from None
        raise SubmissionError(f"router rejected the event: HTTP {exc.code}") from None
    return _check_receipt(status, content_type, raw, event["event_id"])


def main() -> int:
    try:
        events = new_public_video_events(
            _previous_manifest(), Path("content/videos.json").read_text(encoding="utf-8"),
        )
        if not events:
            print("video_discord_events=none")
            return 0
        base_url = os.environ.get("DISCORD_EVENT_ROUTER_URL", "").strip()
        secret = os.environ.get("DISCORD_EVENT_SOURCE_SECRET", "")
        commit = os.environ.get("GITHUB_SHA", "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("GitHub commit identity is invalid")
        for event in events:
            _submit(event, base_url=base_url, secret=secret, commit=commit)
            print(json.dumps({
                "event_id": event["event_id"], "event_type": event["event_type"],
                "destination_channel": "tutorials", "delivery_status": "queued_for_approval",
            }, sort_keys=True))
    except SubmissionError as exc:
        print(f"video Discord event submission failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"video Discord event submission failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
