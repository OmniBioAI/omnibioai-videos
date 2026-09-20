#!/usr/bin/env python3
"""Build the public video portal bundle from the source content directory.

Publication model
-----------------
``content/videos.json`` is the classification registry. Every entry carries a
``visibility`` of exactly ``PUBLIC``, ``INTERNAL`` or ``REVIEW_REQUIRED``.

Only ``PUBLIC`` entries are published. The rule is *fail closed*: a missing,
misspelled, wrongly-cased or non-string ``visibility`` -- and any media file
that is not registered in the manifest at all -- is treated as not public and
is never copied into the output. Nothing else in ``content/`` (guide.html, the
internal index.html, stray files, dotfiles) is ever copied either: the output
is assembled from an explicit allowlist, not by filtering a directory copy.

A PUBLIC entry must also carry the ``sha256`` of the exact bytes that were
reviewed. Approval is bound to content, not to a filename: if the file is
replaced after review the hash no longer matches and the build aborts.

A PUBLIC entry that is malformed (bad filename, missing/empty/symlinked media,
unknown tag, missing or mismatching sha256, text that looks like a secret or an
internal path) aborts the build instead of being silently dropped, so a mistake
cannot quietly publish half of what the owner intended.

Usage::

    python scripts/build_public.py [--content content] [--portal portal] [--out dist/public]

Developer: Manish Kumar <manish@omnibioai.org>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

PUBLIC = "PUBLIC"
INTERNAL = "INTERNAL"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
VISIBILITIES = (PUBLIC, INTERNAL, REVIEW_REQUIRED)
UNCLASSIFIED = "UNCLASSIFIED"  # missing / unknown visibility -> treated as non-public

# Only these manifest fields ever reach the public catalog.
PUBLIC_FIELDS = ("filename", "title", "desc", "tag", "order")
ALLOWED_TAGS = ("intro", "tutorial", "workflow", "demo", "hpc")
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.(mp4|webm)$")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
MEDIA_SUFFIXES = (".mp4", ".webm", ".mov")

# Static portal files copied verbatim (allowlist).
PORTAL_FILES = ("index.html", "portal.css", "portal.js")

# Text that must never appear in anything we publish. Deliberately broad:
# a false positive stops the build, a false negative publishes a secret.
FORBIDDEN_PATTERNS = {
    "anthropic-key": re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    "openai-style-key": re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    "aws-access-key-id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "private-key-block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"),
    "vite-env": re.compile(r"\bVITE_[A-Z0-9_]+"),
    "bearer-token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    "credential-assignment": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|passw(?:or)?d|token)\b\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]{8,}"
    ),
    "home-path": re.compile(r"(?:/home/[A-Za-z0-9._-]+|/Users/[A-Za-z0-9._-]+|/root/|C:\\Users\\)"),
    "private-ip": re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
    ),
    "visibility-marker": re.compile(r"\b(?:REVIEW_REQUIRED|INTERNAL)\b"),
}

TEXT_SUFFIXES = (".html", ".htm", ".js", ".mjs", ".css", ".json", ".txt", ".svg", ".map", ".xml")


class PublishError(Exception):
    """Raised when the public bundle cannot be built safely."""


def classify(entry) -> str:
    """Return the entry's visibility, or UNCLASSIFIED for anything not exactly valid."""
    if not isinstance(entry, dict):
        return UNCLASSIFIED
    value = entry.get("visibility")
    if isinstance(value, str) and value in VISIBILITIES:
        return value
    return UNCLASSIFIED


def scan_text(text: str) -> list[str]:
    """Return the names of forbidden patterns found in ``text``."""
    return [name for name, pattern in FORBIDDEN_PATTERNS.items() if pattern.search(text)]


def scan_tree(root: Path) -> list[tuple[str, str]]:
    """Scan every text-like file under ``root``; return ``(relative path, pattern)`` hits.

    Also flags source maps, dotfiles and env files outright -- they have no
    business in a public bundle whatever they contain.
    """
    hits: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        name = path.name.lower()
        if name.endswith(".map"):
            hits.append((rel, "source-map"))
        if name.startswith(".") or name.endswith((".env", ".pem", ".key")) or name == "env":
            hits.append((rel, "dotfile-or-secret-file"))
        if name.endswith(TEXT_SUFFIXES):
            for pattern in scan_text(path.read_text(encoding="utf-8", errors="replace")):
                hits.append((rel, pattern))
    return hits


