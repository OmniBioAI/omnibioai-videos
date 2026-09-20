"""Publication-boundary tests for the public video portal (videos.omnibioai.org).

Model under test: the video files stay on the host and are mounted READ-ONLY into a hardened nginx
container. ``videos.json`` is the publication control plane; ``visibility == "PUBLIC"`` (plus a matching
``approved_sha256``/``approved_size_bytes``) controls BOTH catalog discovery and direct media access via
an exact-match nginx allowlist, so possession of the mount never makes a file web-accessible.

Covers the fail-closed visibility model and hash gate in ``scripts/build_public.py``, the strict
``nginx.public.conf`` / ``Dockerfile.public`` / compose / runtime-publisher contract, the cloudflared
ingress retarget helper, and -- via Docker -- what an anonymous client actually sees, including a file
being replaced, truncated, symlinked or deleted on the host AFTER deployment. Synthetic sentinel secrets
are injected into the build environment and searched for in the bundle, the image, the container
configuration and every HTTP response. Nothing here touches the real content directory.

Developer: Manish Kumar <manish@omnibioai.org>
"""

import functools
import hashlib
import http.client
import http.server
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


bp = _load("build_public", "scripts/build_public.py")
ri = _load("retarget_ingress", "scripts/retarget_ingress.py")
vd = _load("verify_public_deploy", "scripts/verify_public_deploy.py")
tb = _load("tunnel_baseline", "scripts/tunnel_baseline.py")

# Synthetic secrets: exact values are searched for everywhere the public can reach.
SENTINELS = {
    "VITE_API_KEY": "sentinel-vite-key-7c1e9d20-must-never-ship",
    "VITE_IAM_TOKEN": "sentinel-vite-iam-3b8f5a41-must-never-ship",
    "API_TOKEN": "sentinel-api-token-91d2e6c7-must-never-ship",
    "AWS_SECRET_ACCESS_KEY": "sentinel-aws-secret-5e4a7b13-must-never-ship",
    "ADMIN_SESSION": "sentinel-admin-session-a02c8f96-must-never-ship",
}


def make_mp4(payload_len=300_000, seed=b"public"):
    """A structurally valid (ftyp + mdat + moov) MP4-shaped file with deterministic payload bytes."""
    payload = (hashlib.sha256(seed).digest() * (payload_len // 32 + 1))[:payload_len]
    ftyp = struct.pack(">I4s4sI4s4s", 24, b"ftyp", b"isom", 512, b"isom", b"mp41")
    mdat = struct.pack(">I4s", 8 + payload_len, b"mdat") + payload
    moov = struct.pack(">I4s", 8, b"moov")
    return ftyp + mdat + moov


PUBLIC_BYTES = make_mp4()
NON_PUBLIC = {
    # filename: visibility as written in the manifest (None = key omitted, "null" = JSON null)
    "internal.mp4": "INTERNAL",
    "review.mp4": "REVIEW_REQUIRED",
    "novis.mp4": None,  # key omitted entirely
    "lowercase.mp4": "public",
    "typo.mp4": "PUBLIC ",
    "nullvis.mp4": "null",  # JSON null
}
UNREGISTERED = "unregistered.mp4"
DIRECT_LEAK_FILES = ["guide.html", ".env", "internal-index.html"]


def approval(data):
    return {"approved_sha256": hashlib.sha256(data).hexdigest(), "approved_size_bytes": len(data)}


def _entry(filename, visibility, **extra):
    entry = {
        "filename": filename,
        "title": f"{filename}-TITLE-SENTINEL",
        "desc": f"{filename}-DESC-SENTINEL",
        "tag": "demo",
        "order": 1,
    }
    if visibility is not None:
        entry["visibility"] = None if visibility == "null" else visibility
    if visibility == "PUBLIC":
        entry.update(approval(PUBLIC_BYTES))  # approval is pinned to the exact reviewed bytes
    entry.update(extra)
    return entry


def make_content(base):
    """A host content directory: one approved PUBLIC video plus every kind of non-public content."""
    d = Path(base) / "content"
    d.mkdir()
    manifest = [_entry("pub.mp4", "PUBLIC", internal_path="/srv/private/pub.mp4", notes="NOTES-SENTINEL")]
    (d / "pub.mp4").write_bytes(PUBLIC_BYTES)
    for name, visibility in NON_PUBLIC.items():
        manifest.append(_entry(name, visibility))
        (d / name).write_bytes(f"{name}-MEDIA-SENTINEL".encode() * 50)
    (d / UNREGISTERED).write_bytes(b"UNREGISTERED-MEDIA-SENTINEL" * 50)
    for name in DIRECT_LEAK_FILES:
        (d / name).write_text(f"{name}-SENTINEL")
    (d / "videos.json").write_text(json.dumps(manifest))
    return d


@pytest.fixture
def content_dir(tmp_path):
    return make_content(tmp_path)


@pytest.fixture
def portal_dir():
    return ROOT / "portal"


def _walk_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _write_manifest(content_dir, entries):
    (content_dir / "videos.json").write_text(json.dumps(entries))


BUNDLE_FILES = {
    "www/index.html", "www/portal.css", "www/portal.js",
    "publication/approved.tsv", "publication/entries/001.json", "publication/entries/001.conf",
}


# ── Visibility classification: fail closed ────────────────────────────────────


@pytest.mark.parametrize("value", bp.VISIBILITIES)
def test_classify_accepts_only_the_three_exact_values(value):
    assert bp.classify({"visibility": value}) == value


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"visibility": None},
        {"visibility": ""},
        {"visibility": "public"},
        {"visibility": "Public"},
        {"visibility": " PUBLIC"},
        {"visibility": "PUBLIC "},
        {"visibility": "PUBLIC\n"},
        {"visibility": "PUBLlC"},
        {"visibility": "PRIVATE"},
        {"visibility": 1},
        {"visibility": True},
        {"visibility": ["PUBLIC"]},
        {"visibility": {"PUBLIC": True}},
        None,
        "PUBLIC",
        5,
        [],
    ],
)
def test_classify_missing_or_unknown_visibility_is_unclassified(entry):
    assert bp.classify(entry) == bp.UNCLASSIFIED


# ── Publication artifacts ────────────────────────────────────────────────────


def test_build_publishes_only_explicit_public_content_and_copies_no_media(content_dir, portal_dir, tmp_path):
    out = tmp_path / "out"
    report = bp.build(content_dir, portal_dir, out)

    files = _walk_bytes(out)
    assert set(files) == BUNDLE_FILES
    assert not [n for n in files if n.lower().endswith(bp.MEDIA_SUFFIXES)]  # the video stays on the host
    sha, size = approval(PUBLIC_BYTES).values()
    assert files["publication/approved.tsv"].decode() == f"001\t{sha}\t{size}\tpub.mp4\n"
    assert files["publication/entries/001.conf"].decode() == bp.location_block("pub.mp4")
    assert report["published"] == ["pub.mp4"]
    assert report["approved"] == {"pub.mp4": {"sha256": sha, "size": size}}
    assert report["counts"] == {"PUBLIC": 1, "INTERNAL": 1, "REVIEW_REQUIRED": 1, "UNCLASSIFIED": 4}
    assert report["unregistered_media"] == [UNREGISTERED]

    blob = b"\n".join(files.values())
    for leaked in (
        b"internal.mp4", b"review.mp4", b"novis.mp4", b"lowercase.mp4", b"typo.mp4", b"nullvis.mp4",
        UNREGISTERED.encode(), b"GUIDE-SENTINEL", b"ENV-FILE-SENTINEL", b"INTERNAL-INDEX-SENTINEL",
        b"NOTES-SENTINEL", b"/srv/private", b"REVIEW_REQUIRED", b"INTERNAL",
    ):
        assert leaked not in blob, leaked
    for name in [*NON_PUBLIC, UNREGISTERED]:  # restricted titles/descriptions never reach the catalog
        assert f"{name}-TITLE-SENTINEL".encode() not in blob
        assert f"{name}-DESC-SENTINEL".encode() not in blob


