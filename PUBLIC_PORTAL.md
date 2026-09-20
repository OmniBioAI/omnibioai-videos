# Public video portal (videos.omnibioai.org)

A separate, read-only nginx service that publishes **only** videos explicitly approved in
`content/videos.json`. It is independent of the internal `videos` service (port 8086, used by
Studio/Workbench), which is unchanged.

```
browser -> Cloudflare -> cloudflared -> 127.0.0.1:8087 -> omnibioai-videos-public (hardened nginx)
                                                           |- portal HTML/CSS/JS          (in the image)
                                                           |- approved hashes + allowlist (in the image)
                                                           `- /content  <- host content dir, READ-ONLY mount
```

**The video files stay on the host** (`~/Desktop/machine/omnibioai-videos/content`). The image contains
nginx, the portal, and generated publication metadata -- no video, no `content/`, no `guide.html`.

## Publication control plane: `content/videos.json`

Every entry has `visibility`, exactly one of `PUBLIC`, `INTERNAL`, `REVIEW_REQUIRED`.

| Value | Catalog | `/videos/<file>` |
|-------|---------|------------------|
| `PUBLIC` + approved hash matches | listed | streamable (GET/HEAD/Range/206) |
| `PUBLIC` but file changed / missing / symlink / bad container | not listed | 404 |
| `REVIEW_REQUIRED`, `INTERNAL` | not listed | 404 |
| missing / misspelled / `public` / `null` / any other value | not listed | 404 |
| file in the content dir but not in the manifest (`my_video.mov`, `guide.html`, ...) | not listed | 404 |

The visibility decision controls **both** catalog discovery and direct media access: hiding an item
while leaving its URL reachable is not possible in this design, because the only way a URL exists is
the generated exact-match `location = /videos/<file>` for an approved, hash-verified file.

A `PUBLIC` entry must carry `approved_sha256` and `approved_size_bytes` of the exact bytes that were
approved. Approval is bound to content, not to a filename.

### How a file becomes public

1. Review the whole video at full resolution *and its audio* (on-screen paths, hostnames/IPs, keys,
   admin/personal information, patient/user data) and inspect container metadata (`ffprobe`).
2. Record the reviewed file's identity: `sha256sum content/<file>` and `stat -c %s content/<file>`.
3. Set `visibility` to `PUBLIC` and add `approved_sha256` / `approved_size_bytes` in a reviewed PR.
4. Rebuild and redeploy (below). To withdraw a video, set it back and redeploy.

Recording the hash records **what** was approved; it does not by itself record **that** a human reviewed it.

## Generated artifacts (`scripts/build_public.py`)

For the manifest and the host media directory it verifies every `PUBLIC` file (regular file, not a
symlink, non-empty, approved size, approved SHA-256, structurally valid MP4/WebM) and aborts on any
problem; it copies no media. It emits `dist/public/`:

```
www/               index.html portal.css portal.js         -> web root
publication/       approved.tsv                            -> id, sha256, size, filename
                   entries/<id>.json                       -> the public catalog record (allowlisted fields only)
                   entries/<id>.conf                       -> location = /videos/<file> { alias /content/<file>; ... }
```

## Runtime verification (`runtime/publish-runtime.sh`, the container entrypoint)

Mounting the directory does not publish it. At start-up and continuously (default every 5 s, plus a full
re-hash every 10 min) the publisher checks each approved file on the mount against its approved size and
SHA-256, and only then generates `media.conf` (nginx locations) and `videos.json` (catalog) from the
verified set and reloads nginx. A file that is replaced, modified, truncated, deleted or turned into a
symlink is **delisted and 404s**; if the approved bytes return it is published again. Access is removed
before the listing and granted before the listing, so the catalog never lists a URL that would 404.

Editing the host `videos.json` or dropping files into the mounted directory publishes nothing: the image
holds the reviewed publication snapshot, so a visibility change needs a rebuild.

Residual window: a replacement is detected within the poll interval (seconds); a same-size, same-mtime,
different-bytes in-place swap only at the next full re-hash (default 10 min).

## Deploy

```bash
cd ~/Desktop/machine/omnibioai-videos
SHA=<approved commit>
HOSTCONTENT=$HOME/Desktop/machine/omnibioai-videos/content
BUILD=$(mktemp -d)
GIT_LFS_SKIP_SMUDGE=1 git archive "$SHA" | tar -x -C "$BUILD"    # code + reviewed manifest; no media bytes
cd "$BUILD"

python3 scripts/build_public.py --content content --media-dir "$HOSTCONTENT" --out dist/public
docker build -f Dockerfile.public -t omnibioai-videos-public:"${SHA:0:12}" -t omnibioai-videos-public:local .
VIDEOS_CONTENT_DIR="$HOSTCONTENT" docker compose -p omnibioai-public-videos \
  -f "$BUILD/deploy/docker-compose.public.yml" up -d

python3 scripts/verify_public_deploy.py container omnibioai-videos-public --content-dir "$HOSTCONTENT" --bundle dist/public
python3 scripts/verify_public_deploy.py site http://127.0.0.1:8087 --manifest content/videos.json --media-dir "$HOSTCONTENT"
```

`git archive` is used with smudging disabled on purpose: the build takes its media **only** from the host
directory, never from a Git LFS object at some commit, so the bytes verified are the bytes served.
(`*.mp4` is LFS-tracked in this repo, `.mov`/`.webm` are not; neither matters to the build.)

## Point the tunnel at it

`scripts/retarget_ingress.py` writes a *proposed* config and prints the diff; it never edits the
original and refuses unless exactly one `videos.omnibioai.org` entry currently targets the expected port.
Record `scripts/tunnel_baseline.py record` first and `compare ... --allow videos.omnibioai.org` after.
Cloudflare documents no reload mechanism for a locally-managed tunnel's `config.yml`; it recommends
running a *replica* with the new config, waiting until it is up, then stopping the old instance
(https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/configuration-file/).
On this host `systemctl show cloudflared` reports `CanReload=no` (the unit has no `ExecReload`) and every
earlier config change in the journal was applied by a stop/start. The straightforward way to apply the
change is therefore `systemctl restart cloudflared` (a reconnect of all hostnames, seconds); the replica
procedure avoids that but needs a second unit with the same tunnel credentials.

## Rollback

Restore the backed-up `config.yml` and `systemctl restart cloudflared` (this restores the legacy 8086
exposure, including the public `my_video.mov` and `guide.html`), then optionally
`docker rm -f omnibioai-videos-public`.

## Tests

`python -m pytest tests` -- `tests/test_public_portal.py` covers the visibility model, the hash and
container gates, the exact-match allowlist, no packaged media, the hardened container, the runtime
delisting scenarios (replace / truncate / same-size swap / symlink / delete), ingress isolation and
browser rendering, using synthetic sentinel secrets.
