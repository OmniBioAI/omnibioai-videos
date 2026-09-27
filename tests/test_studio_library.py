"""Studio "Video Tutorials" build of the video library (videos:8086, Studio's /_svc/videos/).

The Studio variant reuses the public portal source and the public PUBLIC-only selection; only
presentation plumbing differs (document-relative URLs, no duplicate Back to Studio link). These
tests cover the generated bundle and its publication boundary, the nginx/Dockerfile contract,
the searchable library running under the /_svc/videos/ prefix in a browser, and -- via Docker --
the real image behind a replica of Studio's router location. Synthetic sentinel content only;
no running service is touched.

Developer: Manish Kumar <manish@omnibioai.org>
"""

import json
import re
import shutil
import subprocess
import uuid

import pytest
from playwright.sync_api import expect, sync_playwright

from test_public_portal import (
    DIRECT_LEAK_FILES, NON_PUBLIC, PUBLIC_BYTES, ROOT, UNREGISTERED, Site,
    _entry, _run, _walk_bytes, _write_manifest, approval, bp, content_dir, make_mp4, serve_bundle, wait_for,
)

STUDIO_CONF = ROOT / "nginx.studio.conf"
STUDIO_DOCKERFILE = ROOT / "Dockerfile.studio"
ROUTER_CONF = ROOT.parent / "omnibioai-studio" / "docker" / "nginx-router.conf"
# Studio's router location for this service, verbatim (asserted against the Studio repo below).
ROUTER_LOCATION = ("location ^~ /_svc/videos      { set $videos_upstream videos:8086; "
                   "rewrite ^/_svc/videos(/.*)$      $1 break; proxy_pass http://$videos_upstream; }")
PREFIX = "/_svc/videos/"
LEAK_MARKERS = ["TITLE-SENTINEL", "DESC-SENTINEL", "MEDIA-SENTINEL", "NOTES-SENTINEL", "/srv/private",
                "REVIEW_REQUIRED", "INTERNAL", UNREGISTERED, *NON_PUBLIC, *DIRECT_LEAK_FILES]


def studio_records():
    """PUBLIC library records (discovery metadata, one with malformed optional fields)."""
    return [
        dict(filename="start.mp4", title="Getting Started", desc="Configure your workspace",
             tag="intro", category="Getting Started", order=1, featured=True, duration=125),
        dict(filename="rna.mp4", title="Running an RNA-seq Workflow", desc="Compare differential expression",
             tag="workflow", category="Workflows", order=2, tags=["transcriptomics", "RNA-seq"],
             keywords=["gene counts"], services=["Nextflow"], modules=["Workflow Runner"], duration="12:34"),
        dict(filename="rag.mp4", title="Knowledge Search", desc="Explore scientific literature",
             tag="demo", category="AI & RAG", order=3, tags=["retrieval", "transcriptomics"], duration=3661),
        # Malformed optional metadata must be dropped, never break the page.
        dict(filename="docs.mp4", title="Documentation Portal", desc="Developer guides", tag="documentation",
             order=4, category="x" * 100, tags="not-a-list", keywords=[1, "", "k" * 200, "handbook"],
             duration=-5, featured="yes", thumbnail="javascript:alert(1)", internal_path="/srv/private/docs.mp4"),
    ]


def studio_content(base):
    """Host content: the PUBLIC library plus every kind of non-public video and stray file."""
    d = base / "studio-content"
    d.mkdir()
    manifest = []
    for record in studio_records():
        manifest.append({**record, "visibility": "PUBLIC", **approval(PUBLIC_BYTES)})
        (d / record["filename"]).write_bytes(PUBLIC_BYTES)
    for name, visibility in NON_PUBLIC.items():
        manifest.append(_entry(name, visibility, order=50, tags=[f"{name}-TAG-SENTINEL"], category="Security",
                               keywords=["transcriptomics"]))
        (d / name).write_bytes(f"{name}-MEDIA-SENTINEL".encode() * 50)
    (d / UNREGISTERED).write_bytes(b"UNREGISTERED-MEDIA-SENTINEL" * 50)
    for name in DIRECT_LEAK_FILES:
        (d / name).write_text(f"{name}-SENTINEL")
    _write_manifest(d, manifest)
    return d


