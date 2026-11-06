"""Deterministic contracts for the React/Vite video service."""

import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
CONTENT = ROOT / "content"
MANIFEST = CONTENT / "videos.json"


def load_manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_docker_builds_and_packages_the_vite_application():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM node:22-alpine AS frontend" in dockerfile
    assert "RUN npm run build" in dockerfile
    assert "COPY --from=frontend /app/dist/web/ /usr/share/nginx/html/" in dockerfile
    assert "COPY content/ /usr/share/nginx/html/" in dockerfile
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
        assert {"filename", "title", "desc", "tag", "order", "visibility"} <= set(entry)
        assert entry["visibility"] in {"PUBLIC", "INTERNAL", "REVIEW_REQUIRED"}
        if entry["visibility"] == "PUBLIC":
            assert re.fullmatch(r"[0-9a-f]{64}", entry["approved_sha256"])
            assert isinstance(entry["approved_size_bytes"], int) and entry["approved_size_bytes"] > 0
        assert re.fullmatch(r"[^/]+\.(?:mp4|webm|mov)", entry["filename"], re.I)
        assert (CONTENT / entry["filename"]).is_file()


def test_vite_has_two_react_entry_points_and_no_legacy_html_app():
    config = (ROOT / "vite.config.js").read_text(encoding="utf-8")
    assert "index.html" in config and "guide.html" in config
    assert (ROOT / "src/VideoPortal.jsx").is_file()
    assert (ROOT / "src/GuideApp.jsx").is_file()
    assert not (CONTENT / "index.html").exists()
    assert not (CONTENT / "guide.html").exists()
    assert not (ROOT / "portal/index.html").exists()
    assert not (ROOT / "portal/portal.js").exists()


def test_guide_defines_all_documented_sections_as_react_data():
    source = (ROOT / "src/GuideApp.jsx").read_text(encoding="utf-8")
    for section in ("overview", "local", "cloud", "hpc", "llm", "workbench", "workflow", "faq"):
        assert f'id: "{section}"' in source


@pytest.mark.xfail(strict=True, reason="Several manifest-listed video assets are zero-byte placeholders")
def test_video_files_are_non_empty():
    assert all((CONTENT / entry["filename"]).stat().st_size > 0 for entry in load_manifest())