def load_manifest(content_dir: Path) -> list:
    manifest_path = content_dir / "videos.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PublishError(f"manifest not found: {manifest_path}") from None
    except json.JSONDecodeError as exc:
        raise PublishError(f"manifest is not valid JSON: {exc}") from None
    if not isinstance(manifest, list):
        raise PublishError("manifest must be a JSON array")
    return manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_public_entry(entry: dict, content_dir: Path) -> dict:
    """Validate one PUBLIC entry and return its allowlisted public projection."""
    name = entry.get("filename")
    if not isinstance(name, str) or not FILENAME_RE.fullmatch(name):
        raise PublishError(f"PUBLIC entry has an invalid filename: {name!r}")
    for field in ("title", "desc"):
        if not isinstance(entry.get(field), str) or not entry[field].strip():
            raise PublishError(f"{name}: PUBLIC entry needs a non-empty '{field}'")
    if entry.get("tag") not in ALLOWED_TAGS:
        raise PublishError(f"{name}: tag must be one of {ALLOWED_TAGS}, got {entry.get('tag')!r}")
    order = entry.get("order")
    if not isinstance(order, int) or isinstance(order, bool):
        raise PublishError(f"{name}: 'order' must be an integer")

    media = content_dir / name
    if media.is_symlink() or not media.is_file():
        raise PublishError(f"{name}: media file is missing or is not a regular file")
    if media.stat().st_size == 0:
        raise PublishError(f"{name}: media file is empty (0 bytes)")
    approved = entry.get("sha256")
    if not isinstance(approved, str) or not SHA256_RE.fullmatch(approved):
        raise PublishError(f"{name}: PUBLIC entry needs the lowercase hex 'sha256' of the reviewed file")
    if _sha256(media) != approved:
        raise PublishError(
            f"{name}: file content changed since it was approved (sha256 mismatch); "
            "re-review it and update 'sha256'"
        )

    projected = {field: entry[field] for field in PUBLIC_FIELDS}
    findings = scan_text(f"{projected['title']}\n{projected['desc']}")
    if findings:
        raise PublishError(f"{name}: title/description contain forbidden content: {findings}")
    return projected


def select_public(manifest: list, content_dir: Path) -> tuple[list[dict], dict]:
    """Return ``(public_entries, report)`` for a manifest.

    ``report`` has ``counts`` (per visibility, incl. UNCLASSIFIED) and
    ``unregistered_media`` (media files in ``content_dir`` absent from the
    manifest -- treated as non-public).
    """
    counts = {v: 0 for v in (*VISIBILITIES, UNCLASSIFIED)}
    public: list[dict] = []
    seen: set[str] = set()
    registered: set[str] = set()

    for entry in manifest:
        visibility = classify(entry)
        counts[visibility] += 1
        if isinstance(entry, dict) and isinstance(entry.get("filename"), str):
            registered.add(entry["filename"])
        if visibility != PUBLIC:
            continue
        projected = _validate_public_entry(entry, content_dir)
        if projected["filename"] in seen:
            raise PublishError(f"duplicate PUBLIC filename: {projected['filename']}")
        seen.add(projected["filename"])
        public.append(projected)

    public.sort(key=lambda e: (e["order"], e["filename"]))
    unregistered = sorted(
        p.name
        for p in content_dir.iterdir()
        if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES and p.name not in registered
    )
    return public, {"counts": counts, "unregistered_media": unregistered}


def build(content_dir: Path, portal_dir: Path, out_dir: Path) -> dict:
    """Assemble the public bundle in ``out_dir`` (recreated from scratch)."""
    manifest = load_manifest(content_dir)
    public, report = select_public(manifest, content_dir)

    for name in PORTAL_FILES:
        if not (portal_dir / name).is_file():
            raise PublishError(f"portal file missing: {portal_dir / name}")

    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "videos").mkdir(parents=True)

    for name in PORTAL_FILES:
        shutil.copyfile(portal_dir / name, out_dir / name)
    for entry in public:
        shutil.copyfile(content_dir / entry["filename"], out_dir / "videos" / entry["filename"])
    (out_dir / "videos.json").write_text(
        json.dumps(public, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    hits = scan_tree(out_dir)
    if hits:
        shutil.rmtree(out_dir)
        detail = "; ".join(f"{path}: {pattern}" for path, pattern in hits)
        raise PublishError(f"forbidden content in public bundle: {detail}")

    report["published"] = [e["filename"] for e in public]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--content", type=Path, default=Path("content"))
    parser.add_argument("--portal", type=Path, default=Path("portal"))
    parser.add_argument("--out", type=Path, default=Path("dist/public"))
    args = parser.parse_args(argv)

    try:
        report = build(args.content, args.portal, args.out)
    except PublishError as exc:
        print(f"BUILD FAILED (nothing published): {exc}", file=sys.stderr)
        return 1

    print("Visibility inventory:")
    for name, count in report["counts"].items():
        print(f"  {name:<16}{count}")
    if report["unregistered_media"]:
        print(f"  unregistered media (NOT published): {', '.join(report['unregistered_media'])}")
    print(f"Published {len(report['published'])} video(s) to {args.out}")
    for name in report["published"]:
        print(f"  + {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