def assert_no_leak(text):
    for marker in LEAK_MARKERS:
        assert marker not in text, marker


# ── Generated bundle and publication boundary ───────────────────────────────


def test_studio_bundle_contains_only_the_verified_public_catalog_and_media(content_dir, tmp_path):
    out = tmp_path / "studio"
    report = bp.build_studio(content_dir, ROOT / "portal", out)
    files = _walk_bytes(out)
    assert set(files) == {"www/index.html", "www/portal.css", "www/portal.js", "www/videos.json",
                          "nginx/media.conf", "media/pub.mp4"}
    assert files["media/pub.mp4"] == PUBLIC_BYTES
    assert report["published"] == ["pub.mp4"]
    assert report["unregistered_media"] == [UNREGISTERED]
    # Exactly the public projection: unknown/internal fields never reach Studio either.
    assert json.loads(files["www/videos.json"]) == [
        {"filename": "pub.mp4", "title": "pub.mp4-TITLE-SENTINEL", "desc": "pub.mp4-DESC-SENTINEL",
         "tag": "demo", "order": 1}]
    for path, data in files.items():
        if not path.startswith("media/"):
            text = data.decode()
            for name in (*NON_PUBLIC, UNREGISTERED, *DIRECT_LEAK_FILES):
                assert name not in text, (path, name)
            assert "NOTES-SENTINEL" not in text and "/srv/private" not in text


def test_studio_catalog_matches_public_catalog_minus_thumbnails(content_dir, tmp_path):
    manifest = json.loads((content_dir / "videos.json").read_text())
    manifest[0].update(thumbnail="thumbs/pub.jpg", category="Workflows", tags=["rna-seq"], duration=90)
    _write_manifest(content_dir, manifest)
    bp.build(content_dir, ROOT / "portal", tmp_path / "public")
    bp.build_studio(content_dir, ROOT / "portal", tmp_path / "studio")
    public = json.loads((tmp_path / "public" / "publication" / "entries" / "001.json").read_text())
    studio = json.loads((tmp_path / "studio" / "www" / "videos.json").read_text())
    assert public.pop("thumbnail") == "thumbs/pub.jpg"  # the Studio image serves no image files
    assert studio == [public]


def test_studio_media_allowlist_is_exact_and_keeps_legacy_root_urls(content_dir, tmp_path):
    bp.build_studio(content_dir, ROOT / "portal", tmp_path / "studio")
    conf = (tmp_path / "studio" / "nginx" / "media.conf").read_text()
    assert re.findall(r"location (\S+) (\S+) \{", conf) == [("=", "/videos/pub.mp4"), ("=", "/pub.mp4")]
    assert conf.count("alias /usr/share/nginx/media/pub.mp4;") == 2
    assert conf.count("disable_symlinks on;") == 2 and "add_header" not in conf


def test_studio_portal_is_the_public_source_with_only_relative_urls_and_no_second_back_link(content_dir, tmp_path):
    bp.build_studio(content_dir, ROOT / "portal", tmp_path / "studio")
    www = tmp_path / "studio" / "www"
    assert (www / "portal.css").read_bytes() == (ROOT / "portal" / "portal.css").read_bytes()
    for name in ("index.html", "portal.js"):  # the declared rewrites are the only differences
        expected = (ROOT / "portal" / name).read_text()
        for old, new in bp.STUDIO_REWRITES[name]:
            expected = expected.replace(old, new)
        assert (www / name).read_text() == expected
    html, js = (www / "index.html").read_text(), (www / "portal.js").read_text()
    assert "back-to-studio" not in html and "data-studio-link" not in html and "/studio" not in html
    assert re.findall(r'(?:src|href)="([^"]+)"', html) == ["data:,", "portal.css", "portal.js"]
    assert js.count("fetch(") == 1 and "fetch('videos.json'" in js and "'/videos" not in js
    assert "url: 'videos/' + encodeURIComponent(v.filename)" in js


