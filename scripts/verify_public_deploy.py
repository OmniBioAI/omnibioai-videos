#!/usr/bin/env python3
"""Verify a running public video portal: the site as an anonymous client sees it, and the container.

    python scripts/verify_public_deploy.py site https://videos.omnibioai.org --media-dir content
    python scripts/verify_public_deploy.py site http://127.0.0.1:8087        --media-dir content
    python scripts/verify_public_deploy.py container omnibioai-videos-public --content-dir content --bundle dist/public

``site`` derives what SHOULD be public from the source manifest (re-verifying each approved file's
size and SHA-256 on the host) and checks the server against it: the catalog lists exactly the PUBLIC
records, each approved video streams (HEAD/GET/Range/206/416, correct bytes) and EVERY other file in the
host content directory -- plus guide.html, dotfiles, traversal paths, the directory itself -- is
unreachable, writes are rejected, security headers are present and no restricted metadata leaks.
Large files are never downloaded: only headers and a few small ranges are read.

``container`` audits the docker container: loopback-only binding, read-only rootfs, cap_drop ALL, a
single read-only content mount, no media inside the image, and a web root identical to the bundle.

Exit status is non-zero if any check fails.

Developer: Manish Kumar <manish@omnibioai.org>
"""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path


def _load_builder():
    spec = importlib.util.spec_from_file_location("build_public", Path(__file__).with_name("build_public.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_public"] = module
    spec.loader.exec_module(module)
    return module


bp = _load_builder()

SECRET_ENV_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|VITE_)", re.IGNORECASE)
ALLOWED_ENV = {"PATH", "NGINX_VERSION", "NJS_VERSION", "NJS_RELEASE", "PKG_RELEASE", "DYNPKG_RELEASE",
               "ACME_VERSION", "PUBLISH_POLL_SECONDS", "PUBLISH_REHASH_SECONDS", "HOSTNAME", "HOME"}
TRAVERSAL_PATHS = ["/../etc/passwd", "/%2e%2e/etc/passwd", "/videos/../guide.html", "/videos/..%2f..%2fetc/passwd",
                   "/videos/%2e%2e/%2e%2e/etc/passwd", "//etc/passwd", "/videos/./../.env"]
# Names the public portal legitimately serves at the root with ITS OWN content (the generated catalog
# and the portal page). A host file with the same name (e.g. the source manifest) must not be what is served.
PORTAL_ROUTES = {"index.html", "videos.json"}
STATIC_PROBES = ["/.env", "/guide.html", "/my_video.mov", "/videos/my_video.mov", "/videos/guide.html",
                 "/videos/videos.json", "/videos.json.map", "/portal.js.map", "/content/videos.json",
                 "/index.html/", "/health/x", "/upload", "/admin", "/api/videos", "/50x.html"]


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""


class Client:
    """Anonymous HTTP(S) client: no cookies, no credentials, no redirect following."""

    def __init__(self, base_url: str):
        parts = urllib.parse.urlsplit(base_url)
        self.https = parts.scheme == "https"
        self.host = parts.hostname
        self.port = parts.port or (443 if self.https else 80)

    def _conn(self):
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=30)

    def request(self, method, path, headers=None, read=True, limit=None):
        conn = self._conn()
        try:
            conn.request(method, path, headers=headers or {})
            resp = conn.getresponse()
            body = (resp.read() if limit is None else resp.read(limit)) if read else b""
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
        finally:
            conn.close()  # closing without reading the rest is how a large GET is "headers only"


