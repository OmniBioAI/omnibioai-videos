"""Publication-boundary tests for the public video portal (videos.omnibioai.org).

Covers the fail-closed visibility model in ``scripts/build_public.py``, the strict
``nginx.public.conf`` / ``Dockerfile.public`` / compose contract, the cloudflared
ingress retarget helper (proving no other hostname's upstream changes), and -- via
Docker -- the behaviour an anonymous browser actually sees. Synthetic sentinel
secrets are injected into the build environment and searched for in the bundle,
the image layers, the container environment and every HTTP response.

Developer: Manish Kumar <manish@omnibioai.org>
"""

import hashlib
import http.client
import importlib.util
import json
import os
import shutil
import functools
import http.server
import subprocess
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
    spec.loader.exec_module(module)
    return module


bp = _load("build_public", "scripts/build_public.py")
ri = _load("retarget_ingress", "scripts/retarget_ingress.py")

# Synthetic secrets: exact values are searched for everywhere the public can reach.
SENTINELS = {
    "VITE_API_KEY": "sentinel-vite-key-7c1e9d20-must-never-ship",
    "VITE_IAM_TOKEN": "sentinel-vite-iam-3b8f5a41-must-never-ship",
    "API_TOKEN": "sentinel-api-token-91d2e6c7-must-never-ship",
    "AWS_SECRET_ACCESS_KEY": "sentinel-aws-secret-5e4a7b13-must-never-ship",
    "ADMIN_SESSION": "sentinel-admin-session-a02c8f96-must-never-ship",
}

PUBLIC_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"PUBLIC-MEDIA-SENTINEL" * 200
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
        entry["sha256"] = hashlib.sha256(PUBLIC_BYTES).hexdigest()  # approval is pinned to content
    entry.update(extra)
    return entry


@pytest.fixture
def content_dir(tmp_path):
    """A content directory holding one PUBLIC video and every kind of non-public content."""
    d = tmp_path / "content"
    d.mkdir()
    manifest = [
        _entry("pub.mp4", "PUBLIC", internal_path="/srv/private/pub.mp4", notes="NOTES-SENTINEL"),
    ]
    (d / "pub.mp4").write_bytes(PUBLIC_BYTES)
    for name, visibility in NON_PUBLIC.items():
        manifest.append(_entry(name, visibility))
        (d / name).write_bytes(f"{name}-MEDIA-SENTINEL".encode() * 50)
    (d / UNREGISTERED).write_bytes(b"UNREGISTERED-MEDIA-SENTINEL" * 50)
    (d / "guide.html").write_text("GUIDE-SENTINEL")
    (d / ".env").write_text("ENV-FILE-SENTINEL=1")
    (d / "internal-index.html").write_text("INTERNAL-INDEX-SENTINEL")
    (d / "videos.json").write_text(json.dumps(manifest))
    return d


@pytest.fixture
def portal_dir():
    return ROOT / "portal"


