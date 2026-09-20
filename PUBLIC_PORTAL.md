# Public video portal (videos.omnibioai.org)

A separate, read-only nginx image that serves **only** content explicitly classified `PUBLIC`.
It is independent of the internal `videos` service (port 8086, used by Studio/Workbench), which is
unchanged.

```
browser -> Cloudflare -> cloudflared -> 127.0.0.1:8087 -> omnibioai-videos-public (nginx, static)
                                                          |- index.html / portal.css / portal.js
                                                          |- videos.json   (generated, PUBLIC entries only)
                                                          `- videos/<PUBLIC files only>
```

No backend, no API calls, no cookies, no credentials, no volumes. The container never sees
`content/`; it only contains what `scripts/build_public.py` emitted.

## Classification

Every entry in `content/videos.json` carries `visibility`, exactly one of:

| Value | Meaning |
|-------|---------|
| `PUBLIC` | Reviewed and approved for anonymous viewing. The only value that is ever published. |
| `INTERNAL` | For OmniBioAI users/staff only. Never published. |
| `REVIEW_REQUIRED` | Not yet cleared. Never published. |

**Fail closed:** missing, misspelled, wrongly-cased (`public`), padded (`"PUBLIC "`), non-string or
`null` visibility is treated as not public. Media files that are not in the manifest at all are not
public either. A malformed `PUBLIC` entry (bad filename, missing/empty/symlinked file, unknown tag,
secret- or path-like text) **aborts** the build rather than being skipped.

Only `filename`, `title`, `desc`, `tag`, `order` reach the public catalog; any other manifest field
is dropped. `guide.html` and the internal `index.html` are never copied.

### To publish a video

1. Review the whole video at full resolution (on-screen paths, hostnames/IPs, keys, patient/user data,
   audio) and its container metadata (`ffprobe -show_format`).
2. Set its `visibility` to `PUBLIC` **and** add `"sha256": "<sha256sum of the reviewed file>"` in
   `content/videos.json`, in a reviewed PR. Approval is pinned to those exact bytes: if the file is
   replaced afterwards the build aborts until it is re-reviewed and the hash updated.
3. Rebuild and redeploy (below). To withdraw a video, set it back and redeploy.

## Build and run

Build from a pristine export of the approved commit, never from a working tree with local edits:

```bash
cd ~/Desktop/machine/omnibioai-videos
SHA=<approved commit>
BUILD=$(mktemp -d) && git archive "$SHA" | tar -x -C "$BUILD" && cd "$BUILD"

python3 scripts/build_public.py --out dist/public      # prints the visibility inventory; aborts on any problem
docker build -f Dockerfile.public -t omnibioai-videos-public:"$SHA" -t omnibioai-videos-public:local .
docker compose -p omnibioai-public-videos \
  -f ~/Desktop/machine/omnibioai-videos/deploy/docker-compose.public.yml up -d

curl -s http://127.0.0.1:8087/videos.json              # expect [] until something is PUBLIC
```

`git archive` exports Git-LFS media as pointer files. That is fine while nothing is `PUBLIC`; once a
video is approved, export with a real checkout (`git worktree add --detach "$BUILD" "$SHA"`) so the
bytes exist and can be hash-verified.

## Point the tunnel at it

`scripts/retarget_ingress.py` writes a *proposed* config and prints the diff; it never edits the
original and refuses unless exactly one `videos.omnibioai.org` entry currently targets the expected port.

```bash
python scripts/retarget_ingress.py --config /etc/cloudflared/config.yml \
  --hostname videos.omnibioai.org --from-port 8086 --to-port 8087 --out /tmp/cloudflared.proposed.yml
# review the diff: exactly one line changes
sudo cp /etc/cloudflared/config.yml /etc/cloudflared/config.yml.pre-videos-portal-$(date +%Y%m%d).bak
sudo cp /tmp/cloudflared.proposed.yml /etc/cloudflared/config.yml
sudo cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
sudo systemctl restart cloudflared     # brief reconnect for ALL tunnel hostnames
```

## Rollback

```bash
sudo cp /etc/cloudflared/config.yml.pre-videos-portal-<date>.bak /etc/cloudflared/config.yml
sudo systemctl restart cloudflared
docker compose -p omnibioai-public-videos -f deploy/docker-compose.public.yml down   # optional
```

(Rolling back restores the previous public exposure of the legacy 8086 service.)

## Tests

`python -m pytest tests` -- includes `tests/test_public_portal.py` (visibility model, bundle scan,
nginx/Docker/compose contract, ingress isolation, and Docker-backed anonymous-client tests with
synthetic sentinel secrets).