@pytest.mark.parametrize("name, old", [(n, old) for n, rules in bp.STUDIO_REWRITES.items() for old, _ in rules])
def test_studio_build_fails_closed_when_a_rewrite_marker_drifts(content_dir, tmp_path, name, old):
    portal = tmp_path / "portal"
    shutil.copytree(ROOT / "portal", portal)
    (portal / name).write_text((portal / name).read_text().replace(old, ""))
    out = tmp_path / "studio"
    with pytest.raises(bp.PublishError, match="exactly one"):
        bp.build_studio(content_dir, portal, out)
    assert not out.exists()


def test_studio_build_rejects_missing_portal_and_changed_media(content_dir, tmp_path):
    with pytest.raises(bp.PublishError, match="portal file missing"):
        bp.build_studio(content_dir, tmp_path / "no-portal", tmp_path / "a")
    (content_dir / "pub.mp4").write_bytes(make_mp4(seed=b"swapped"))
    with pytest.raises(bp.PublishError, match="sha256 mismatch"):
        bp.build_studio(content_dir, ROOT / "portal", tmp_path / "b")
    assert not (tmp_path / "b").exists()


def test_studio_build_reverifies_the_copied_bytes(content_dir, tmp_path, monkeypatch):
    def corrupting_copy(src, dst):
        dst.write_bytes(make_mp4(seed=b"tampered"))
    monkeypatch.setattr(bp.shutil, "copyfile", corrupting_copy)
    out = tmp_path / "studio"
    with pytest.raises(bp.PublishError, match="copied media does not match"):
        bp.build_studio(content_dir, ROOT / "portal", out)
    assert not out.exists()


def test_studio_build_scans_the_bundle_for_forbidden_content(content_dir, tmp_path):
    portal = tmp_path / "portal"
    shutil.copytree(ROOT / "portal", portal)
    (portal / "portal.css").write_text("/* /home/someone/secret */")
    out = tmp_path / "studio"
    with pytest.raises(bp.PublishError, match="forbidden content in Studio bundle"):
        bp.build_studio(content_dir, portal, out)
    assert not out.exists()


def test_studio_build_with_nothing_public_is_empty_and_serves_no_media(content_dir, tmp_path):
    manifest = json.loads((content_dir / "videos.json").read_text())
    manifest[0]["visibility"] = "REVIEW_REQUIRED"
    _write_manifest(content_dir, manifest)
    report = bp.build_studio(content_dir, ROOT / "portal", tmp_path / "studio")
    assert report["published"] == []
    assert (tmp_path / "studio" / "www" / "videos.json").read_text() == "[]"
    assert (tmp_path / "studio" / "nginx" / "media.conf").read_text() == ""
    assert list((tmp_path / "studio" / "media").iterdir()) == []