def test_nginx_allowlist_contains_exactly_the_public_filenames(content_dir, portal_dir, tmp_path):
    out = tmp_path / "out"
    bp.build(content_dir, portal_dir, out)
    conf = (out / "publication" / "entries" / "001.conf").read_text()
    assert conf.count("location = ") == 1 and "location = /videos/pub.mp4 {" in conf
    assert "alias /content/pub.mp4;" in conf and "disable_symlinks on;" in conf
    assert "alias /content/;" not in conf and "autoindex" not in conf and "try_files" not in conf


def test_public_catalog_record_is_an_allowlisted_projection(content_dir, portal_dir, tmp_path):
    out = tmp_path / "out"
    bp.build(content_dir, portal_dir, out)
    record = json.loads((out / "publication" / "entries" / "001.json").read_text())
    assert set(record) == set(bp.PUBLIC_FIELDS)  # no visibility, approved_*, internal_path or notes


def test_media_can_live_in_a_different_host_directory_than_the_manifest(content_dir, portal_dir, tmp_path):
    media = tmp_path / "host-media"
    media.mkdir()
    shutil.move(content_dir / "pub.mp4", media / "pub.mp4")
    out = tmp_path / "out"
    assert bp.build(content_dir, portal_dir, out, media_dir=media)["published"] == ["pub.mp4"]
    (media / "pub.mp4").unlink()
    with pytest.raises(bp.PublishError, match="missing or is not a regular file"):
        bp.build(content_dir, portal_dir, out, media_dir=media)


def test_build_recreates_output_dir_so_stale_files_cannot_linger(content_dir, portal_dir, tmp_path):
    out = tmp_path / "out"
    (out / "publication" / "entries").mkdir(parents=True)
    (out / "publication" / "entries" / "099.conf").write_text("location = /videos/stale.mp4 {}")
    bp.build(content_dir, portal_dir, out)
    assert not (out / "publication" / "entries" / "099.conf").exists()


