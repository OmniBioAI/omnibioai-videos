#!/usr/bin/env python3
"""Record and compare anonymous HTTP behaviour of every cloudflared tunnel hostname.

    python scripts/tunnel_baseline.py record  --config /etc/cloudflared/config.yml --out baseline.json
    python scripts/tunnel_baseline.py compare --config /etc/cloudflared/config.yml --baseline baseline.json \\
        --allow videos.omnibioai.org

Read-only anonymous GETs of ``/`` and ``/health`` per hostname (no credentials, no redirect following);
records status, content type and redirect target. ``compare`` re-probes and reports any hostname whose
behaviour differs from the baseline (except ``--allow``ed ones), so an ingress change can be shown to
have affected only the intended hostname. Origins that are momentarily down show up as differences --
re-run before drawing conclusions.

Developer: Manish Kumar <manish@omnibioai.org>
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

PATHS = ("/", "/health")


def hostnames(config_text: str) -> list[str]:
    return re.findall(r"^\s*-\s*hostname:\s*(\S+)\s*$", config_text, flags=re.MULTILINE)


def normalise_location(location: str) -> str:
    """Keep scheme/host/path only: Cloudflare Access login redirects embed a per-request token in the query."""
    parts = urllib.parse.urlsplit(location)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def probe(host: str, path: str) -> dict:
    conn = http.client.HTTPSConnection(host, timeout=20)
    try:
        conn.request("GET", path, headers={"User-Agent": "omnibioai-tunnel-baseline/1"})
        resp = conn.getresponse()
        resp.read(1024)
        ctype = (resp.getheader("content-type") or "").split(";")[0]
        return {"status": resp.status, "content_type": ctype, "location": normalise_location(resp.getheader("location") or "")}
    except OSError as exc:  # DNS failure, refused, timeout: recorded as a state, not an exception
        return {"status": f"error:{type(exc).__name__}", "content_type": "", "location": ""}
    finally:
        conn.close()


def record(config_text: str) -> dict:
    return {"taken_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "hosts": {h: {p: probe(h, p) for p in PATHS} for h in hostnames(config_text)}}


def compare(baseline: dict, current: dict, allow: set[str]) -> list[str]:
    diffs = []
    for host in sorted(set(baseline["hosts"]) | set(current["hosts"])):
        if host in allow:
            continue
        before, after = baseline["hosts"].get(host), current["hosts"].get(host)
        if before is None or after is None:
            diffs.append(f"{host}: present in only one of baseline/current")
            continue
        for path in PATHS:
            if before[path] != after[path]:
                diffs.append(f"{host}{path}: {before[path]} -> {after[path]}")
    return diffs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--config", type=Path, required=True)
    rec.add_argument("--out", type=Path, required=True)
    cmp_ = sub.add_parser("compare")
    cmp_.add_argument("--config", type=Path, required=True)
    cmp_.add_argument("--baseline", type=Path, required=True)
    cmp_.add_argument("--allow", action="append", default=[])
    args = parser.parse_args(argv)

    current = record(args.config.read_text(encoding="utf-8"))
    if args.command == "record":
        args.out.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        for host, paths in current["hosts"].items():
            print(f"{host:<28}" + "  ".join(f"{p} {v['status']} {v['content_type']}" for p, v in paths.items()))
        print(f"\nbaseline of {len(current['hosts'])} hostnames written to {args.out}")
        return 0
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    diffs = compare(baseline, current, set(args.allow))
    print("\n".join(diffs) if diffs else f"no differences across {len(current['hosts'])} hostnames (allowed: {args.allow or 'none'})")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