def test_cli_builds_each_variant_into_its_default_directory(content_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert bp.main(["--variant", "studio", "--content", str(content_dir), "--portal", str(ROOT / "portal")]) == 0
    assert "approved media copied and re-verified" in capsys.readouterr().out
    assert (tmp_path / "dist" / "studio" / "media" / "pub.mp4").read_bytes() == PUBLIC_BYTES
    assert bp.main(["--content", str(content_dir), "--portal", str(ROOT / "portal")]) == 0
    assert "(no media copied)" in capsys.readouterr().out
    assert not list((tmp_path / "dist" / "public").rglob("*.mp4"))
    (content_dir / "pub.mp4").write_bytes(b"changed")
    assert bp.main(["--variant", "studio", "--content", str(content_dir), "--portal", str(ROOT / "portal")]) == 1
    assert "BUILD FAILED" in capsys.readouterr().err


# ── Static nginx / Dockerfile contract ──────────────────────────────────────


def test_studio_nginx_is_an_exact_allowlist_framable_only_by_studio():
    code = "\n".join(line.split("#", 1)[0] for line in STUDIO_CONF.read_text().splitlines())
    assert "listen 8086;" in code
    assert "include /etc/nginx/studio/media.conf;" in code
    assert "location ^~ /videos/ {\n        return 404;" in code and "location / {\n        return 404;" in code
    assert "if ($request_method !~ ^(GET|HEAD)$)" in code
    assert "frame-ancestors 'self'" in code and 'X-Frame-Options "SAMEORIGIN"' in code
    assert "absolute_redirect off;" in code and "return 301 /_svc/videos/;" in code
    for forbidden in ("autoindex on", "try_files $uri", "/index.html;", "Access-Control-Allow-Origin",
                      "location ~", "alias /usr/share/nginx/html"):
        assert forbidden not in code, forbidden
    assert "add_header" not in code.split("\n    location ", 1)[1]  # headers set only at server level


def test_studio_dockerfile_never_packages_the_content_directory():
    code = "\n".join(l for l in STUDIO_DOCKERFILE.read_text().splitlines() if not l.startswith("#"))
    assert "content" not in code
    assert "COPY dist/studio/media/ /usr/share/nginx/media/" in code
    assert "COPY dist/studio/nginx/ /etc/nginx/studio/" in code and "EXPOSE 8086" in code
    ignore = [l for l in (ROOT / "Dockerfile.studio.dockerignore").read_text().splitlines() if l and l[0] != "#"]
    assert ignore == ["*", "!Dockerfile.studio", "!nginx.studio.conf", "!dist/studio"]


@pytest.mark.skipif(not ROUTER_CONF.is_file(), reason="omnibioai-studio checkout not present")
def test_router_location_replica_matches_studio():
    assert ROUTER_CONF.read_text().count(ROUTER_LOCATION) == 1


# ── Browser: the library under Studio's /_svc/videos/ prefix ────────────────


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        instance = pw.chromium.launch()
        yield instance
        instance.close()


def mount_under_prefix(out, root):
    """Lay the bundle out as the router exposes it: /_svc/videos/<www> and /_svc/videos/videos/<media>."""
    target = root / PREFIX.strip("/")
    shutil.copytree(out / "www", target)
    shutil.copytree(out / "media", target / "videos")
    return root


@pytest.fixture
def studio(browser, tmp_path, serve_bundle):
    content = studio_content(tmp_path)
    out = tmp_path / "studio-bundle"
    bp.build_studio(content, ROOT / "portal", out)
    base = serve_bundle(mount_under_prefix(out, tmp_path / "site"))
    opened = []

    def open_library(query="", width=1280, height=900):
        page = browser.new_page(viewport={"width": width, "height": height})
        errors, requests = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: request.url.startswith("data:") or requests.append(request.url))
        page.goto(base + PREFIX + query)
        expect(page.locator("#statusPill")).not_to_have_text("● LOADING")
        opened.append((page, errors))
        page.requests = requests
        return page

    open_library.base = base
    yield open_library
    for page, errors in opened:
        page.close()
        assert errors == []


def titles(page):
    return page.locator(".card-title").all_text_contents()


def test_studio_renders_the_new_library_with_studio_navigation_only(studio):
    page = studio()
    expect(page.get_by_role("combobox", name="Search OmniBioAI videos")).to_have_attribute(
        "placeholder", "Search OmniBioAI videos...")
    expect(page.locator("h1")).to_have_text("Video Tutorials")
    expect(page.locator("#videoCount")).to_have_text("4 videos")
    assert titles(page) == [r["title"] for r in studio_records()]
    assert page.locator(".filter-btn").all_text_contents() == [
        "All", "Getting Started", "Workflows", "AI & RAG", "Documentation"]
    assert page.locator(".library-section h2").all_text_contents() == [
        "Featured", "Workflows", "AI & RAG", "Documentation"]
    assert page.locator(".thumb-duration").all_text_contents() == ["2:05", "12:34", "1:01:01"]
    # Studio's shell owns navigation: no second Back to Studio, no link out of the page.
    assert page.locator("a").count() == 0
    assert page.locator(".topbar-path").inner_text().split() == ["studio", "/", "videos"]


