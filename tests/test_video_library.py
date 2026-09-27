"""Discovery UX and metadata-boundary regressions for the public video library.

Browser tests serve the actual portal; publication tests build real catalog fragments
from synthetic approved media. No production data or running service is modified.
"""

import json

import pytest
from playwright.sync_api import expect, sync_playwright

from test_public_portal import (
    ROOT, NON_PUBLIC, _entry, _write_manifest, _assemble_www,
    _static_portal_dir, bp, content_dir, serve_bundle,
)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        instance = pw.chromium.launch()
        yield instance
        instance.close()


def records():
    return [
        dict(filename="start.mp4", title="Getting Started", desc="Configure your workspace",
             tag="intro", category="Getting Started", order=1, featured=True, duration=125),
        dict(filename="rna.mp4", title="Running an RNA-seq Workflow", desc="Compare differential expression from FASTQ",
             tag="workflow", category="Workflows", order=2, tags=["transcriptomics", "RNA-seq", "rnaseq"],
             keywords=["gene counts"], services=["Nextflow"], modules=["Workflow Runner"],
             workflows=["nf-core RNA-seq"], duration="12:34"),
        dict(filename="rag.mp4", title="Knowledge Search", desc="Explore scientific literature",
             tag="demo", category="AI & RAG", order=3, tags=["retrieval", "transcriptomics"], duration=3661),
        dict(filename="docs.mp4", title="Documentation Portal", desc="Developer guides",
             tag="documentation", category="Documentation", order=4),
    ]


@pytest.fixture
def library(browser, tmp_path, serve_bundle):
    pages = []

    def open_library(catalog=None, query="", width=1280, height=900):
        base = serve_bundle(_static_portal_dir(tmp_path, records() if catalog is None else catalog))
        page = browser.new_page(viewport={"width": width, "height": height})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/" + query)
        expect(page.locator("#statusPill")).not_to_have_text("● LOADING")
        pages.append((page, errors))
        return page

    yield open_library
    for page, errors in pages:
        page.close()
        assert errors == []


def titles(page):
    return page.locator(".card-title").all_text_contents()


def test_sections_counts_order_featured_and_back_to_studio(library):
    page = library()
    expect(page.locator("#videoCount")).to_have_text("4 videos")
    assert titles(page) == [r["title"] for r in records()]
    assert page.locator(".library-section h2").all_text_contents() == [
        "Featured", "Workflows", "AI & RAG", "Documentation"]
    assert page.locator(".filter-btn").all_text_contents() == [
        "All", "Getting Started", "Workflows", "AI & RAG", "Documentation"]
    expect(page.get_by_role("link", name="← Back to Studio")).to_have_attribute("href", "/studio")
    assert page.locator(".thumb-duration").all_text_contents() == ["2:05", "12:34", "1:01:01"]
    page.get_by_role("button", name="Getting Started", exact=True).click()
    assert titles(page) == ["Getting Started"]  # featured videos still participate in filters
    expect(page.get_by_role("button", name="Getting Started", exact=True)).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="All", exact=True).click()
    expect(page.locator(".video-card")).to_have_count(4)


def test_production_public_bundle_links_to_canonical_studio_across_origins(
    content_dir, tmp_path, serve_bundle, browser,
):
    out = tmp_path / "production-bundle"
    bp.build(content_dir, ROOT / "portal", out)
    page = browser.new_page()
    page.goto(serve_bundle(_assemble_www(out)))
    link = page.get_by_role("link", name="← Back to Studio")
    expect(link).to_have_attribute("href", "https://webstudio.omnibioai.org/studio")
    assert page.evaluate("new URL(document.querySelector('.back-to-studio').href).origin") != page.evaluate("location.origin")
    assert titles(page) == ["pub.mp4-TITLE-SENTINEL"]
    assert "review.mp4-TITLE-SENTINEL" not in page.locator("body").inner_text()
    page.close()


def test_build_uses_configured_studio_origin_and_supports_local_development(
    content_dir, tmp_path, monkeypatch,
):
    monkeypatch.setenv("STUDIO_URL", "https://studio-staging.example.org")
    out = tmp_path / "configured-bundle"
    bp.build(content_dir, ROOT / "portal", out)
    html = (out / "www" / "index.html").read_text()
    assert 'href="https://studio-staging.example.org/studio"' in html

    out = tmp_path / "local-bundle"
    bp.build(content_dir, ROOT / "portal", out, studio_url="http://localhost:5173")
    html = (out / "www" / "index.html").read_text()
    assert 'href="http://localhost:5173/studio"' in html