def check_site(base_url: str, manifest_path: Path, media_dir: Path) -> list[Result]:
    results: list[Result] = []

    def rec(name, ok, detail=""):
        results.append(Result(name, bool(ok), detail))

    client = Client(base_url)
    manifest = bp.load_manifest(manifest_path)
    items, report = bp.select_public(manifest, media_dir)  # re-verifies host files against approved hashes
    approved = {i["public"]["filename"]: i for i in items}
    rec("host files match approved sha256/size", True, f"{len(items)} PUBLIC video(s) verified on host")

    # ── portal + catalog ────────────────────────────────────────────────────────
    for path in ("/", "/portal.css", "/portal.js", "/health"):
        status, _, _ = client.request("GET", path)
        rec(f"GET {path} -> 200", status == 200, str(status))
    status, headers, body = client.request("GET", "/videos.json")
    try:
        catalog = json.loads(body)
    except ValueError:
        catalog = None
    expected = [i["public"] for i in items]
    rec("GET /videos.json -> 200 application/json", status == 200 and headers.get("content-type", "").startswith("application/json"),
        f"{status} {headers.get('content-type')}")
    rec(f"/videos.json is exactly the {len(expected)} PUBLIC record(s)", catalog == expected,
        f"got {len(catalog) if isinstance(catalog, list) else catalog!r} record(s)")
    if not expected:
        rec("/videos.json is exactly []", body.strip() == b"[]", body[:40].decode("utf-8", "replace"))

    # ── approved videos: streaming semantics ────────────────────────────────────
    for name, item in approved.items():
        path, size = f"/videos/{name}", item["size"]
        mime = "video/webm" if name.endswith(".webm") else "video/mp4"
        source = media_dir / name
        status, headers, _ = client.request("HEAD", path)
        rec(f"HEAD {path} -> 200 {mime}, Content-Length {size}, Accept-Ranges bytes",
            status == 200 and headers.get("content-type") == mime and headers.get("content-length") == str(size)
            and headers.get("accept-ranges") == "bytes", f"{status} {headers.get('content-type')} {headers.get('content-length')}")
        status, headers, _ = client.request("GET", path, read=False)
        rec(f"GET {path} -> 200 (headers only, body not downloaded)", status == 200, str(status))
        for label, spec, start, end in (
            ("first 1 KiB", "0-1023", 0, 1023),
            ("middle 4 KiB", f"{size // 2}-{size // 2 + 4095}", size // 2, size // 2 + 4095),
            ("suffix 1 KiB", "-1024", size - 1024, size - 1),
            ("last byte", f"{size - 1}-", size - 1, size - 1),
        ):
            end = min(end, size - 1)
            status, headers, body = client.request("GET", path, {"Range": f"bytes={spec}"})
            with source.open("rb") as handle:
                handle.seek(start)
                truth = handle.read(end - start + 1)
            rec(f"Range {label} -> 206, Content-Range bytes {start}-{end}/{size}, exact bytes",
                status == 206 and headers.get("content-range") == f"bytes {start}-{end}/{size}" and body == truth,
                f"{status} {headers.get('content-range')} bytes_match={body == truth}")
        status, headers, _ = client.request("GET", path, {"Range": "bytes=0-"}, read=False)
        rec("Range open-ended (bytes=0-) -> 206 without downloading", status == 206
            and headers.get("content-range") == f"bytes 0-{size - 1}/{size}", f"{status} {headers.get('content-range')}")
        status, _, _ = client.request("GET", path, {"Range": f"bytes={size + 100}-"})
        rec("Range beyond end -> 416", status == 416, str(status))

    # ── everything else must be unreachable ────────────────────────────────────
    host_files = sorted(p.name for p in media_dir.iterdir() if p.is_file())
    restricted = [n for n in host_files if n not in approved]
    for name in restricted:
        if name in PORTAL_ROUTES:
            status, _, served = client.request("GET", f"/{name}")
            hidden = client.request("GET", f"/videos/{name}", {"Range": "bytes=0-0"})[0]
            rec(f"host file {name}: not served (portal's own /{name} differs; /videos/{name} -> 404)",
                hidden == 404 and served != (media_dir / name).read_bytes(), f"/videos/ {hidden}, root {status}")
            continue
        codes = {p: client.request("GET", p, {"Range": "bytes=0-0"})[0] for p in (f"/videos/{name}", f"/{name}")}
        rec(f"restricted host file {name}: /videos/ and / -> 404", set(codes.values()) == {404}, str(codes))
    for path in STATIC_PROBES:
        status, _, body = client.request("GET", path, {"Range": "bytes=0-0"})
        rec(f"GET {path} -> 404", status == 404, str(status))
    for path in TRAVERSAL_PATHS:
        status, _, body = client.request("GET", path)
        rec(f"traversal {path} -> 400/404", status in (400, 404) and b"root:" not in body, str(status))
    status, _, body = client.request("GET", "/videos/")
    rec("directory listing unavailable (/videos/ -> 404)", status == 404 and b"Index of" not in body, str(status))
    for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
        codes = {p: client.request(method, p)[0] for p in ("/", "/videos.json", "/upload", "/api/videos")}
        rec(f"{method} rejected (405)", set(codes.values()) == {405}, str(codes))

    # ── headers + leaks ────────────────────────────────────────────────────────
    for path in ("/", "/videos.json", "/nope"):
        _, headers, _ = client.request("GET", path)
        good = ("default-src 'none'" in headers.get("content-security-policy", "")
                and headers.get("x-content-type-options") == "nosniff" and headers.get("x-frame-options") == "DENY"
                and headers.get("referrer-policy") == "no-referrer" and "access-control-allow-origin" not in headers
                and "set-cookie" not in headers and "x-powered-by" not in headers)
        rec(f"security headers on {path}", good, headers.get("content-security-policy", "")[:40])
    surfaces = {p: client.request("GET", p)[2].decode("utf-8", "replace") for p in ("/", "/portal.js", "/portal.css", "/videos.json", "/nope")}
    secret_bits = {"REVIEW_REQUIRED", "INTERNAL", "approved_sha256", *(set(restricted) - PORTAL_ROUTES)}
    for entry in manifest:
        if isinstance(entry, dict) and bp.classify(entry) != bp.PUBLIC:
            secret_bits.update(str(entry.get(k)) for k in ("filename", "title", "desc") if entry.get(k))
    leaked = sorted(bit for bit in secret_bits for text in surfaces.values() if bit in text)
    rec("no restricted metadata (names/titles/descriptions/markers) in any served page", not leaked, ", ".join(leaked[:5]))
    return results


def _docker_json(*args):
    return json.loads(subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout)


def _tree_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): bp.sha256_file(p) for p in sorted(root.rglob("*")) if p.is_file()}