def _walk_bytes(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


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


# ── Bundle assembly ──────────────────────────────────────────────────────────


def test_build_publishes_only_explicit_public_content(content_dir, portal_dir, tmp_path):
    out = tmp_path / "out"
    report = bp.build(content_dir, portal_dir, out)

    files = _walk_bytes(out)
    assert set(files) == {"index.html", "portal.css", "portal.js", "videos.json", "videos/pub.mp4"}
    assert files["videos/pub.mp4"] == PUBLIC_BYTES
    assert [e["filename"] for e in json.loads(files["videos.json"])] == ["pub.mp4"]
    assert report["published"] == ["pub.mp4"]
    assert report["counts"]["PUBLIC"] == 1
    assert report["counts"]["INTERNAL"] == 1
    assert report["counts"]["REVIEW_REQUIRED"] == 1
    assert report["counts"]["UNCLASSIFIED"] == 4
    assert report["unregistered_media"] == [UNREGISTERED]

    blob = b"\n".join(files.values())
    for leaked in (
        b"internal.mp4", b"review.mp4", b"novis.mp4", b"lowercase.mp4", b"typo.mp4",
        b"nullvis.mp4", UNREGISTERED.encode(), b"GUIDE-SENTINEL",
        b"ENV-FILE-SENTINEL", b"INTERNAL-INDEX-SENTINEL", b"NOTES-SENTINEL", b"/srv/private",
    ):
        assert leaked not in blob, leaked
    # non-public titles/descriptions must not reach the catalog either
    for name in [*NON_PUBLIC, UNREGISTERED]:
        assert f"{name}-TITLE-SENTINEL".encode() not in blob
        assert f"{name}-DESC-SENTINEL".encode() not in blob


def test_public_catalog_is_an_allowlisted_projection(content_dir, portal_dir, tmp_path):
    out = tmp_path / "out"
    bp.build(content_dir, portal_dir, out)
    (entry,) = json.loads((out / "videos.json").read_text())
    assert set(entry) == set(bp.PUBLIC_FIELDS)  # no visibility, internal_path or notes


def test_build_recreates_output_dir_so_stale_files_cannot_linger(content_dir, portal_dir, tmp_path):
    out = tmp_path / "out"
    (out / "videos").mkdir(parents=True)
    (out / "videos" / "stale-internal.mp4").write_bytes(b"STALE")
    bp.build(content_dir, portal_dir, out)
    assert not (out / "videos" / "stale-internal.mp4").exists()


def test_build_with_no_public_entries_yields_an_empty_catalog(content_dir, portal_dir, tmp_path):
    manifest = json.loads((content_dir / "videos.json").read_text())
    for entry in manifest:
        entry["visibility"] = "REVIEW_REQUIRED"
    (content_dir / "videos.json").write_text(json.dumps(manifest))
    out = tmp_path / "out"
    report = bp.build(content_dir, portal_dir, out)
    assert report["published"] == []
    assert json.loads((out / "videos.json").read_text()) == []
    assert list((out / "videos").iterdir()) == []


def _write_manifest(content_dir, entries):
    (content_dir / "videos.json").write_text(json.dumps(entries))


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
    ],
)
def test_malformed_public_entry_aborts_the_build(content_dir, portal_dir, tmp_path, mutation, message):
    entry = _entry("pub.mp4", "PUBLIC")
    mutation(entry)
    _write_manifest(content_dir, [entry])
    with pytest.raises(bp.PublishError, match=message):
        bp.build(content_dir, portal_dir, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("bad", [None, "", "abc", "A" * 64, "g" * 64, 12345])
def test_public_entry_without_a_valid_sha256_aborts_the_build(content_dir, portal_dir, tmp_path, bad):
    entry = _entry("pub.mp4", "PUBLIC")
    entry["sha256"] = bad
    if bad is None:
        del entry["sha256"]
    _write_manifest(content_dir, [entry])
    with pytest.raises(bp.PublishError, match="needs the lowercase hex 'sha256'"):
        bp.build(content_dir, portal_dir, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_file_replaced_after_approval_cannot_be_published(content_dir, portal_dir, tmp_path):
    """Approval is bound to content: swapping the bytes under the same filename aborts the build."""
    (content_dir / "pub.mp4").write_bytes(b"A NEW, UNREVIEWED RECORDING" * 100)
    with pytest.raises(bp.PublishError, match="sha256 mismatch"):
        bp.build(content_dir, portal_dir, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_empty_media_file_aborts_the_build(content_dir, portal_dir, tmp_path):
    (content_dir / "pub.mp4").write_bytes(b"")
    with pytest.raises(bp.PublishError, match="empty"):
        bp.build(content_dir, portal_dir, tmp_path / "out")


def test_symlinked_media_aborts_the_build(content_dir, portal_dir, tmp_path):
    secret = tmp_path / "outside-secret.mp4"
    secret.write_bytes(b"OUTSIDE-SENTINEL")
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


def test_cli_prints_inventory(content_dir, tmp_path, capsys):
    out = tmp_path / "out"
    code = bp.main(["--content", str(content_dir), "--portal", str(ROOT / "portal"), "--out", str(out)])
    stdout = capsys.readouterr().out
    assert code == 0
    assert "Published 1 video(s)" in stdout
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


def test_scan_tree_ignores_binary_media_bytes(tmp_path):
    (tmp_path / "v.mp4").write_bytes(b"sk-ant-api03-abcdefghij")  # not a text asset
    assert bp.scan_tree(tmp_path) == []


# ── The real repo: manifest, portal assets, build ───────────────────────────


def test_repo_manifest_every_entry_has_an_explicit_valid_visibility():
    manifest = json.loads((ROOT / "content" / "videos.json").read_text())
    assert manifest, "manifest unexpectedly empty"
    bad = [e.get("filename") for e in manifest if bp.classify(e) == bp.UNCLASSIFIED]
    assert bad == [], f"entries without an explicit visibility (would be withheld): {bad}"


def test_real_bundle_publishes_exactly_the_public_entries_and_scans_clean(tmp_path):
    out = tmp_path / "out"
    report = bp.build(ROOT / "content", ROOT / "portal", out)
    manifest = json.loads((ROOT / "content" / "videos.json").read_text())
    expected = sorted(e["filename"] for e in manifest if e.get("visibility") == "PUBLIC")
    assert sorted(report["published"]) == expected
    assert sorted(p.name for p in (out / "videos").iterdir()) == expected
    assert bp.scan_tree(out) == []


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


# ── Static server contract (no Docker) ──────────────────────────────────────


def test_public_nginx_conf_is_strict():
    conf = (ROOT / "nginx.public.conf").read_text()
    code = "\n".join(line for line in conf.splitlines() if not line.strip().startswith("#"))
    assert "listen 8087;" in code
    assert "server_tokens off;" in code and "autoindex off;" in code
    for forbidden in (
        "autoindex on", "Access-Control-Allow-Origin", "proxy_pass", "fastcgi_pass", "uwsgi_pass",
        "upstream", "$uri/ /index.html", "dav_methods",
    ):
        assert forbidden not in code, forbidden
    assert "GET|HEAD" in code and "return 405" in code
    assert code.rstrip().endswith("location / {\n        return 404;\n    }\n}")
    assert "default-src 'none'" in code and "frame-ancestors 'none'" in code
    assert "'unsafe-inline'" not in code and "'unsafe-eval'" not in code


def test_public_dockerfile_copies_only_the_built_bundle():
    lines = [l.strip() for l in (ROOT / "Dockerfile.public").read_text().splitlines()
             if l.strip() and not l.strip().startswith("#")]
    copies = [l for l in lines if l.startswith(("COPY", "ADD"))]
    assert copies == [
        "COPY nginx.public.conf /etc/nginx/conf.d/default.conf",
        "COPY dist/public/ /usr/share/nginx/html/",
    ]
    assert not any(l.startswith(("ARG", "ENV")) for l in lines)  # no build-time secret channel


def test_public_build_context_is_an_allowlist_that_admits_the_bundle():
    rules = [l.strip() for l in (ROOT / "Dockerfile.public.dockerignore").read_text().splitlines()
             if l.strip() and not l.strip().startswith("#")]
    assert rules == ["*", "!Dockerfile.public", "!nginx.public.conf", "!dist/public"]
    assert "dist/" in (ROOT / ".gitignore").read_text().splitlines()  # a built bundle is never committed


def test_empty_catalog_says_videos_coming_soon_not_a_search_failure():
    js = (ROOT / "portal" / "portal.js").read_text()
    body = js[js.index("function showComingSoon()"):js.index("function applyFilters()")]
    assert "Videos coming soon" in body and "COMING SOON" in body
    for misleading in ("No videos found", "search", "filter", "Try a different"):
        assert misleading not in body
    assert "showComingSoon();" in js[js.index("function load()"):]  # empty catalog routes here
    assert 'id="controls"' in (ROOT / "portal" / "index.html").read_text()
    assert "[hidden]" in (ROOT / "portal" / "portal.css").read_text()  # .controls is display:flex


def test_public_compose_is_loopback_only_and_credential_free():
    compose = yaml.safe_load((ROOT / "deploy" / "docker-compose.public.yml").read_text())
    (svc,) = compose["services"].values()
    assert svc["ports"] == ["127.0.0.1:8087:8087"]
    for key in ("volumes", "environment", "env_file", "secrets", "privileged", "network_mode", "pid"):
        assert key not in svc, key
    assert svc["read_only"] is True
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]


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


# ── Docker: what an anonymous browser actually sees ─────────────────────────


class Site:
    """Anonymous HTTP client: no cookies, no credentials, no redirect following."""

    def __init__(self, port):
        self.port = port

    def request(self, method, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def get(self, path, headers=None):
        return self.request("GET", path, headers)


def _run(*args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, check=True, **kwargs)


@pytest.fixture(scope="module")
def deployed(tmp_path_factory):
    """Build the real public image from a fixture bundle, with sentinel secrets in the environment."""
    base = tmp_path_factory.mktemp("public-portal")
    content = base / "content"
    content.mkdir()
    manifest = [_entry("pub.mp4", "PUBLIC")]
    (content / "pub.mp4").write_bytes(PUBLIC_BYTES)
    for name, visibility in NON_PUBLIC.items():
        manifest.append(_entry(name, visibility))
        (content / name).write_bytes(f"{name}-MEDIA-SENTINEL".encode() * 50)
    (content / UNREGISTERED).write_bytes(b"UNREGISTERED-MEDIA-SENTINEL" * 50)
    for name in DIRECT_LEAK_FILES:
        (content / name).write_text(f"{name}-SENTINEL")
    (content / "videos.json").write_text(json.dumps(manifest))

    ctx = base / "ctx"
    ctx.mkdir()
    for name in ("Dockerfile.public", "Dockerfile.public.dockerignore", ".dockerignore", "nginx.public.conf"):
        shutil.copy(ROOT / name, ctx)  # the real ignore rules must let the bundle through
    report = bp.build(content, ROOT / "portal", ctx / "dist" / "public")

    tag = f"omnibioai-videos-public-test:{uuid.uuid4().hex[:10]}"
    name = f"omnibioai-videos-public-test-{uuid.uuid4().hex[:10]}"
    build_args = [x for k, v in SENTINELS.items() for x in ("--build-arg", f"{k}={v}")]
    env = {**os.environ, **SENTINELS}  # secrets present in the build environment, as in a real CI job
    _run("docker", "build", "-q", "-f", "Dockerfile.public", "-t", tag, *build_args, ".", cwd=ctx, env=env)
    _run("docker", "run", "-d", "--name", name, "-p", "127.0.0.1::8087", tag, env=env)
    try:
        mapping = _run("docker", "port", name, "8087/tcp").stdout.split()[-1]
        site = Site(int(mapping.rsplit(":", 1)[1]))
        for _ in range(60):
            try:
                if site.get("/health")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("public portal container did not become healthy")
        yield {"site": site, "name": name, "tag": tag, "bundle": ctx / "dist" / "public", "report": report}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


@pytest.fixture
def site(deployed):
    return deployed["site"]


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
def test_explicit_public_video_is_listed_and_viewable(site):
    status, headers, body = site.get("/videos.json")
    assert status == 200 and headers["cache-control"] == "no-cache"
    catalog = json.loads(body)
    assert [e["filename"] for e in catalog] == ["pub.mp4"]
    assert set(catalog[0]) == set(bp.PUBLIC_FIELDS)

    status, headers, body = site.get("/videos/pub.mp4")
    assert status == 200 and body == PUBLIC_BYTES
    assert headers["content-type"] == "video/mp4" and headers["accept-ranges"] == "bytes"
    status, headers, body = site.get("/videos/pub.mp4", {"Range": "bytes=0-9"})
    assert status == 206 and body == PUBLIC_BYTES[:10]
    assert site.request("HEAD", "/videos/pub.mp4")[0] == 200


@pytest.mark.docker
@pytest.mark.parametrize("filename", [*NON_PUBLIC, UNREGISTERED])
def test_non_public_content_is_neither_discoverable_nor_fetchable(site, filename):
    """INTERNAL, REVIEW_REQUIRED, missing/invalid visibility and unregistered media all 404."""
    for path in (f"/videos/{filename}", f"/{filename}", f"/content/{filename}"):
        status, _, body = site.get(path)
        assert status == 404, path
        assert f"{filename}-MEDIA-SENTINEL".encode() not in body
    catalog = site.get("/videos.json")[2].decode()
    for surface in (catalog, site.get("/")[2].decode(), site.get("/portal.js")[2].decode()):
        assert filename not in surface
        assert f"{filename}-TITLE-SENTINEL" not in surface


@pytest.mark.docker
@pytest.mark.parametrize(
    "path",
    [
        "/guide.html", "/.env", "/internal-index.html", "/content/videos.json", "/videos.json.map",
        "/portal.js.map", "/nginx.conf", "/Dockerfile", "/videos", "/videos/", "/videos/.env",
        "/videos/../guide.html", "/../etc/passwd", "/%2e%2e/etc/passwd", "/videos/..%2f..%2fetc/passwd",
        "/videos/%2e%2e/%2e%2e/etc/passwd", "/videos/pub.mp4/../internal.mp4", "//etc/passwd",
        "/50x.html", "/index.html/", "/portal.js/", "/health/x",
    ],
)
def test_no_source_files_or_traversal_are_served(site, path):
    status, _, body = site.get(path)
    assert status in (400, 404), (path, status)
    assert b"root:" not in body and b"SENTINEL" not in body


@pytest.mark.docker
@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@pytest.mark.parametrize(
    "path", ["/", "/videos.json", "/videos/pub.mp4", "/upload", "/api/videos", "/api/upload", "/admin"]
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
    _, headers, _ = site.get(path)
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
def test_sentinel_secrets_are_absent_from_bundle_image_env_and_responses(deployed, site, tmp_path):
    values = [v.encode() for v in SENTINELS.values()]

    def assert_clean(blob, where):
        for value in values:
            assert value not in blob, f"sentinel found in {where}"

    # 1. the generated browser bundle on disk
    for rel, data in _walk_bytes(deployed["bundle"]).items():
        assert_clean(data, f"bundle:{rel}")
    assert bp.scan_tree(deployed["bundle"]) == []

    # 2. the web root inside the running container
    dest = tmp_path / "webroot"
    _run("docker", "cp", f"{deployed['name']}:/usr/share/nginx/html", str(dest))
    served = _walk_bytes(dest)
    assert set(served) == {"index.html", "portal.css", "portal.js", "videos.json", "videos/pub.mp4"}
    for rel, data in served.items():
        assert_clean(data, f"webroot:{rel}")

    # 3. image layers / build history and container configuration
    assert_clean(_run("docker", "history", "--no-trunc", deployed["tag"]).stdout.encode(), "image history")
    assert_clean(_run("docker", "inspect", deployed["tag"]).stdout.encode(), "image config")
    assert_clean(_run("docker", "inspect", deployed["name"]).stdout.encode(), "container config")

    # 4. everything an anonymous client can fetch
    for path in ("/", "/portal.js", "/portal.css", "/videos.json", "/videos/pub.mp4"):
        _, headers, body = site.get(path)
        assert_clean(body + str(headers).encode(), f"http:{path}")


def test_sentinel_detection_is_not_vacuous(tmp_path, content_dir):
    """A planted sentinel must make the scan (and therefore the build) fail."""
    portal = tmp_path / "portal"
    shutil.copytree(ROOT / "portal", portal)
    planted = SENTINELS["VITE_API_KEY"]
    (portal / "portal.js").write_text(f"const cfg = {{ key: '{planted}' }}; // VITE_API_KEY")
    with pytest.raises(bp.PublishError, match="vite-env"):
        bp.build(content_dir, portal, tmp_path / "out")
    assert not (tmp_path / "out").exists()  # nothing containing the planted secret is left behind


# ── Browser rendering of the empty / populated catalog ──────────────────────


@pytest.fixture
def serve_bundle():
    """Serve a built bundle directory over loopback HTTP; yields a ``url(bundle_dir) -> base_url`` factory."""
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
def test_browser_shows_videos_coming_soon_for_an_empty_catalog(tmp_path, serve_bundle, viewport):
    """The real repo state (nothing PUBLIC) must read as 'coming soon', never as a failed search."""
    out = tmp_path / "out"
    assert bp.build(ROOT / "content", ROOT / "portal", out)["published"] == []
    page = _page_text(serve_bundle(out), viewport)
    assert page["heading"] == "Videos coming soon"
    assert page["pill"] == "● COMING SOON"
    assert not page["controls_visible"]  # nothing to search or filter yet
    for misleading in ("No videos found", "0 videos", "0 VIDEOS", "Try a different"):
        assert misleading not in page["body"]
    assert page["errors"] == []


def test_browser_lists_public_videos_and_keeps_filters_when_populated(content_dir, tmp_path, serve_bundle):
    out = tmp_path / "out"
    bp.build(content_dir, ROOT / "portal", out)
    page = _page_text(serve_bundle(out), {"width": 1280, "height": 800})
    assert page["pill"] == "● 1 VIDEOS" and page["controls_visible"]
    assert "pub.mp4-TITLE-SENTINEL" in page["body"]
    for name in [*NON_PUBLIC, UNREGISTERED]:
        assert f"{name}-TITLE-SENTINEL" not in page["body"]
    assert page["errors"] == []