def test_studio_library_requests_stay_under_the_proxy_prefix(studio):
    page = studio()
    page.locator(".video-card").first.scroll_into_view_if_needed()
    page.wait_for_function("document.querySelectorAll('.thumb video[src]').length > 0")
    for url in page.requests:
        assert url.startswith(studio.base + PREFIX), url
    assert any(url == studio.base + PREFIX + "videos.json" for url in page.requests)


def test_studio_search_counts_categories_and_no_results(studio):
    page = studio()
    search = page.get_by_role("combobox", name="Search OmniBioAI videos")
    search.fill("rnaseq")
    assert titles(page) == ["Running an RNA-seq Workflow"]
    expect(page.locator("#resultStatus")).to_have_text("1 result for “rnaseq”")
    search.fill("transcriptomics")
    assert titles(page) == ["Running an RNA-seq Workflow", "Knowledge Search"]
    search.press("Escape")  # dismiss autocomplete, which overlays the chips
    page.get_by_role("button", name="AI & RAG", exact=True).click()  # category + search combine
    assert titles(page) == ["Knowledge Search"]
    expect(page.locator("#videoCount")).to_have_text("1 video")
    assert "category=ai-rag" in page.url and "q=transcriptomics" in page.url
    assert page.url.startswith(studio.base + PREFIX)
    search.fill("zebrafish")
    expect(page.locator(".state-box h3")).to_have_text("No videos found for “zebrafish”")
    expect(page.locator("#videoCount")).to_have_text("0 videos")
    page.get_by_role("button", name="Clear search").click()
    assert titles(page) == ["Knowledge Search"]
    page.get_by_role("button", name="All", exact=True).click()
    expect(page.locator(".video-card")).to_have_count(4)


def test_studio_autocomplete_keyboard_and_video_opening(studio):
    page = studio()
    search = page.get_by_role("combobox", name="Search OmniBioAI videos")
    search.press_sequentially("rna")
    listbox = page.get_by_role("listbox", name="Search suggestions")
    expect(listbox).to_be_visible()
    expect(search).to_have_attribute("aria-expanded", "true")
    assert page.locator(".suggestion-text").all_text_contents()[0] == "Running an RNA-seq Workflow"
    search.press("ArrowDown")
    expect(page.locator("#suggestion-0")).to_have_attribute("aria-selected", "true")
    expect(search).to_have_attribute("aria-activedescendant", "suggestion-0")
    search.press("Enter")  # a video suggestion opens the player
    expect(page.locator("#modal")).to_have_class(re.compile(r"\bopen\b"))
    expect(page.locator("#modalClose")).to_be_focused()
    src = page.locator("#modalVideo").evaluate("v => v.src")
    assert src == studio.base + PREFIX + "videos/rna.mp4"
    page.keyboard.press("Escape")
    expect(page.locator("#modal")).not_to_have_class(re.compile(r"\bopen\b"))
    search.fill("")
    card = page.get_by_role("button", name="Play Knowledge Search")
    card.focus()
    card.press("Enter")
    assert page.locator("#modalVideo").evaluate("v => v.src") == studio.base + PREFIX + "videos/rag.mp4"
    expect(page.locator("#modalMeta")).to_have_text("AI & RAG · 1:01:01 · OmniBioAI Platform")


