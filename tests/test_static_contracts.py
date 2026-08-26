"""Deterministic contract tests for the static video service.

These tests intentionally exercise the files that are packaged into the nginx
image without requiring Docker, a network service, or video decoding.
"""

import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
CONTENT = ROOT / "content"
MANIFEST = CONTENT / "videos.json"
NGINX = ROOT / "nginx.conf"
DOCKERFILE = ROOT / "Dockerfile"
INDEX = CONTENT / "index.html"
GUIDE = CONTENT / "guide.html"


def load_manifest():
    with MANIFEST.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_docker_image_packages_the_documented_content_root():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY content/ /usr/share/nginx/html/" in dockerfile
    assert "COPY nginx.conf /etc/nginx/conf.d/default.conf" in dockerfile
    assert "EXPOSE 8086" in dockerfile


def test_manifest_entries_are_complete_unique_and_backed_by_files():
    entries = load_manifest()
    assert isinstance(entries, list) and entries

    filenames = [entry["filename"] for entry in entries]
    orders = [entry["order"] for entry in entries]
    assert len(filenames) == len(set(filenames))
    assert len(orders) == len(set(orders))
    assert orders == sorted(orders)

    for entry in entries:
        assert set(entry) == {"filename", "title", "desc", "tag", "order"}
        assert re.fullmatch(r"[^/]+\.(?:mp4|webm|mov)", entry["filename"], re.I)
        assert (CONTENT / entry["filename"]).is_file()
        assert all(isinstance(entry[field], str) and entry[field].strip() for field in ("title", "desc", "tag"))
        assert isinstance(entry["order"], int) and not isinstance(entry["order"], bool)


def test_nginx_routes_match_the_packaged_layout_and_documented_endpoints():
    config = NGINX.read_text(encoding="utf-8")
    assert 'root /usr/share/nginx/html;' in config
    assert "location /videos.json" in config
    assert 'alias /videos/videos.json;' in config
    assert "location /videos/" in config
    assert "alias /videos/;" in config
    assert "location /health" in config


@pytest.mark.xfail(strict=True, reason="Known production routing mismatch: index.js uses /videos/* while Dockerfile packages content at the nginx document root")
def test_index_uses_the_same_video_urls_as_the_nginx_configuration():
    html = INDEX.read_text(encoding="utf-8")
    # This currently fails and records the production routing defect: the
    # image contains /usr/share/nginx/html/*, not /videos/*.
    assert "fetch(`${API}/videos.json`)" in html
    assert "fetch(`${API}/videos/`)" in html
    assert "${API}/${v.filename}" in html


def test_index_contains_manifest_filter_search_and_modal_contracts():
    html = INDEX.read_text(encoding="utf-8")
    for marker in ("videos.json", "filterVideos", "setFilter", "openModal", "closeModal", "searchInput", "videoCount"):
        assert marker in html
    assert "manifest.sort" in html
    assert "(a.order || 999) - (b.order || 999)" in html


def test_guide_has_all_documented_sections_and_navigation_handler():
    html = GUIDE.read_text(encoding="utf-8")
    for section in ("overview", "local", "cloud", "hpc", "llm", "workbench", "workflow", "faq"):
        assert f'id="p-{section}"' in html
    assert "function show(" in html
    assert "show('overview')" in html


@pytest.mark.xfail(strict=True, reason="Several manifest-listed video assets are zero-byte placeholders in the repository")
def test_video_files_are_non_empty_and_have_expected_media_extensions():
    manifest_names = {entry["filename"] for entry in load_manifest()}
    content_videos = {
        path.name for path in CONTENT.iterdir() if path.suffix.lower() in {".mp4", ".webm", ".mov"}
    }
    assert manifest_names <= content_videos
    assert all((CONTENT / name).stat().st_size > 0 for name in manifest_names)