@pytest.mark.parametrize("value", [
    "javascript:alert(1)",
    "data:text/html,unsafe",
    "http://webstudio.omnibioai.org",
    "https://user:password@studio.example.org",
    "https://studio.example.org/path",
    "https://studio.example.org/?next=/evil",
    "https://studio.example.org?",
    "https://studio.example.org/#fragment",
    "https://studio.example.org#",
    "https://bad host.example.org",
    "https://studio.example.org:99999",
    " https://studio.example.org",
])
def test_malformed_or_unsafe_studio_url_fails_before_output_changes(content_dir, tmp_path, value):
    out = tmp_path / "existing-output"
    out.mkdir()
    sentinel = out / "keep.txt"
    sentinel.write_text("untouched")
    with pytest.raises(bp.PublishError, match="STUDIO_URL"):
        bp.build(content_dir, ROOT / "portal", out, studio_url=value)
    assert sentinel.read_text() == "untouched"


@pytest.mark.parametrize("query", [
    "Running", "differential expression", "transcriptomics", "RUNNING", "RNA-seq", "rnaseq", "RNA seq",
    "gene counts", "Nextflow", "Workflow Runner", "nf-core", "Workflows",
])
def test_search_fields_and_normalization(library, query):
    page = library()
    page.get_by_role("combobox").fill(query)
    expected = ["Running an RNA-seq Workflow", "Knowledge Search"] if query == "transcriptomics" else ["Running an RNA-seq Workflow"]
    assert titles(page) == expected
    expect(page.locator(".library-section h2")).to_have_text("Search Results")
    expect(page.locator("#resultStatus")).to_contain_text(f'for “{query}”')


def test_combined_filters_clear_and_empty_state(library):
    page = library()
    page.get_by_role("button", name="Workflows", exact=True).click()
    search = page.get_by_role("combobox")
    search.fill("transcriptomics")
    assert titles(page) == ["Running an RNA-seq Workflow"]
    search.fill("no such recording")
    expect(page.get_by_role("heading", name="No videos found for “no such recording”")).to_be_visible()
    expect(page.locator(".library-section")).to_have_count(0)
    expect(page.locator("#videoCount")).to_have_text("0 videos")
    expect(page.locator("#suggestions")).to_be_hidden()
    page.get_by_role("button", name="Clear search").click()
    assert titles(page) == ["Running an RNA-seq Workflow"]
    page.get_by_role("button", name="All", exact=True).click()
    expect(page.locator(".video-card")).to_have_count(4)
    search.fill("rna")
    search.fill("")
    expect(page.locator(".video-card")).to_have_count(4)
    expect(page.locator("#suggestions")).to_be_hidden()


def test_autocomplete_keyboard_enter_escape_outside_and_focus(library):
    page = library()
    search = page.get_by_role("combobox")
    search.fill("rna")
    expect(search).to_have_attribute("aria-expanded", "true")
    expect(page.get_by_role("listbox")).to_be_visible()
    assert 2 <= page.get_by_role("option").count() <= 8
    # RNA-seq / rnaseq share a suggestion key.
    assert page.locator(".suggestion-text").all_text_contents().count("RNA-seq") == 1
    assert "rnaseq" not in page.locator(".suggestion-text").all_text_contents()
    search.press("ArrowUp")
    expect(page.get_by_role("option").last).to_have_attribute("aria-selected", "true")
    search.press("ArrowDown")
    expect(page.get_by_role("option").first).to_have_attribute("aria-selected", "true")
    expect(search).to_have_attribute("aria-activedescendant", "suggestion-0")
    search.press("ArrowDown")
    expect(page.get_by_role("option").nth(1)).to_have_attribute("aria-selected", "true")
    search.press("ArrowUp")
    search.press("Enter")
    expect(page.get_by_role("dialog")).to_be_visible()
    expect(page.locator("#modalTitle")).to_have_text("Running an RNA-seq Workflow")
    expect(page.locator("#modalVideo")).to_have_attribute("src", "/videos/rna.mp4")
    expect(page.get_by_role("button", name="Close", exact=True)).to_be_focused()
    page.keyboard.press("Escape")
    expect(search).to_be_focused()
    search.fill("rna")
    search.press("Escape")
    expect(search).to_have_attribute("aria-expanded", "false")
    expect(search).not_to_have_attribute("aria-activedescendant", "suggestion-0")
    search.press("ArrowDown")
    expect(page.get_by_role("listbox")).to_be_visible()
    page.get_by_role("heading", name="Video Tutorials").click()
    expect(page.get_by_role("listbox")).to_be_hidden()
    search.fill("rna")
    search.press("Tab")
    expect(page.get_by_role("listbox")).to_be_hidden()