def test_studio_malformed_optional_metadata_is_dropped_safely(studio):
    page = studio()
    card = page.get_by_role("button", name="Play Documentation Portal")
    expect(card.locator(".card-footer span").first).to_have_text("Documentation")  # overlong category dropped
    assert card.locator(".thumb-duration").count() == 0 and card.locator("img").count() == 0
    page.get_by_role("combobox", name="Search OmniBioAI videos").fill("handbook")
    assert titles(page) == ["Documentation Portal"]


def test_studio_never_exposes_non_public_metadata(studio):
    page = studio()
    search = page.get_by_role("combobox", name="Search OmniBioAI videos")
    for probe in ("review", "internal", "SENTINEL", "Security", "unregistered", "nullvis"):
        search.fill(probe)
        expect(page.locator(".state-box h3")).to_contain_text("No videos found")
        assert page.locator(".suggestion").count() == 0, probe
    assert "Security" not in page.locator(".filter-btn").all_text_contents()
    search.fill("transcriptomics")  # a keyword shared with hidden entries counts only visible videos
    expect(page.locator("#videoCount")).to_have_text("2 videos")
    assert_no_leak(page.content())
    assert_no_leak(page.evaluate("fetch('videos.json').then(r => r.text())"))


@pytest.mark.parametrize("width", [320, 768, 1920])
def test_studio_layout_is_responsive_without_horizontal_scroll(studio, width):
    page = studio(width=width, height=800)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    columns = page.locator(".video-grid").first.evaluate(
        "g => getComputedStyle(g).gridTemplateColumns.split(' ').length")
    assert (columns == 1) if width == 320 else (columns >= 2)


# ── Docker: the real image behind a replica of Studio's router location ─────