def check_container(name: str, content_dir: Path, bundle_dir: Path | None = None, max_image_bytes: int = 150_000_000) -> list[Result]:
    results: list[Result] = []

    def rec(check, ok, detail=""):
        results.append(Result(check, bool(ok), detail))

    info = _docker_json("inspect", name)[0]
    host = info["HostConfig"]
    bindings = [b for blist in (info["NetworkSettings"]["Ports"] or {}).values() for b in (blist or [])]
    rec("published ports bound to 127.0.0.1 only", bool(bindings) and all(b["HostIp"] == "127.0.0.1" for b in bindings), str(bindings))
    rec("read-only root filesystem", host["ReadonlyRootfs"] is True)
    added = {c.removeprefix("CAP_") for c in host["CapAdd"] or []}
    dropped = [c.removeprefix("CAP_") for c in host["CapDrop"] or []]
    rec("cap_drop ALL (adds only CHOWN/SETGID/SETUID)", dropped == ["ALL"] and added <= {"CHOWN", "SETGID", "SETUID"},
        f"drop={dropped} add={sorted(added)}")
    rec("not privileged / host network / host pid", not host["Privileged"] and host["NetworkMode"] != "host" and not host["PidMode"])
    rec("no-new-privileges", any(o.startswith("no-new-privileges") for o in (host["SecurityOpt"] or [])), str(host["SecurityOpt"]))

    mounts = info["Mounts"]
    persistent = [m for m in mounts if m["Type"] != "tmpfs"]
    ok = (len(persistent) == 1 and persistent[0]["Type"] == "bind" and persistent[0]["Destination"] == "/content"
          and persistent[0]["RW"] is False
          and Path(persistent[0]["Source"]).resolve() == content_dir.resolve())
    rec("exactly one mount: host content dir -> /content, read-only (rest tmpfs)", ok, str([(m["Type"], m["Destination"], m["RW"]) for m in mounts]))

    env = [e.split("=", 1)[0] for e in info["Config"]["Env"] or []]
    rec("no secret-like environment variables", not [e for e in env if SECRET_ENV_RE.search(e)] and set(env) <= ALLOWED_ENV,
        ",".join(sorted(set(env) - ALLOWED_ENV)))
    size = _docker_json("image", "inspect", info["Image"])[0]["Size"]
    rec(f"image is small (< {max_image_bytes // 1_000_000} MB): no video packaged", size < max_image_bytes, f"{size} bytes")
    found = subprocess.run(
        ["docker", "exec", name, "sh", "-c",
         r"find / -xdev -type f \( -name '*.mp4' -o -name '*.mov' -o -name '*.webm' -o -name 'guide.html' -o -name 'videos.json.source' \) 2>/dev/null"],
        capture_output=True, text=True).stdout.split()
    rec("no media / guide.html / source content inside the image", found == [], " ".join(found))

    if bundle_dir is not None:
        with tempfile.TemporaryDirectory() as tmp:
            for src, dst in (("/usr/share/nginx/html", "www"), ("/etc/publication", "publication")):
                subprocess.run(["docker", "cp", f"{name}:{src}", f"{tmp}/{dst}"], capture_output=True, check=True)
            actual = {f"{d}/{k}": v for d in ("www", "publication") for k, v in _tree_hashes(Path(tmp) / d).items()}
        expected = {f"{d}/{k}": v for d in ("www", "publication") for k, v in _tree_hashes(bundle_dir / d).items()}
        rec("web root + publication data are exactly the generated bundle", actual == expected,
            f"{len(actual)} files; differing: {sorted(set(actual) ^ set(expected))[:4]}")
    return results


def _report(results: list[Result]) -> int:
    failed = [r for r in results if not r.ok]
    for r in results:
        print(f"{'PASS' if r.ok else 'FAIL'}  {r.name}" + (f"  [{r.detail}]" if r.detail and not r.ok else ""))
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + ("" if not failed else f"; {len(failed)} FAILED"))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    site = sub.add_parser("site")
    site.add_argument("base_url")
    site.add_argument("--manifest", type=Path)
    site.add_argument("--media-dir", type=Path, required=True)
    cont = sub.add_parser("container")
    cont.add_argument("name")
    cont.add_argument("--content-dir", type=Path, required=True)
    cont.add_argument("--bundle", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "site":
            results = check_site(args.base_url, args.manifest or args.media_dir / "videos.json", args.media_dir)
        else:
            results = check_container(args.name, args.content_dir, args.bundle)
    except bp.PublishError as exc:
        print(f"FAIL  host content does not match the approved manifest: {exc}", file=sys.stderr)
        return 1
    return _report(results)


if __name__ == "__main__":
    sys.exit(main())