def test_build_with_no_public_entries_yields_an_empty_allowlist(content_dir, portal_dir, tmp_path):
    manifest = json.loads((content_dir / "videos.json").read_text())
    for entry in manifest:
        entry["visibility"] = "REVIEW_REQUIRED"
    _write_manifest(content_dir, manifest)
    out = tmp_path / "out"
    report = bp.build(content_dir, portal_dir, out)
    assert report["published"] == []
    assert (out / "publication" / "approved.tsv").read_text() == ""
    assert list((out / "publication" / "entries").iterdir()) == []
    assert (out / "www" / "index.html").is_file()


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda e: e.update(filename="../pub.mp4"), "invalid filename"),
        (lambda e: e.update(filename="sub/pub.mp4"), "invalid filename"),
        (lambda e: e.update(filename="pub.mp4\n"), "invalid filename"),
        (lambda e: e.update(filename=".hidden.mp4"), "invalid filename"),
        (lambda e: e.update(filename="pub.exe"), "invalid filename"),
        (lambda e: e.update(filename="pub.mov"), "invalid filename"),
        (lambda e: e.update(filename=None), "invalid filename"),
        (lambda e: e.update(filename="absent.mp4"), "missing or is not a regular file"),
        (lambda e: e.update(title=""), "non-empty 'title'"),
        (lambda e: e.pop("desc"), "non-empty 'desc'"),
        (lambda e: e.update(tag="secret-tag"), "tag must be one of"),
        (lambda e: e.update(order="1"), "'order' must be an integer"),
        (lambda e: e.update(order=True), "'order' must be an integer"),
        (lambda e: e.update(desc="see /home/manish/Desktop/machine/data"), "forbidden content"),
        (lambda e: e.update(title="key sk-ant-api03-abcdefghijkl"), "forbidden content"),
        (lambda e: e.update(desc="host 192.168.86.234"), "forbidden content"),
        (lambda e: e.pop("approved_sha256"), "needs the lowercase hex 'approved_sha256'"),
        (lambda e: e.update(approved_sha256=""), "needs the lowercase hex 'approved_sha256'"),
        (lambda e: e.update(approved_sha256="abc"), "needs the lowercase hex 'approved_sha256'"),
        (lambda e: e.update(approved_sha256="A" * 64), "needs the lowercase hex 'approved_sha256'"),
        (lambda e: e.update(approved_sha256=12345), "needs the lowercase hex 'approved_sha256'"),
        (lambda e: e.pop("approved_size_bytes"), "positive integer 'approved_size_bytes'"),
        (lambda e: e.update(approved_size_bytes=0), "positive integer 'approved_size_bytes'"),
        (lambda e: e.update(approved_size_bytes=-5), "positive integer 'approved_size_bytes'"),
        (lambda e: e.update(approved_size_bytes="5"), "positive integer 'approved_size_bytes'"),
        (lambda e: e.update(approved_size_bytes=True), "positive integer 'approved_size_bytes'"),
    ],
)
def test_malformed_public_entry_aborts_the_build(content_dir, portal_dir, tmp_path, mutation, message):
    entry = _entry("pub.mp4", "PUBLIC")
    mutation(entry)
    _write_manifest(content_dir, [entry])
    with pytest.raises(bp.PublishError, match=message):
        bp.build(content_dir, portal_dir, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_file_replaced_after_approval_cannot_be_published(content_dir, portal_dir, tmp_path):
    """Approval is bound to content: same name, same size, different bytes -> hash mismatch, build aborts."""
    replacement = make_mp4(seed=b"a different, unreviewed recording")
    assert len(replacement) == len(PUBLIC_BYTES) and replacement != PUBLIC_BYTES
    (content_dir / "pub.mp4").write_bytes(replacement)
    with pytest.raises(bp.PublishError, match="sha256 mismatch"):
        bp.build(content_dir, portal_dir, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_file_with_a_different_size_than_approved_aborts_the_build(content_dir, portal_dir, tmp_path):
    (content_dir / "pub.mp4").write_bytes(make_mp4(payload_len=len(PUBLIC_BYTES)))  # different size
    with pytest.raises(bp.PublishError, match="size mismatch"):
        bp.build(content_dir, portal_dir, tmp_path / "out")


@pytest.mark.parametrize(
    "data, message",
    [
        (PUBLIC_BYTES[:-5], "truncated box header"),  # partial write: cut inside the trailing moov header
        (PUBLIC_BYTES[:-1000], "overruns the file"),  # partial write: cut inside the mdat payload
        (b"x" * 100, "not a valid MP4"),  # not a video container at all
        (PUBLIC_BYTES[24:], "no leading ftyp box"),  # ftyp missing
        (PUBLIC_BYTES[:-8], "missing moov or mdat"),  # never finalised: no moov
    ],
)
def test_approved_but_structurally_invalid_mp4_aborts_the_build(content_dir, portal_dir, tmp_path, data, message):
    """Even with a matching approved hash a non-video / truncated file is never published."""
    (content_dir / "pub.mp4").write_bytes(data)
    _write_manifest(content_dir, [_entry("pub.mp4", "PUBLIC", **approval(data))])
    with pytest.raises(bp.PublishError, match=message):
        bp.build(content_dir, portal_dir, tmp_path / "out")


def _box64(kind, payload):
    return struct.pack(">I4sQ", 1, kind, 16 + len(payload)) + payload


@pytest.mark.parametrize(
    "data",
    [
        make_mp4(),
        struct.pack(">I4s4sI4s4s", 24, b"ftyp", b"isom", 0, b"isom", b"mp41") + _box64(b"mdat", b"x" * 50)
        + struct.pack(">I4s", 8, b"moov"),  # 64-bit box size
        struct.pack(">I4s4sI4s4s", 24, b"ftyp", b"isom", 0, b"isom", b"mp41") + struct.pack(">I4s", 8, b"moov")
        + struct.pack(">I4s", 0, b"mdat") + b"x" * 50,  # size 0: box extends to end of file
    ],
)
def test_check_container_accepts_valid_mp4_layouts(tmp_path, data):
    path = tmp_path / "v.mp4"
    path.write_bytes(data)
    bp.check_container(path, "v.mp4")


def test_check_container_rejects_a_truncated_64_bit_box(tmp_path):
    path = tmp_path / "v.mp4"
    path.write_bytes(make_mp4()[:24] + struct.pack(">I4s", 1, b"mdat") + b"\x00\x00\x00")
    with pytest.raises(bp.PublishError, match="truncated 64-bit box"):
        bp.check_container(path, "v.mp4")


def test_webm_needs_the_ebml_header(content_dir, portal_dir, tmp_path):
    good = b"\x1a\x45\xdf\xa3" + b"webm-payload" * 100
    (content_dir / "pub.webm").write_bytes(good)
    _write_manifest(content_dir, [_entry("pub.webm", "PUBLIC", **approval(good))])
    out = tmp_path / "ok"
    assert bp.build(content_dir, portal_dir, out)["published"] == ["pub.webm"]
    assert "video/webm" in (out / "publication" / "entries" / "001.conf").read_text()

    bad = b"RIFF" + b"not-a-webm" * 100
    (content_dir / "pub.webm").write_bytes(bad)
    _write_manifest(content_dir, [_entry("pub.webm", "PUBLIC", **approval(bad))])
    with pytest.raises(bp.PublishError, match="not a valid WebM"):
        bp.build(content_dir, portal_dir, tmp_path / "bad")


def test_empty_media_file_aborts_the_build(content_dir, portal_dir, tmp_path):
    (content_dir / "pub.mp4").write_bytes(b"")
    with pytest.raises(bp.PublishError, match="empty"):
        bp.build(content_dir, portal_dir, tmp_path / "out")


def test_zero_byte_placeholder_can_never_be_public(content_dir, portal_dir, tmp_path):
    """Even if someone flips a 0-byte placeholder to PUBLIC with a plausible-looking approval."""
    (content_dir / "placeholder.mp4").write_bytes(b"")
    _write_manifest(content_dir, [_entry("placeholder.mp4", "PUBLIC")])
    with pytest.raises(bp.PublishError, match="empty"):
        bp.build(content_dir, portal_dir, tmp_path / "out")


def test_symlinked_media_aborts_the_build(content_dir, portal_dir, tmp_path):
    secret = tmp_path / "outside-secret.mp4"
    secret.write_bytes(PUBLIC_BYTES)
    (content_dir / "pub.mp4").unlink()
    (content_dir / "pub.mp4").symlink_to(secret)
    with pytest.raises(bp.PublishError, match="not a regular file"):
        bp.build(content_dir, portal_dir, tmp_path / "out")


def test_duplicate_public_filename_aborts_the_build(content_dir, portal_dir, tmp_path):
    _write_manifest(content_dir, [_entry("pub.mp4", "PUBLIC"), _entry("pub.mp4", "PUBLIC")])
    with pytest.raises(bp.PublishError, match="duplicate"):
        bp.build(content_dir, portal_dir, tmp_path / "out")


@pytest.mark.parametrize("payload, message", [("{}", "JSON array"), ("[not json", "not valid JSON")])
def test_bad_manifest_aborts_the_build(content_dir, portal_dir, tmp_path, payload, message):
    (content_dir / "videos.json").write_text(payload)
    with pytest.raises(bp.PublishError, match=message):
        bp.build(content_dir, portal_dir, tmp_path / "out")


def test_missing_manifest_aborts_the_build(tmp_path, portal_dir):
    (tmp_path / "content").mkdir()
    with pytest.raises(bp.PublishError, match="manifest not found"):
        bp.build(tmp_path / "content", portal_dir, tmp_path / "out")


def test_missing_portal_file_aborts_the_build(content_dir, tmp_path):
    (tmp_path / "portal").mkdir()
    with pytest.raises(bp.PublishError, match="portal file missing"):
        bp.build(content_dir, tmp_path / "portal", tmp_path / "out")


def test_bundle_scan_failure_aborts_and_removes_output(content_dir, tmp_path):
    portal = tmp_path / "portal"
    shutil.copytree(ROOT / "portal", portal)
    (portal / "portal.js").write_text("const k = 'VITE_API_KEY';")
    out = tmp_path / "out"
    with pytest.raises(bp.PublishError, match="vite-env"):
        bp.build(content_dir, portal, out)
    assert not out.exists()


def test_cli_reports_failure_without_publishing(content_dir, tmp_path, capsys):
    (content_dir / "videos.json").write_text("[not json")
    out = tmp_path / "out"
    code = bp.main(["--content", str(content_dir), "--portal", str(ROOT / "portal"), "--out", str(out)])
    assert code == 1
    assert "BUILD FAILED (nothing published)" in capsys.readouterr().err
    assert not out.exists()


def test_cli_prints_inventory_and_approval(content_dir, tmp_path, capsys):
    out = tmp_path / "out"
    code = bp.main(["--content", str(content_dir), "--portal", str(ROOT / "portal"), "--out", str(out),
                    "--media-dir", str(content_dir)])
    stdout = capsys.readouterr().out
    assert code == 0
    assert "artifacts for 1 video(s)" in stdout and "no media copied" in stdout
    assert f"sha256={approval(PUBLIC_BYTES)['approved_sha256']}" in stdout
    assert f"unregistered media (NOT published): {UNREGISTERED}" in stdout


# ── Secret / internal-detail scanner ─────────────────────────────────────────


@pytest.mark.parametrize(
    "text, pattern",
    [
        ("sk-ant-api03-abcdefghij", "anthropic-key"),
        ("sk-abcdefghijklmnopqrstuvwx", "openai-style-key"),
        ("AKIAABCDEFGHIJKLMNOP", "aws-access-key-id"),
        ("ghp_" + "a1B2c3D4e5" * 3, "github-token"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private-key-block"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc", "jwt"),
        ("import.meta.env.VITE_API_KEY", "vite-env"),
        ("Authorization: Bearer abcdefghijklmnop", "bearer-token"),
        ("api_key = 'abcd1234efgh'", "credential-assignment"),
        ("/home/manish/Desktop/machine/data", "home-path"),
        ("/Users/someone/project", "home-path"),
        ("192.168.86.234", "private-ip"),
        ("10.0.0.5", "private-ip"),
        ("172.20.1.9", "private-ip"),
        ("visibility: REVIEW_REQUIRED", "visibility-marker"),
        ("INTERNAL notes", "visibility-marker"),
    ],
)
def test_scan_text_detects_forbidden_content(text, pattern):
    assert pattern in bp.scan_text(text)


def test_scan_text_passes_ordinary_public_copy():
    assert bp.scan_text("Complete walkthrough of the platform: setup, configuration and first run.") == []


def test_scan_tree_flags_source_maps_dotfiles_and_secret_files(tmp_path):
    for name in ("app.js.map", ".env", "prod.env", "server.pem", "id.key"):
        (tmp_path / name).write_text("harmless")
    (tmp_path / "ok.js").write_text("console.log('hi')")
    flagged = {path for path, _ in bp.scan_tree(tmp_path)}
    assert flagged == {"app.js.map", ".env", "prod.env", "server.pem", "id.key"}


def test_scan_tree_flags_any_media_file_in_the_bundle_without_reading_it(tmp_path):
    for name in ("v.mp4", "v.webm", "v.mov"):
        (tmp_path / name).write_bytes(b"binary")
    assert {(p, why) for p, why in bp.scan_tree(tmp_path)} == {
        ("v.mp4", "media-file-in-bundle"), ("v.webm", "media-file-in-bundle"), ("v.mov", "media-file-in-bundle")}


# ── The real repo: manifest, portal assets ───────────────────────────────────


def test_repo_manifest_every_entry_has_an_explicit_valid_visibility():
    manifest = json.loads((ROOT / "content" / "videos.json").read_text())
    assert manifest, "manifest unexpectedly empty"
    bad = [e.get("filename") for e in manifest if bp.classify(e) == bp.UNCLASSIFIED]
    assert bad == [], f"entries without an explicit visibility (would be withheld): {bad}"


def test_repo_public_entries_carry_a_valid_content_approval():
    manifest = json.loads((ROOT / "content" / "videos.json").read_text())
    public = [e for e in manifest if e.get("visibility") == "PUBLIC"]
    assert [e["filename"] for e in public] == ["intro_getting_started.mp4"]  # the one approved video
    for entry in public:
        assert bp.SHA256_RE.fullmatch(entry["approved_sha256"])
        assert isinstance(entry["approved_size_bytes"], int) and entry["approved_size_bytes"] > 0
    unapproved = [e["filename"] for e in manifest if e.get("visibility") != "PUBLIC"]
    assert len(unapproved) == 9 and all(e.get("visibility") == "REVIEW_REQUIRED" for e in manifest if e["filename"] in unapproved)


def test_real_manifest_builds_against_the_real_host_media_when_present(tmp_path):
    """On a machine holding the approved file the real manifest must build; elsewhere this is skipped."""
    manifest = json.loads((ROOT / "content" / "videos.json").read_text())
    public = [e for e in manifest if e.get("visibility") == "PUBLIC"]
    for entry in public:
        media = ROOT / "content" / entry["filename"]
        if not media.is_file() or media.stat().st_size != entry["approved_size_bytes"] \
                or bp.sha256_file(media) != entry["approved_sha256"]:
            pytest.skip("the approved media bytes are not present on this machine")
    out = tmp_path / "out"
    report = bp.build(ROOT / "content", ROOT / "portal", out)
    assert report["published"] == [e["filename"] for e in public]
    assert bp.scan_tree(out) == []
    assert not [p for p in out.rglob("*") if p.suffix.lower() in bp.MEDIA_SUFFIXES]


def test_repo_portal_assets_contain_no_secret_material():
    assert bp.scan_tree(ROOT / "portal") == []


def test_portal_is_read_only_and_credential_free():
    js = (ROOT / "portal" / "portal.js").read_text()
    html = (ROOT / "portal" / "index.html").read_text()
    css = (ROOT / "portal" / "portal.css").read_text()
    for forbidden in (
        "innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write",
        "localStorage", "sessionStorage", "document.cookie", "Authorization", "XMLHttpRequest",
        "WebSocket", "sendBeacon", "method:", "'POST'", '"POST"', "upload", "delete", "admin",
    ):
        assert forbidden not in js, forbidden
    assert js.count("fetch(") == 1 and "fetch('/videos.json'" in js
    assert "credentials: 'omit'" in js
    for source in (html, js, css):
        assert "http://" not in source and "https://" not in source
    # nothing that a strict CSP (no unsafe-inline) would block or that hides behaviour in markup
    assert "onclick=" not in html and " style=" not in html
    assert html.count("<script") == 1 and 'src="/portal.js"' in html
    assert '<link rel="icon" href="data:,">' in html  # no /favicon.ico request -> no 404 noise in the console


def test_empty_catalog_says_videos_coming_soon_not_a_search_failure():
    js = (ROOT / "portal" / "portal.js").read_text()
    body = js[js.index("function showComingSoon()"):js.index("function applyFilters()")]
    assert "Videos coming soon" in body and "COMING SOON" in body
    for misleading in ("No videos found", "search", "filter", "Try a different"):
        assert misleading not in body
    assert "showComingSoon();" in js[js.index("function load()"):]  # empty catalog routes here
    assert 'id="controls"' in (ROOT / "portal" / "index.html").read_text()
    assert "[hidden]" in (ROOT / "portal" / "portal.css").read_text()  # .controls is display:flex


# ── Static server contract (no Docker) ──────────────────────────────────────


def _code(path):
    return "\n".join(l for l in (ROOT / path).read_text().splitlines() if not l.strip().startswith("#"))


def test_public_nginx_conf_is_strict_and_has_no_generic_media_alias():
    code = _code("nginx.public.conf")
    assert "listen 8087;" in code and "server_tokens off;" in code and "autoindex off;" in code
    for forbidden in (
        "autoindex on", "Access-Control-Allow-Origin", "proxy_pass", "fastcgi_pass", "uwsgi_pass",
        "upstream", "$uri/ /index.html", "dav_methods", "'unsafe-inline'", "'unsafe-eval'",
    ):
        assert forbidden not in code, forbidden
    assert "/content" not in code, "the content directory must only ever appear in generated exact-match locations"
    assert "GET|HEAD" in code and "return 405" in code
    assert "include /run/publication/media.conf;" in code and "alias /run/publication/videos.json;" in code
    assert "location ^~ /videos/ {\n        return 404;\n    }" in code  # unknown /videos/* -> 404
    assert code.rstrip().endswith("location / {\n        return 404;\n    }\n}")
    assert "default-src 'none'" in code and "frame-ancestors 'none'" in code


def test_public_dockerfile_packages_no_media_and_no_content():
    lines = [l.strip() for l in (ROOT / "Dockerfile.public").read_text().splitlines()
             if l.strip() and not l.strip().startswith("#")]
    copies = [l for l in lines if l.startswith(("COPY", "ADD"))]
    assert copies == [
        "COPY nginx.public.conf /etc/nginx/conf.d/default.conf",
        "COPY dist/public/www/ /usr/share/nginx/html/",
        "COPY dist/public/publication/ /etc/publication/",
        "COPY runtime/publish-runtime.sh /usr/local/bin/publish-runtime.sh",
    ]
    assert not any(l.startswith(("ARG", "ENV")) for l in lines)  # no build-time secret channel
    assert 'ENTRYPOINT ["/usr/local/bin/publish-runtime.sh"]' in lines
    assert "STOPSIGNAL SIGTERM" in lines  # the nginx base image's SIGQUIT would be ignored by the shell entrypoint


def test_public_build_context_is_an_allowlist_that_admits_only_the_bundle():
    rules = [l.strip() for l in (ROOT / "Dockerfile.public.dockerignore").read_text().splitlines()
             if l.strip() and not l.strip().startswith("#")]
    assert rules == ["*", "!Dockerfile.public", "!nginx.public.conf", "!runtime/publish-runtime.sh", "!dist/public"]
    assert "dist/" in (ROOT / ".gitignore").read_text().splitlines()  # a built bundle is never committed


def test_public_compose_is_loopback_only_with_one_read_only_content_mount():
    compose = yaml.safe_load((ROOT / "deploy" / "docker-compose.public.yml").read_text())
    (svc,) = compose["services"].values()
    assert svc["ports"] == ["127.0.0.1:8087:8087"]
    (volume,) = svc["volumes"]
    assert volume["type"] == "bind" and volume["target"] == "/content" and volume["read_only"] is True
    assert volume["source"].startswith("${VIDEOS_CONTENT_DIR:?")  # required: never defaults to some directory
    assert volume["bind"] == {"create_host_path": False}
    assert set(svc["environment"]) == {"PUBLISH_POLL_SECONDS", "PUBLISH_REHASH_SECONDS"}
    for key in ("env_file", "secrets", "privileged", "network_mode", "pid"):
        assert key not in svc, key
    assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert "/run:mode=0755" in svc["tmpfs"]


def test_runtime_publisher_checks_every_gate_before_publishing():
    script = (ROOT / "runtime" / "publish-runtime.sh").read_text()
    assert "set -u" in script and script.startswith("#!/bin/sh")
    for gate in ('[ -f "$f" ]', '[ ! -L "$f" ]', "stat -c %s", "sha256sum"):
        assert gate in script, gate
    for forbidden in ("eval ", "`", "curl", "wget", "$CONTENT_DIR/*", "for f in $CONTENT_DIR"):
        assert forbidden not in script, forbidden  # never enumerates or evals the mounted directory
    assert script.index("mv \"$GENERATED_DIR/media.conf.tmp\"") < script.index("mv \"$GENERATED_DIR/videos.json.tmp\"")


# ── Tunnel baseline tool ────────────────────────────────────────────────────


def test_tunnel_baseline_hostnames_and_compare():
    assert tb.hostnames(INGRESS)[:3] == ["omnibioai.org", "webstudio.omnibioai.org", "workbench.omnibioai.org"]
    assert "videos.omnibioai.org" in tb.hostnames(INGRESS) and len(tb.hostnames(INGRESS)) == 17
    ok = {"status": 200, "content_type": "text/html", "location": ""}
    base = {"hosts": {"a.example": {"/": ok, "/health": ok}, "videos.example": {"/": ok, "/health": ok}}}
    same = json.loads(json.dumps(base))
    assert tb.compare(base, same, set()) == []
    changed = json.loads(json.dumps(base))
    changed["hosts"]["a.example"]["/"]["status"] = 401
    changed["hosts"]["videos.example"]["/health"]["status"] = 404
    assert tb.compare(base, changed, {"videos.example"}) == ["a.example/: {'status': 200, 'content_type': 'text/html', 'location': ''} -> {'status': 401, 'content_type': 'text/html', 'location': ''}"]
    del changed["hosts"]["a.example"]
    assert "present in only one" in tb.compare(base, changed, {"videos.example"})[0]


def test_tunnel_baseline_probe_records_errors_as_state():
    assert tb.probe("nonexistent.invalid", "/")["status"].startswith("error:")


# ── cloudflared ingress: only the videos upstream may change ─────────────────

# Hostname -> upstream map of the production tunnel (LAN address sanitised).
INGRESS = """\
tunnel: omnibioai-beta
credentials-file: /etc/cloudflared/example.json

ingress:
  # ── Root & Workbench ──
  - hostname: omnibioai.org
    service: http://localhost:8000
  - hostname: webstudio.omnibioai.org
    service: http://localhost:80
  - hostname: workbench.omnibioai.org
    service: http://localhost:8000
  - hostname: api.omnibioai.org
    service: http://localhost:8080
  - hostname: lims.omnibioai.org
    service: http://localhost:7000
  - hostname: rag.omnibioai.org
    service: http://localhost:8090
  - hostname: models.omnibioai.org
    service: http://localhost:8095
  - hostname: bundles.omnibioai.org
    service: http://localhost:8098
  - hostname: videos.omnibioai.org
    service: http://localhost:8086
  - hostname: dev.omnibioai.org
    service: http://localhost:8082
  - hostname: sdk.omnibioai.org
    service: http://localhost:5190
  - hostname: control.omnibioai.org
    service: http://localhost:5174
  - hostname: admin.omnibioai.org
    service: http://localhost:5174
  - hostname: monitor.omnibioai.org
    service: http://localhost:3000
  - hostname: license.omnibioai.org
    service: http://10.0.0.5:8099
  - hostname: neo4j.omnibioai.org
    service: http://localhost:7474
  - hostname: docs.omnibioai.org
    service: http://localhost:80
  - service: http_status:404
"""

PROTECTED_HOSTS = ("control", "admin", "rag", "api", "workbench", "lims", "models", "bundles", "monitor",
                   "neo4j", "license", "dev", "sdk", "webstudio", "docs")


def _mapping(text):
    pairs, host = {}, None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- hostname:"):
            host = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("service:") and host:
            pairs[host] = stripped.split(":", 1)[1].strip()
            host = None
    return pairs


def _assert_only_videos_changed(before_text, after_text):
    before, after = _mapping(before_text), _mapping(after_text)
    assert before.keys() == after.keys()
    changed = {h for h in before if before[h] != after[h]}
    assert changed == {"videos.omnibioai.org"}
    assert after["videos.omnibioai.org"] == "http://localhost:8087"
    for sub in PROTECTED_HOSTS:
        host = f"{sub}.omnibioai.org"
        assert before[host] == after[host], host
    # everything that is not a hostname's service line is byte-identical
    assert len(before_text.splitlines()) == len(after_text.splitlines())
    diff = [(a, b) for a, b in zip(before_text.splitlines(), after_text.splitlines()) if a != b]
    assert diff == [("    service: http://localhost:8086", "    service: http://localhost:8087")]


def test_ingress_retarget_changes_only_the_videos_upstream():
    after = ri.retarget(INGRESS, "videos.omnibioai.org", 8086, 8087)
    _assert_only_videos_changed(INGRESS, after)


def test_ingress_retarget_is_reversible_for_rollback():
    forward = ri.retarget(INGRESS, "videos.omnibioai.org", 8086, 8087)
    assert ri.retarget(forward, "videos.omnibioai.org", 8087, 8086) == INGRESS


@pytest.mark.parametrize(
    "hostname, from_port, message",
    [
        ("nonexistent.omnibioai.org", 8086, "found 0"),
        ("videos.omnibioai.org", 9999, "service line is not"),
    ],
)
def test_ingress_retarget_refuses_ambiguous_or_unexpected_input(hostname, from_port, message):
    with pytest.raises(ri.IngressError, match=message):
        ri.retarget(INGRESS, hostname, from_port, 8087)


def test_ingress_retarget_refuses_duplicate_hostname():
    doubled = INGRESS + "  - hostname: videos.omnibioai.org\n    service: http://localhost:1\n"
    with pytest.raises(ri.IngressError, match="found 2"):
        ri.retarget(doubled, "videos.omnibioai.org", 8086, 8087)


def test_ingress_retarget_refuses_entry_without_service_line():
    with pytest.raises(ri.IngressError, match="no service line"):
        ri.retarget("ingress:\n  - hostname: videos.omnibioai.org", "videos.omnibioai.org", 8086, 8087)


def test_ingress_cli_never_edits_the_original(tmp_path, capsys):
    config = tmp_path / "config.yml"
    config.write_text(INGRESS)
    out = tmp_path / "proposed.yml"
    code = ri.main(["--config", str(config), "--hostname", "videos.omnibioai.org",
                    "--from-port", "8086", "--to-port", "8087", "--out", str(out)])
    assert code == 0
    assert config.read_text() == INGRESS
    _assert_only_videos_changed(INGRESS, out.read_text())
    assert "-    service: http://localhost:8086" in capsys.readouterr().out


def test_ingress_cli_refuses_with_nonzero_exit(tmp_path, capsys):
    config = tmp_path / "config.yml"
    config.write_text(INGRESS)
    code = ri.main(["--config", str(config), "--hostname", "videos.omnibioai.org",
                    "--from-port", "1234", "--to-port", "8087"])
    assert code == 1 and "REFUSED" in capsys.readouterr().err


def test_live_host_ingress_change_would_touch_only_videos():
    live = Path("/etc/cloudflared/config.yml")
    try:
        text = live.read_text()
    except OSError:
        pytest.skip("cloudflared config not readable on this host")
    if _mapping(text).get("videos.omnibioai.org") != "http://localhost:8086":
        pytest.skip("videos hostname is not on the legacy 8086 upstream (already retargeted?)")
    _assert_only_videos_changed(text, ri.retarget(text, "videos.omnibioai.org", 8086, 8087))


# ── Docker: what an anonymous client sees, and what happens when host files change ──────────────

HARDENING = [
    "--read-only", "--tmpfs", "/run:mode=0755", "--tmpfs", "/var/cache/nginx", "--tmpfs", "/tmp",
    "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "SETGID", "--cap-add", "SETUID",
    "--security-opt", "no-new-privileges:true",
]


class Site:
    """Anonymous HTTP client: no cookies, no credentials, no redirect following."""

    def __init__(self, port):
        self.port = port

    def request(self, method, path, headers=None, read=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, (resp.read() if read else b"")
        finally:
            conn.close()

    def get(self, path, headers=None):
        return self.request("GET", path, headers)

    def catalog(self):
        return json.loads(self.get("/videos.json")[2])


def _run(*args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, check=True, **kwargs)


def wait_for(predicate, timeout=25.0, interval=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except OSError:
            pass
        time.sleep(interval)
    return False


class Runner:
    """Starts candidate public-portal containers exactly as compose does (read-only, cap_drop ALL, loopback)."""

    def __init__(self):
        self.names = []

    def start(self, tag, content_dir, poll=1, rehash=3, hardened=True):
        name = f"omnibioai-videos-public-test-{uuid.uuid4().hex[:10]}"
        args = ["docker", "run", "-d", "--name", name, "-p", "127.0.0.1::8087",
                "-v", f"{content_dir}:/content:ro",
                "-e", f"PUBLISH_POLL_SECONDS={poll}", "-e", f"PUBLISH_REHASH_SECONDS={rehash}"]
        if hardened:
            args += HARDENING
        _run(*args, tag)
        self.names.append(name)
        port = int(_run("docker", "port", name, "8087/tcp").stdout.split()[-1].rsplit(":", 1)[1])
        site = Site(port)
        assert wait_for(lambda: site.get("/health")[0] == 200, timeout=30), _run("docker", "logs", name).stdout
        return name, site

    def close(self):
        for name in self.names:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Build the real public image (real Dockerfile + ignore rules) from a fixture host content dir."""
    base = tmp_path_factory.mktemp("public-portal")
    content = make_content(base)
    ctx = base / "ctx"
    (ctx / "runtime").mkdir(parents=True)
    for name in ("Dockerfile.public", "Dockerfile.public.dockerignore", ".dockerignore", "nginx.public.conf"):
        shutil.copy(ROOT / name, ctx)  # the real ignore rules must let the bundle through
    shutil.copy(ROOT / "runtime" / "publish-runtime.sh", ctx / "runtime")
    bundle = ctx / "dist" / "public"
    report = bp.build(content, ROOT / "portal", bundle)
    tag = f"omnibioai-videos-public-test:{uuid.uuid4().hex[:10]}"
    build_args = [x for k, v in SENTINELS.items() for x in ("--build-arg", f"{k}={v}")]
    env = {**os.environ, **SENTINELS}  # secrets present in the build environment, as in a real CI job
    _run("docker", "build", "-q", "-f", "Dockerfile.public", "-t", tag, *build_args, ".", cwd=ctx, env=env)
    yield {"tag": tag, "bundle": bundle, "content": content, "report": report}
    subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


@pytest.fixture
def runner():
    r = Runner()
    yield r
    r.close()


@pytest.fixture(scope="module")
def deployed(built):
    r = Runner()
    name, site = r.start(built["tag"], built["content"])
    yield {"name": name, "site": site, **built}
    r.close()


@pytest.fixture
def site(deployed):
    return deployed["site"]


@pytest.mark.docker
def test_image_contains_no_video_content_directory_or_restricted_files(built):
    listing = _run("docker", "run", "--rm", "--entrypoint", "sh", built["tag"], "-c",
                   "find / -xdev -type f \\( -name '*.mp4' -o -name '*.mov' -o -name '*.webm' -o -name guide.html "
                   "-o -name .env -o -name 'internal-index.html' \\) 2>/dev/null; ls -d /content 2>&1; true").stdout
    assert "No such file" in listing and ".mp4" not in listing and "guide.html" not in listing
    assert int(_run("docker", "image", "inspect", built["tag"], "--format", "{{.Size}}").stdout) < 150_000_000


@pytest.mark.docker
def test_anonymous_user_can_load_the_public_portal(site):
    status, headers, body = site.get("/")
    assert status == 200 and headers["content-type"].startswith("text/html")
    assert b"<title>OmniBioAI" in body and b'src="/portal.js"' in body
    assert site.get("/index.html")[0] == 200
    assert site.get("/portal.css")[0] == 200
    assert site.get("/portal.js")[0] == 200
    assert "set-cookie" not in headers


@pytest.mark.docker
def test_catalog_lists_exactly_the_public_record(site):
    status, headers, body = site.get("/videos.json")
    assert status == 200 and headers["content-type"].startswith("application/json")
    assert headers["cache-control"] == "no-cache"
    catalog = json.loads(body)
    assert [e["filename"] for e in catalog] == ["pub.mp4"]
    assert set(catalog[0]) == set(bp.PUBLIC_FIELDS)


@pytest.mark.docker
def test_public_video_streams_with_range_requests_from_the_host_mount(site):
    size = len(PUBLIC_BYTES)
    status, headers, _ = site.request("HEAD", "/videos/pub.mp4")
    assert status == 200 and headers["content-type"] == "video/mp4"
    assert headers["accept-ranges"] == "bytes" and headers["content-length"] == str(size)
    assert site.request("GET", "/videos/pub.mp4", read=False)[0] == 200
    status, headers, body = site.get("/videos/pub.mp4")
    assert status == 200 and body == PUBLIC_BYTES  # the served bytes are the host file's bytes
    status, headers, body = site.get("/videos/pub.mp4", {"Range": "bytes=10-19"})
    assert status == 206 and body == PUBLIC_BYTES[10:20]
    assert headers["content-range"] == f"bytes 10-19/{size}"
    status, headers, body = site.get("/videos/pub.mp4", {"Range": "bytes=-100"})
    assert status == 206 and body == PUBLIC_BYTES[-100:]
    assert site.get("/videos/pub.mp4", {"Range": f"bytes={size + 5}-"})[0] == 416


@pytest.mark.docker
@pytest.mark.parametrize("filename", [*NON_PUBLIC, UNREGISTERED, *DIRECT_LEAK_FILES])
def test_files_in_the_mount_that_are_not_approved_are_neither_listed_nor_fetchable(site, filename):
    """The whole content dir is mounted, yet INTERNAL / REVIEW_REQUIRED / missing / invalid visibility,
    unregistered media, guide.html and dotfiles all 404 -- on both /videos/<file> and /<file>."""
    for path in (f"/videos/{filename}", f"/{filename}", f"/content/{filename}"):
        status, _, body = site.get(path)
        assert status == 404, path
        assert b"SENTINEL" not in body
    surfaces = [site.get(p)[2].decode() for p in ("/videos.json", "/", "/portal.js")]
    for surface in surfaces:
        assert filename not in surface
        assert f"{filename}-TITLE-SENTINEL" not in surface


@pytest.mark.docker
@pytest.mark.parametrize(
    "path",
    [
        "/guide.html", "/.env", "/internal-index.html", "/content/videos.json", "/videos.json.map", "/portal.js.map",
        "/nginx.conf", "/Dockerfile", "/videos", "/videos/", "/videos/.env", "/videos/videos.json",
        "/videos/../guide.html", "/../etc/passwd", "/%2e%2e/etc/passwd", "/videos/..%2f..%2fetc/passwd",
        "/videos/%2e%2e/%2e%2e/etc/passwd", "/videos/pub.mp4/../internal.mp4", "//etc/passwd", "/50x.html",
        "/index.html/", "/portal.js/", "/health/x", "/videos/pub.mp4/", "/videos/PUB.MP4", "/videos/pub.mp4%00",
        "/videos/pub.mp4.bak", "/publication/approved.tsv", "/etc/publication/approved.tsv", "/run/publication/media.conf",
    ],
)
def test_no_source_files_generated_config_or_traversal_are_served(site, path):
    status, _, body = site.get(path)
    assert status in (400, 404), (path, status)
    assert b"root:" not in body and b"SENTINEL" not in body and b"approved" not in body and b"alias" not in body


@pytest.mark.docker
def test_directory_listing_is_unavailable(site):
    status, _, body = site.get("/videos/")
    assert status == 404 and b"Index of" not in body and b"pub.mp4" not in body


@pytest.mark.docker
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@pytest.mark.parametrize(
    "path", ["/", "/videos.json", "/videos/pub.mp4", "/videos/internal.mp4", "/upload", "/api/videos", "/admin"]
)
def test_upload_edit_delete_are_rejected(site, method, path):
    assert site.request(method, path)[0] == 405


@pytest.mark.docker
@pytest.mark.parametrize(
    "path",
    ["/upload", "/admin", "/api/videos", "/api/upload", "/login", "/auth", "/control", "/rag", "/pubmed",
     "/api/services", "/health/detail", "/metrics", "/docs", "/openapi.json"],
)
def test_no_admin_or_backend_surface_exists(site, path):
    status, headers, _ = site.get(path)
    assert status == 404 and "set-cookie" not in headers


@pytest.mark.docker
@pytest.mark.parametrize("path", ["/", "/videos.json", "/videos/pub.mp4", "/portal.js", "/nope", "/health"])
def test_security_headers_on_every_response(site, path):
    _, headers, _ = site.request("GET", path, {"Range": "bytes=0-9"} if path.endswith(".mp4") else None)
    assert "default-src 'none'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cross-origin-resource-policy"] == "same-origin"
    assert "access-control-allow-origin" not in headers
    assert headers["server"] == "nginx"  # no version disclosure
    assert "x-powered-by" not in headers and "set-cookie" not in headers


@pytest.mark.docker
def test_deployment_verifier_passes_against_the_running_site(deployed):
    results = vd.check_site(f"http://127.0.0.1:{deployed['site'].port}", deployed["content"] / "videos.json", deployed["content"])
    failed = [f"{r.name}: {r.detail}" for r in results if not r.ok]
    assert failed == [] and len(results) > 40


@pytest.mark.docker
def test_container_audit_passes_for_the_hardened_container(deployed):
    results = vd.check_container(deployed["name"], deployed["content"], deployed["bundle"])
    assert [f"{r.name}: {r.detail}" for r in results if not r.ok] == []
    assert len(results) == 10


@pytest.mark.docker
def test_container_audit_flags_an_unhardened_container(built, runner):
    name, _ = runner.start(built["tag"], built["content"], hardened=False)
    failed = {r.name for r in vd.check_container(name, built["content"], built["bundle"]) if not r.ok}
    assert "read-only root filesystem" in failed and "cap_drop ALL (adds only CHOWN/SETGID/SETUID)" in failed


@pytest.mark.docker
def test_content_mount_and_root_filesystem_are_not_writable(deployed):
    for target in ("/content/planted.mp4", "/etc/planted", "/usr/share/nginx/html/planted.html"):
        result = subprocess.run(["docker", "exec", deployed["name"], "sh", "-c", f"touch {target}"], capture_output=True)
        assert result.returncode != 0, target


@pytest.mark.docker
def test_sentinel_secrets_are_absent_from_bundle_image_env_and_responses(deployed, site, tmp_path):
    values = [v.encode() for v in SENTINELS.values()]

    def assert_clean(blob, where):
        for value in values:
            assert value not in blob, f"sentinel found in {where}"

    for rel, data in _walk_bytes(deployed["bundle"]).items():
        assert_clean(data, f"bundle:{rel}")
    assert bp.scan_tree(deployed["bundle"]) == []
    for src, dst in (("/usr/share/nginx/html", "www"), ("/etc/publication", "publication")):
        _run("docker", "cp", f"{deployed['name']}:{src}", str(tmp_path / dst))
        for rel, data in _walk_bytes(tmp_path / dst).items():
            assert_clean(data, f"{dst}:{rel}")
    # /run is a tmpfs (docker cp cannot read it): read the runtime-generated files through exec
    generated = _run("docker", "exec", deployed["name"], "sh", "-c", "cat /run/publication/media.conf /run/publication/videos.json")
    assert_clean(generated.stdout.encode(), "generated publication files")
    assert_clean(_run("docker", "history", "--no-trunc", deployed["tag"]).stdout.encode(), "image history")
    assert_clean(_run("docker", "inspect", deployed["tag"]).stdout.encode(), "image config")
    assert_clean(_run("docker", "inspect", deployed["name"]).stdout.encode(), "container config")
    for path in ("/", "/portal.js", "/portal.css", "/videos.json"):
        _, headers, body = site.get(path)
        assert_clean(body + str(headers).encode(), f"http:{path}")


# ── Runtime: the host file changes AFTER deployment ─────────────────────────


@pytest.fixture
def live(built, runner, tmp_path):
    """A private copy of the host content dir + a fresh container serving it (poll 1s, full re-hash 3s)."""
    content = tmp_path / "content"
    shutil.copytree(built["content"], content, symlinks=True)
    name, site = runner.start(built["tag"], content)
    assert [e["filename"] for e in site.catalog()] == ["pub.mp4"]
    return {"content": content, "site": site, "name": name}


def _withheld(site):
    return site.catalog() == [] and site.get("/videos/pub.mp4")[0] == 404


def _published(site):
    return [e["filename"] for e in site.catalog()] == ["pub.mp4"] and site.get("/videos/pub.mp4")[0] == 200


def _replace(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)  # atomic swap of the inode, like an editor export or `mv`


@pytest.mark.docker
def test_replaced_video_is_delisted_and_404s_until_the_approved_bytes_return(live):
    site, video = live["site"], live["content"] / "pub.mp4"
    replacement = make_mp4(seed=b"an unreviewed re-recording")
    assert len(replacement) == len(PUBLIC_BYTES)
    _replace(video, replacement)
    assert wait_for(lambda: _withheld(site)), "replaced file must be delisted AND its URL must 404"
    assert site.get("/videos/pub.mp4", {"Range": "bytes=0-9"})[0] == 404
    _replace(video, PUBLIC_BYTES)
    assert wait_for(lambda: _published(site)), "the approved bytes returning must re-publish it"
    assert site.get("/videos/pub.mp4")[2] == PUBLIC_BYTES


@pytest.mark.docker
def test_same_size_and_mtime_swap_is_caught_by_the_periodic_rehash(live):
    site, video = live["site"], live["content"] / "pub.mp4"
    before = video.stat()
    with video.open("r+b") as handle:  # rewrite the bytes in place, then forge the timestamps back
        handle.write(make_mp4(seed=b"sneaky in-place swap"))
    os.utime(video, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert (video.stat().st_size, video.stat().st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert wait_for(lambda: _withheld(site)), "size+mtime unchanged: only the full re-hash can catch this"


@pytest.mark.docker
def test_truncated_or_deleted_video_is_withheld(live):
    site, video = live["site"], live["content"] / "pub.mp4"
    video.write_bytes(PUBLIC_BYTES[:1000])  # a copy still in progress
    assert wait_for(lambda: _withheld(site))
    _replace(video, PUBLIC_BYTES)
    assert wait_for(lambda: _published(site))
    video.unlink()
    assert wait_for(lambda: _withheld(site))
    _replace(video, PUBLIC_BYTES)
    assert wait_for(lambda: _published(site))


@pytest.mark.docker
def test_symlinked_video_is_withheld_even_with_identical_bytes(live):
    site, content = live["site"], live["content"]
    (content / "elsewhere.mp4").write_bytes(PUBLIC_BYTES)
    (content / "pub.mp4").unlink()
    (content / "pub.mp4").symlink_to("elsewhere.mp4")
    assert wait_for(lambda: _withheld(site))
    assert site.get("/videos/elsewhere.mp4")[0] == 404  # and the symlink target is not reachable either


@pytest.mark.docker
def test_video_missing_at_startup_is_not_published_and_appears_when_it_arrives(built, runner, tmp_path):
    content = tmp_path / "content"
    shutil.copytree(built["content"], content)
    (content / "pub.mp4").unlink()
    _, site = runner.start(built["tag"], content)
    assert site.catalog() == [] and site.get("/videos/pub.mp4")[0] == 404
    _replace(content / "pub.mp4", PUBLIC_BYTES)
    assert wait_for(lambda: _published(site))


@pytest.mark.docker
def test_editing_the_host_manifest_or_dropping_files_in_does_not_publish_anything(live):
    """The image holds the reviewed publication snapshot: visibility changes need a rebuild, so
    neither a host videos.json edit nor a new file in the mounted directory can make anything public."""
    site, content = live["site"], live["content"]
    manifest = json.loads((content / "videos.json").read_text())
    for entry in manifest:
        if entry["filename"] == "novis.mp4":
            entry.update(visibility="PUBLIC", **approval((content / "novis.mp4").read_bytes()))
    (content / "videos.json").write_text(json.dumps(manifest))
    (content / "surprise.mp4").write_bytes(make_mp4(seed=b"surprise"))
    time.sleep(4)  # > one poll and one periodic re-hash
    for name in ("novis.mp4", "surprise.mp4", "internal.mp4", "review.mp4"):
        assert site.get(f"/videos/{name}")[0] == 404, name
    assert [e["filename"] for e in site.catalog()] == ["pub.mp4"]


@pytest.mark.docker
def test_container_stops_promptly_and_cleanly_on_sigterm(live):
    started = time.time()
    _run("docker", "stop", "-t", "10", live["name"])
    assert time.time() - started < 6
    assert _run("docker", "inspect", "-f", "{{.State.ExitCode}}", live["name"]).stdout.strip() == "0"


# ── Browser rendering of the empty / populated catalog ──────────────────────


def _assemble_www(out):
    """Mimic the runtime publisher for browser tests: web root + catalog assembled from the entry records."""
    entries = sorted((out / "publication" / "entries").glob("*.json"))
    (out / "www" / "videos.json").write_text("[" + ",".join(p.read_text() for p in entries) + "]\n")
    return out / "www"


@pytest.fixture
def serve_bundle():
    """Serve a directory over loopback HTTP; yields a ``start(directory) -> base_url`` factory."""
    servers = []

    def start(directory):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
        handler.log_message = lambda *args: None
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _page_text(base_url, viewport):
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        with sync_api.sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport=viewport)
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(base_url + "/")
            page.wait_for_selector(".state-box, .video-card", timeout=8000)
            result = {
                "pill": page.locator("#statusPill").inner_text(),
                "heading": page.locator(".state-box h3, .card-title").first.inner_text(),
                "body": page.locator("body").inner_text(),
                "controls_visible": page.locator("#controls").is_visible(),
                "errors": errors,
            }
            browser.close()
            return result
    except sync_api.Error as exc:  # browser binaries not installed in this environment
        pytest.skip(f"Chromium unavailable: {str(exc)[:80]}")


@pytest.mark.parametrize("viewport", [{"width": 1280, "height": 800}, {"width": 390, "height": 844}])
def test_browser_shows_videos_coming_soon_when_nothing_is_public(content_dir, tmp_path, serve_bundle, viewport):
    manifest = json.loads((content_dir / "videos.json").read_text())
    for entry in manifest:
        entry["visibility"] = "REVIEW_REQUIRED"
    _write_manifest(content_dir, manifest)
    out = tmp_path / "out"
    assert bp.build(content_dir, ROOT / "portal", out)["published"] == []
    page = _page_text(serve_bundle(_assemble_www(out)), viewport)
    assert page["heading"] == "Videos coming soon"
    assert page["pill"] == "● COMING SOON"
    assert not page["controls_visible"]  # nothing to search or filter yet
    for misleading in ("No videos found", "0 videos", "0 VIDEOS", "Try a different"):
        assert misleading not in page["body"]
    assert page["errors"] == []


def test_browser_lists_only_the_public_video_and_keeps_filters(content_dir, tmp_path, serve_bundle):
    out = tmp_path / "out"
    bp.build(content_dir, ROOT / "portal", out)
    page = _page_text(serve_bundle(_assemble_www(out)), {"width": 1280, "height": 800})
    assert page["pill"] == "● 1 VIDEOS" and page["controls_visible"]
    assert "pub.mp4-TITLE-SENTINEL" in page["body"]
    for name in [*NON_PUBLIC, UNREGISTERED]:
        assert f"{name}-TITLE-SENTINEL" not in page["body"]
    assert page["errors"] == []