@pytest.fixture(scope="module")
def studio_stack(tmp_path_factory):
    base = tmp_path_factory.mktemp("studio-image")
    content = studio_content(base)
    ctx = base / "ctx"
    ctx.mkdir()
    for name in ("Dockerfile.studio", "Dockerfile.studio.dockerignore", ".dockerignore", "nginx.studio.conf"):
        shutil.copy(ROOT / name, ctx)
    shutil.copytree(content, ctx / "content")  # present in the context; the ignore rules must keep it out
    bp.build_studio(content, ROOT / "portal", ctx / "dist" / "studio")
    tag = f"omnibioai-videos-studio-test:{uuid.uuid4().hex[:10]}"
    _run("docker", "build", "-q", "-f", "Dockerfile.studio", "-t", tag, ".", cwd=ctx)
    suffix = uuid.uuid4().hex[:10]
    network, videos, router = (f"omnibioai-videos-studio-test-{kind}-{suffix}" for kind in ("net", "svc", "router"))
    (base / "router.conf").write_text(
        "resolver 127.0.0.11 valid=10s;\nserver {\n    listen 80;\n    " + ROUTER_LOCATION + "\n}\n")
    try:
        _run("docker", "network", "create", network)
        _run("docker", "run", "-d", "--name", videos, "--network", network, "--network-alias", "videos",
             "-p", "127.0.0.1::8086", tag)
        _run("docker", "run", "-d", "--name", router, "--network", network, "-p", "127.0.0.1::80",
             "-v", f"{base / 'router.conf'}:/etc/nginx/conf.d/default.conf:ro", "nginx:alpine")
        ports = [int(_run("docker", "port", name, port).stdout.split()[-1].rsplit(":", 1)[1])
                 for name, port in ((videos, "8086/tcp"), (router, "80/tcp"))]
        direct, proxied = Site(ports[0]), Site(ports[1])
        assert wait_for(lambda: proxied.get(PREFIX + "videos.json")[0] == 200, timeout=30)
        yield {"tag": tag, "name": videos, "direct": direct, "proxied": proxied}
    finally:
        subprocess.run(["docker", "rm", "-f", videos, router], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


@pytest.mark.docker
def test_studio_image_contains_only_approved_media_and_no_content_dir(studio_stack):
    listing = _run("docker", "exec", studio_stack["name"], "sh", "-c",
                   "find /usr/share/nginx /etc/nginx/studio -type f | sort").stdout.split()
    assert listing == [
        "/etc/nginx/studio/media.conf",
        *(f"/usr/share/nginx/html/{n}" for n in ("index.html", "portal.css", "portal.js", "videos.json")),
        *(f"/usr/share/nginx/media/{r['filename']}" for r in sorted(studio_records(), key=lambda r: r["filename"])),
    ]


@pytest.mark.docker
def test_studio_image_serves_the_library_through_the_router(studio_stack):
    site = studio_stack["proxied"]
    status, headers, body = site.get(PREFIX)
    assert status == 200 and b'src="portal.js"' in body and b"back-to-studio" not in body
    assert "frame-ancestors 'self'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "SAMEORIGIN" and headers["x-content-type-options"] == "nosniff"
    assert "access-control-allow-origin" not in headers
    assert site.get(PREFIX + "portal.js")[0] == 200 and site.get(PREFIX + "portal.css")[0] == 200
    catalog = json.loads(site.get(PREFIX + "videos.json")[2])
    assert [v["filename"] for v in catalog] == [r["filename"] for r in studio_records()]
    status, headers, _ = site.request("HEAD", PREFIX + "videos/rna.mp4")
    assert status == 200 and headers["content-type"] == "video/mp4"
    assert int(headers["content-length"]) == len(PUBLIC_BYTES)
    status, headers, body = site.get(PREFIX + "videos/rna.mp4", {"Range": "bytes=10-19"})
    assert status == 206 and body == PUBLIC_BYTES[10:20]
    assert site.get(PREFIX + "rna.mp4")[2] == PUBLIC_BYTES  # media URL used by earlier Studio releases
    status, headers, _ = site.get("/_svc/videos")
    assert status == 301 and headers["location"] == PREFIX
    status, _, body = studio_stack["direct"].get("/health")
    assert status == 200 and body == b'{"status":"ok"}'


@pytest.mark.docker
def test_studio_image_withholds_everything_not_approved(studio_stack):
    site = studio_stack["proxied"]
    names = [*NON_PUBLIC, UNREGISTERED, *DIRECT_LEAK_FILES, "videos.json.map", "50x.html", "nginx.conf"]
    for name in names:
        for path in (f"{PREFIX}videos/{name}", f"{PREFIX}{name}"):
            status, _, body = site.get(path)
            assert status == 404, path
            assert_no_leak(body.decode(errors="replace"))
    for path in (PREFIX + "videos/", PREFIX + "nonexistent", PREFIX + "videos/rna.mp4/", PREFIX + "videos/RNA.MP4",
                 PREFIX + "videos/../guide.html", PREFIX + "media/rna.mp4"):
        assert site.get(path)[0] == 404, path  # no SPA fallback, no listing, no traversal
    assert site.request("POST", PREFIX + "videos.json")[0] == 405
    for path in (PREFIX, PREFIX + "videos.json", PREFIX + "portal.js"):
        assert_no_leak(site.get(path)[2].decode())


@pytest.mark.docker
def test_studio_image_library_works_in_a_browser_under_its_csp(studio_stack, browser):
    base = f"http://127.0.0.1:{studio_stack['proxied'].port}"
    page = browser.new_page()
    problems = []
    page.on("pageerror", lambda error: problems.append(str(error)))
    page.on("console", lambda msg: msg.type == "error" and "Content Security Policy" in msg.text
            and problems.append(msg.text))
    page.goto(base + PREFIX + "?q=rnaseq")
    expect(page.locator("#videoCount")).to_have_text("1 video")
    assert titles(page) == ["Running an RNA-seq Workflow"]
    page.get_by_role("button", name="Play Running an RNA-seq Workflow").click()
    assert page.locator("#modalVideo").evaluate("v => v.src") == base + PREFIX + "videos/rna.mp4"
    page.close()
    assert problems == []
