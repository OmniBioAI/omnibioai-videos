#!/usr/bin/env python3
"""Generate the public video portal's publication artifacts from the source manifest.

Publication model
-----------------
``videos.json`` is the publication control plane. Every entry carries a
``visibility`` of exactly ``PUBLIC``, ``INTERNAL`` or ``REVIEW_REQUIRED``. Only
``PUBLIC`` entries are ever published, and the decision controls BOTH catalog
discovery and direct media access: the nginx allowlist generated here contains
one exact-match ``location`` per PUBLIC file and nothing else, so every other
file in the (read-only) host content directory answers 404.

Fail closed: a missing, misspelled, wrongly-cased or non-string ``visibility`` --
and any media file that is not registered in the manifest -- is treated as not
public. A PUBLIC entry must also carry ``approved_sha256`` and
``approved_size_bytes`` of the exact bytes that were approved. Approval is bound
to content, not to a filename: the host file must exist (regular file, not a
symlink), be non-empty, have the approved size and SHA-256, and be a structurally
valid MP4/WebM, otherwise the build aborts. The same check is repeated at
runtime by ``runtime/publish-runtime.sh`` so a file replaced after deployment is
delisted and 404s.

No media is ever copied: the video files stay on the host and are mounted
read-only into the container at request time.

Output layout (``--out``)::

    www/index.html, portal.css, portal.js      web root (static portal only)
    publication/approved.tsv                   id <TAB> sha256 <TAB> size <TAB> filename
    publication/entries/<id>.json              one public catalog record
    publication/entries/<id>.conf              exact-match nginx location for that file

Usage::

    python scripts/build_public.py [--content content] [--media-dir DIR] [--portal portal] [--out dist/public]

Developer: Manish Kumar <manish@omnibioai.org>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import struct
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
CONTENT_MOUNT = "/content"  # where the host content directory is mounted (read-only) in the container

# Static portal files copied verbatim into the web root (allowlist).
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

TEXT_SUFFIXES = (".html", ".htm", ".js", ".mjs", ".css", ".json", ".txt", ".svg", ".map", ".xml", ".tsv", ".conf")


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

    Also flags source maps, dotfiles, env/key files and any media file outright --
    none of them have any business in the generated bundle whatever they contain.
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
        if name.endswith(MEDIA_SUFFIXES):
            hits.append((rel, "media-file-in-bundle"))
        if name.endswith(TEXT_SUFFIXES):
            for pattern in scan_text(path.read_text(encoding="utf-8", errors="replace")):
                hits.append((rel, pattern))
    return hits


def load_manifest(manifest_path: Path) -> list:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PublishError(f"manifest not found: {manifest_path}") from None
    except json.JSONDecodeError as exc:
        raise PublishError(f"manifest is not valid JSON: {exc}") from None
    if not isinstance(manifest, list):
        raise PublishError("manifest must be a JSON array")
    return manifest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_container(path: Path, name: str) -> None:
    """Structural sanity check that a file is a complete MP4/WebM container, not just non-empty.

    MP4: walk the top-level boxes; require ``ftyp`` first, both ``mdat`` and ``moov`` present, and
    boxes that end exactly at end-of-file (catches truncated / partially written files).
    WebM: require the EBML magic. This is not a decoder; use ffprobe/ffmpeg when approving.
    """
    size = path.stat().st_size
    with path.open("rb") as handle:
        if path.suffix.lower() == ".webm":
            if handle.read(4) != b"\x1a\x45\xdf\xa3":
                raise PublishError(f"{name}: not a valid WebM (missing EBML header)")
            return
        boxes: list[bytes] = []
        pos = 0
        while pos < size:
            handle.seek(pos)
            head = handle.read(16)
            if len(head) < 8:
                raise PublishError(f"{name}: not a valid MP4 (truncated box header at byte {pos})")
            box_size, box_type = struct.unpack(">I4s", head[:8])
            header = 8
            if box_size == 1:
                if len(head) < 16:
                    raise PublishError(f"{name}: not a valid MP4 (truncated 64-bit box at byte {pos})")
                box_size, header = struct.unpack(">Q", head[8:16])[0], 16
            elif box_size == 0:
                box_size = size - pos
            if box_size < header or pos + box_size > size:
                raise PublishError(f"{name}: not a valid MP4 (box {box_type!r} at byte {pos} overruns the file)")
            boxes.append(box_type)
            pos += box_size
    if not boxes or boxes[0] != b"ftyp":
        raise PublishError(f"{name}: not a valid MP4 (no leading ftyp box)")
    if b"moov" not in boxes or b"mdat" not in boxes:
        raise PublishError(f"{name}: not a valid MP4 (missing moov or mdat box)")


def _validate_public_entry(entry: dict, media_dir: Path) -> dict:
    """Validate one PUBLIC entry against the host file; return its public projection + approval."""
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

    approved_sha = entry.get("approved_sha256")
    if not isinstance(approved_sha, str) or not SHA256_RE.fullmatch(approved_sha):
        raise PublishError(f"{name}: PUBLIC entry needs the lowercase hex 'approved_sha256' of the approved file")
    approved_size = entry.get("approved_size_bytes")
    if not isinstance(approved_size, int) or isinstance(approved_size, bool) or approved_size <= 0:
        raise PublishError(f"{name}: PUBLIC entry needs a positive integer 'approved_size_bytes'")

    media = media_dir / name
    if media.is_symlink() or not media.is_file():
        raise PublishError(f"{name}: media file is missing or is not a regular file")
    actual_size = media.stat().st_size
    if actual_size == 0:
        raise PublishError(f"{name}: media file is empty (0 bytes)")
    if actual_size != approved_size:
        raise PublishError(f"{name}: size mismatch (approved {approved_size}, found {actual_size}); re-review required")
    check_container(media, name)
    if sha256_file(media) != approved_sha:
        raise PublishError(
            f"{name}: file content changed since it was approved (sha256 mismatch); "
            "re-review it and update 'approved_sha256'"
        )

    projected = {field: entry[field] for field in PUBLIC_FIELDS}
    findings = scan_text(f"{projected['title']}\n{projected['desc']}")
    if findings:
        raise PublishError(f"{name}: title/description contain forbidden content: {findings}")
    return {"public": projected, "sha256": approved_sha, "size": approved_size}


def select_public(manifest: list, media_dir: Path) -> tuple[list[dict], dict]:
    """Return ``(approved_items, report)``; each item is ``{public, sha256, size}``.

    ``report`` has ``counts`` (per visibility, incl. UNCLASSIFIED) and
    ``unregistered_media`` (media files in ``media_dir`` absent from the manifest --
    treated as non-public).
    """
    counts = {v: 0 for v in (*VISIBILITIES, UNCLASSIFIED)}
    items: list[dict] = []
    seen: set[str] = set()
    registered: set[str] = set()

    for entry in manifest:
        visibility = classify(entry)
        counts[visibility] += 1
        if isinstance(entry, dict) and isinstance(entry.get("filename"), str):
            registered.add(entry["filename"])
        if visibility != PUBLIC:
            continue
        item = _validate_public_entry(entry, media_dir)
        filename = item["public"]["filename"]
        if filename in seen:
            raise PublishError(f"duplicate PUBLIC filename: {filename}")
        seen.add(filename)
        items.append(item)

    items.sort(key=lambda i: (i["public"]["order"], i["public"]["filename"]))
    unregistered = sorted(
        p.name
        for p in media_dir.iterdir()
        if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES and p.name not in registered
    )
    return items, {"counts": counts, "unregistered_media": unregistered}


def location_block(filename: str) -> str:
    """The one exact-match nginx location that publishes ``filename`` from the read-only mount."""
    return (
        f"location = /videos/{filename} {{\n"
        f"    alias {CONTENT_MOUNT}/{filename};\n"
        "    types { video/mp4 mp4; video/webm webm; }\n"
        "    default_type application/octet-stream;\n"
        "    disable_symlinks on;\n"
        "    expires 5m;\n"
        "}\n"
    )


def build(
    content_dir: Path,
    portal_dir: Path,
    out_dir: Path,
    media_dir: Path | None = None,
    manifest_path: Path | None = None,
) -> dict:
    """Generate the publication artifacts in ``out_dir`` (recreated from scratch).

    ``content_dir`` holds ``videos.json`` unless ``manifest_path`` is given; ``media_dir`` (default
    ``content_dir``) is the host directory whose files are verified against the approved hashes.
    """
    manifest = load_manifest(manifest_path or content_dir / "videos.json")
    items, report = select_public(manifest, media_dir or content_dir)

    for name in PORTAL_FILES:
        if not (portal_dir / name).is_file():
            raise PublishError(f"portal file missing: {portal_dir / name}")

    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "www").mkdir(parents=True)
    entries = out_dir / "publication" / "entries"
    entries.mkdir(parents=True)

    for name in PORTAL_FILES:
        shutil.copyfile(portal_dir / name, out_dir / "www" / name)

    tsv = []
    for index, item in enumerate(items, 1):
        ident = f"{index:03d}"
        filename = item["public"]["filename"]
        (entries / f"{ident}.json").write_text(
            json.dumps(item["public"], separators=(",", ":"), ensure_ascii=False), encoding="utf-8"
        )
        (entries / f"{ident}.conf").write_text(location_block(filename), encoding="utf-8")
        tsv.append(f"{ident}\t{item['sha256']}\t{item['size']}\t{filename}\n")
    (out_dir / "publication" / "approved.tsv").write_text("".join(tsv), encoding="utf-8")

    hits = scan_tree(out_dir)
    if hits:
        shutil.rmtree(out_dir)
        detail = "; ".join(f"{path}: {pattern}" for path, pattern in hits)
        raise PublishError(f"forbidden content in public bundle: {detail}")

    report["published"] = [i["public"]["filename"] for i in items]
    report["approved"] = {i["public"]["filename"]: {"sha256": i["sha256"], "size": i["size"]} for i in items}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--content", type=Path, default=Path("content"), help="directory holding videos.json")
    parser.add_argument("--manifest", type=Path, help="videos.json path (default: <content>/videos.json)")
    parser.add_argument("--media-dir", type=Path, help="host media directory to verify (default: --content)")
    parser.add_argument("--portal", type=Path, default=Path("portal"))
    parser.add_argument("--out", type=Path, default=Path("dist/public"))
    args = parser.parse_args(argv)

    try:
        report = build(args.content, args.portal, args.out, args.media_dir, args.manifest)
    except PublishError as exc:
        print(f"BUILD FAILED (nothing published): {exc}", file=sys.stderr)
        return 1

    print("Visibility inventory:")
    for name, count in report["counts"].items():
        print(f"  {name:<16}{count}")
    if report["unregistered_media"]:
        print(f"  unregistered media (NOT published): {', '.join(report['unregistered_media'])}")
    print(f"Publication artifacts for {len(report['published'])} video(s) written to {args.out} (no media copied)")
    for name, approval in report["approved"].items():
        print(f"  + {name}  size={approval['size']}  sha256={approval['sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