def test_autocomplete_click_terms_categories_and_category_scope(library):
    page = library()
    search = page.get_by_role("combobox")
    search.fill("rna")
    page.get_by_role("option", name="Keyword RNA-seq", exact=True).click()
    expect(search).to_have_value("RNA-seq")
    assert titles(page) == ["Running an RNA-seq Workflow"]
    expect(page.get_by_role("listbox")).to_be_hidden()
    search.fill("work")
    page.get_by_role("option", name="Category Workflows", exact=True).click()
    expect(search).to_have_value("")
    expect(page.get_by_role("button", name="Workflows", exact=True)).to_have_attribute("aria-pressed", "true")
    search.fill("transcriptomics")
    assert "Knowledge Search" not in page.get_by_role("listbox").inner_text()
    page.get_by_role("option", name="Video Running an RNA-seq Workflow", exact=True).click()
    expect(page.get_by_role("dialog")).to_be_visible()


def test_url_state_bookmarks_and_history(library):
    page = library(query="?q=RNA+seq&category=workflows")
    expect(page.get_by_role("combobox")).to_have_value("RNA seq")
    assert titles(page) == ["Running an RNA-seq Workflow"]
    page.reload()
    expect(page.locator(".video-card")).to_have_count(1)
    expect(page.get_by_role("button", name="Workflows", exact=True)).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="Clear search").click()
    assert page.url.endswith("?category=workflows")
    page.get_by_role("button", name="All", exact=True).click()
    assert page.url.endswith("/")
    page.evaluate("history.pushState(null, '', '?q=retrieval&category=ai-rag'); dispatchEvent(new PopStateEvent('popstate'))")
    assert titles(page) == ["Knowledge Search"]
    page = library(query="?category=unknown")
    expect(page.locator(".video-card")).to_have_count(4)


@pytest.mark.parametrize("width,columns", [(1920, 5), (1360, 4), (1024, 3), (760, 2), (390, 1), (320, 1)])
def test_hundred_video_catalog_layout_filtering_and_request_budget(library, width, columns):
    catalog = [dict(records()[1], filename=f"video-{i:03d}.mp4", title=f"RNA-seq tutorial {i:03d}",
                    order=i, desc="Long description " * 30, tags=[f"marker{i:03d}"]) for i in range(100)]
    page = library(catalog, width=width)
    expect(page.locator(".video-card")).to_have_count(100)
    expect(page.locator("#videoCount")).to_have_text("100 videos")
    shape = page.evaluate("""() => {
      const cards = [...document.querySelectorAll('.video-card')].map(c => c.getBoundingClientRect());
      return {columns: cards.filter(c => c.top === cards[0].top).length,
        overflow: document.documentElement.scrollWidth > innerWidth,
        previews: document.querySelectorAll('.thumb video[src]').length};
    }""")
    assert shape["columns"] == columns
    assert not shape["overflow"]
    assert shape["previews"] < 100  # offscreen media do not load eagerly
    page.locator(".video-card").first.evaluate("node => { window.originalCard = node; }")
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    search = page.get_by_role("combobox")
    search.fill("marker099")
    assert titles(page) == ["RNA-seq tutorial 099"]
    search.fill("rna")
    assert 1 <= page.get_by_role("option").count() <= 8
    bounds = page.get_by_role("listbox").bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    search.fill("")
    expect(page.locator(".video-card")).to_have_count(100)
    assert page.locator(".video-card").first.evaluate("node => node === window.originalCard")
    assert not any("videos.json" in url or "?q=" in url for url in requests)


