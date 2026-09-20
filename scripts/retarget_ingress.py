#!/usr/bin/env python3
"""Propose a cloudflared ingress change that touches ONLY one hostname's upstream.

It never edits a file in place: it reads the current config, writes the proposed
config to ``--out`` (or stdout) and prints a unified diff, so the operator can
review and apply it with sudo deliberately.

    python scripts/retarget_ingress.py --config /etc/cloudflared/config.yml \\
        --hostname videos.omnibioai.org --from-port 8086 --to-port 8087 --out /tmp/config.proposed.yml

Refuses (exit 1) unless exactly one ``hostname:`` entry matches and its service
line currently points at ``--from-port``; nothing else in the file may differ.

Developer: Manish Kumar <manish@omnibioai.org>
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path


class IngressError(Exception):
    """Raised when the requested retarget is not an unambiguous single-line change."""


def retarget(text: str, hostname: str, from_port: int, to_port: int) -> str:
    """Return ``text`` with the ``service:`` line following ``hostname`` re-pointed."""
    lines = text.splitlines(keepends=True)
    host_re = re.compile(rf"^\s*-\s*hostname:\s*{re.escape(hostname)}\s*$")
    matches = [i for i, line in enumerate(lines) if host_re.match(line)]
    if len(matches) != 1:
        raise IngressError(f"expected exactly one ingress entry for {hostname}, found {len(matches)}")

    idx = matches[0] + 1
    if idx >= len(lines):
        raise IngressError(f"{hostname}: entry has no service line")
    service_re = re.compile(rf"^(\s*service:\s*http://[^\s:]+:){from_port}(\s*)$")
    match = service_re.match(lines[idx].rstrip("\r\n"))
    if not match:
        raise IngressError(
            f"{hostname}: service line is not 'http://<host>:{from_port}': {lines[idx].strip()!r}"
        )
    newline = lines[idx][len(lines[idx].rstrip("\r\n")):]
    lines[idx] = f"{match.group(1)}{to_port}{match.group(2)}{newline}"
    return "".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--from-port", type=int, required=True)
    parser.add_argument("--to-port", type=int, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    original = args.config.read_text(encoding="utf-8")
    try:
        proposed = retarget(original, args.hostname, args.from_port, args.to_port)
    except IngressError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        proposed.splitlines(keepends=True),
        fromfile=str(args.config),
        tofile="proposed",
    )
    sys.stdout.writelines(diff)
    if args.out:
        args.out.write_text(proposed, encoding="utf-8")
        print(f"\nProposed config written to {args.out} (original untouched)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