def test_optional_metadata_degrades_gracefully_and_text_is_not_markup(library):
    page = library([
        None, "bad", {}, dict(filename="../bad.mp4"),
        dict(filename="minimal.mp4"),
        dict(filename="malformed.mp4", title="<img src=x onerror=alert(1)>", desc=["bad"],
             category={}, tags="rna", keywords=[{}, None, "usable"], order="bad", tag=[],
             featured="true", duration={}, thumbnail="javascript:alert(1)"),
        dict(filename="private.mp4", visibility="REVIEW_REQUIRED", title="Hidden review title", tags=["hidden"]),
        dict(filename="internal.mp4", visibility="INTERNAL", title="Hidden internal title"),
    ])
    expect(page.locator("#videoCount")).to_have_text("2 videos")
    assert titles(page) == ["<img src=x onerror=alert(1)>", "minimal.mp4"]
    assert page.locator(".library-section h2").all_text_contents() == ["Tutorial"]
    expect(page.locator(".card-title img")).to_have_count(0)
    expect(page.locator(".thumb-duration")).to_have_count(0)
    page.get_by_role("combobox").fill("usable")
    expect(page.locator(".video-card")).to_have_count(1)
    page.get_by_role("combobox").fill("hidden")
    expect(page.locator(".video-card")).to_have_count(0)
    expect(page.get_by_role("listbox")).to_be_hidden()


def test_nonpublic_metadata_never_reaches_bundle_search_or_autocomplete(content_dir, tmp_path, serve_bundle, browser):
    manifest = json.loads((content_dir / "videos.json").read_text())
    for entry in manifest[1:]:
        entry.update(category="Classified-Sentinel", tags=["classified-tags"], keywords=["classified-keywords"],
                     services=["classified-service"], modules=["classified-module"], workflows=["classified-workflow"])
    manifest[0].update(category="Platform", tags=["public-discovery"])
    _write_manifest(content_dir, manifest)
    out = tmp_path / "bundle"
    bp.build(content_dir, ROOT / "portal", out)
    blob = "\n".join(p.read_text() for p in out.rglob("*") if p.is_file())
    assert "classified-" not in blob.lower()
    for name in NON_PUBLIC:
        assert name not in blob
    page = browser.new_page()
    page.goto(serve_bundle(_assemble_www(out)))
    expect(page.locator("#videoCount")).to_have_text("1 video")
    assert titles(page) == ["pub.mp4-TITLE-SENTINEL"]
    assert page.locator(".filter-btn").all_text_contents() == ["All", "Platform"]
    page.get_by_role("combobox").fill("public-discovery")
    expect(page.get_by_role("option")).to_have_count(2)
    for query in ["classified", "review.mp4", "internal.mp4", "NOTES-SENTINEL"]:
        page.get_by_role("combobox").fill(query)
        expect(page.locator(".video-card")).to_have_count(0)
        expect(page.get_by_role("listbox")).to_be_hidden()
    page.close()


def test_build_projects_only_well_formed_optional_discovery_fields(content_dir, tmp_path):
    entry = _entry("pub.mp4", "PUBLIC", category=" Workflows ", featured=True,
                   tags=["rna-seq", "rna-seq", None, "", 3, "x" * 121], keywords="invalid",
                   services=["Nextflow"], modules=["Runner"], workflows=["RNA-seq"], notes="private")
    _write_manifest(content_dir, [entry])
    out = tmp_path / "bundle"
    bp.build(content_dir, ROOT / "portal", out)
    record = json.loads((out / "publication/entries/001.json").read_text())
    assert record["tags"] == ["rna-seq"] and record["category"] == "Workflows"
    assert record["featured"] is True
    assert record["services"] == ["Nextflow"] and record["modules"] == ["Runner"]
    assert record["workflows"] == ["RNA-seq"]
    assert "keywords" not in record and "notes" not in record
    entry.update(category=[], featured="true", tags={}, duration=float("inf"))
    _write_manifest(content_dir, [entry])
    bp.build(content_dir, ROOT / "portal", out)
    record = json.loads((out / "publication/entries/001.json").read_text())
    assert not {"category", "featured", "tags", "duration"} & record.keys()


@pytest.mark.parametrize("field", ["category", "tags", "keywords", "services", "modules", "workflows"])
@pytest.mark.parametrize("secret", ["/home/private/project", "C:\\Users\\private"])
def test_new_public_fields_are_secret_scanned(content_dir, tmp_path, field, secret):
    value = secret if field == "category" else [secret]
    _write_manifest(content_dir, [_entry("pub.mp4", "PUBLIC", **{field: value})])
    with pytest.raises(bp.PublishError, match="forbidden content"):
        bp.build(content_dir, ROOT / "portal", tmp_path / "out")
