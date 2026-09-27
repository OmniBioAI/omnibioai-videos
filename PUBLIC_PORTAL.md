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

## Searchable Video Tutorials library

`portal/index.html`, `portal/portal.js`, and `portal/portal.css` implement the public library,
independently of the internal player. The browser fetches `/videos.json` once, without credentials,
then searches and filters that eligible snapshot locally. It never fetches the source manifest.
Counts, sections, category chips, and autocomplete all use the same authorized records.
`REVIEW_REQUIRED`, `INTERNAL`, unclassified, and hash-withheld entries contribute no filenames,
titles, descriptions, categories, or discovery terms. Runtime delisting and media allowlisting
are unchanged; reload the page to refresh an already-open browser's catalog snapshot.

Search matches title, `desc`, category, legacy `tag`, and the discovery lists below. Matching is
case-insensitive and ignores accents, punctuation, and spacing variants such as `RNA-seq`,
`rnaseq`, and `RNA seq`. Multiple query words must all match. Search and category filters combine;
clearing search keeps the selected category, and **All** resets the category.

Autocomplete shows up to eight distinct video, keyword, and category suggestions from eligible
metadata within the selected category. Use ↑/↓ and Enter or click a suggestion. Video suggestions
open the player; keywords refine the search; category suggestions select that category and clear
the query. Escape, Tab, or clicking outside closes the list. The combobox/listbox announces its
selection, result counts are announced politely, and cards/chips support keyboard access.

`?q=rnaseq&category=workflows` bookmarks a filtered view; category IDs use lowercase labels with
punctuation/spacing replaced by hyphens (for example `AI & RAG` → `ai-rag`). The browser updates
the URL without navigation or a network search. The `← Back to Studio` link targets the Studio
origin configured at public-build time, followed by `/studio`. The default is
`https://webstudio.omnibioai.org/studio`; Studio origin and video portal are separate public
hosts, so the portal does not proxy `/studio` through its restricted nginx service.

Set the non-secret `STUDIO_URL` environment variable, or pass `--studio-url`, to select another
Studio origin when building for a deployment or local environment. The value must be a bare HTTPS
origin without credentials, path, query, or fragment. HTTP is accepted only for `localhost` or
`127.0.0.1` development servers. If unset, the production Studio origin above is used. The value
is embedded only into the built portal's Back to Studio link; no runtime endpoint or credential is
added. For example, a local Studio server can use `STUDIO_URL=http://localhost:5173`.

### Optional discovery metadata

The existing required fields remain `filename`, `title`, `desc`, `tag`, and integer `order`, plus
the existing publication approval fields for a `PUBLIC` entry. The existing six `tag` values
remain valid. No new field is required on legacy entries.

| Field | Type | Behavior |
|-------|------|----------|
| `category` | Nonempty string, at most 80 characters | Main category; defaults to the legacy tag label. `All` is reserved. |
| `tags` | Array of strings | Topic terms and aliases, such as `rna-seq`, `transcriptomics`. Separate from the legacy singular `tag`. |
| `keywords` | Array of strings | Additional discovery phrases. |
| `services`, `modules`, `workflows` | Arrays of strings | Service/module/workflow names to match and suggest. |
| `featured` | Boolean | `true` moves a video into the top Featured section in the unfiltered library. Defaults to false. |
| `duration` | Positive finite seconds or a display string | Duration badge, e.g. `754` or `12:34`; omitted when unavailable. |
| `thumbnail` | Existing validated local image path | Optional image preview; the current bundle does not generate or publish additional image files. |

Example discovery fields to add to an existing approved record:

```json
{
  "category": "Workflows",
  "tags": ["rna-seq", "transcriptomics"],
  "keywords": ["differential expression"],
  "services": ["Nextflow"],
  "modules": ["Workflow Runner"],
  "workflows": ["nf-core RNA-seq"],
  "featured": false,
  "duration": "12:34"
}
```

Optional malformed fields are omitted; lists retain up to 32 distinct, nonempty strings of at
most 120 characters each. Every exported field is scanned for forbidden content, including the
new discovery fields. Unknown/internal fields remain excluded by the public projection. Metadata
on an approved record is public-facing copy and must be reviewed before rebuilding the image.

Chips and sections appear only for populated categories. Preferred order is Getting Started,
Platform, Workflows, AI & RAG, Bioinformatics, Security, Administration, Documentation, followed
by legacy tag categories and then other category names alphabetically. Videos retain ascending
`order`, then filename as the tie-breaker. Featured videos appear once in the unfiltered library
and still participate in category/search results. No dates or “recently added” status are inferred.

The grid supports five columns on wide desktops, four on normal desktops, two to three on
tablets, and one on narrow phones. Chips scroll horizontally. Cards retain 16:9 thumbnails,
play controls, optional duration badges, and clamped titles/descriptions. Missing images use the
existing branded fallback; absent image metadata uses a video-frame preview loaded only near
the viewport. Search indexes are computed once and card DOM is reused while filtering, avoiding
recreated media previews for every keystroke. No backend search or virtualization is required.

### Studio build (`--variant studio`, `videos:8086`)

Studio's Video Tutorials page (`/_svc/videos/`, proxied to `videos:8086` without authentication)
embeds a variant of this same portal. `build_studio()` reuses `select_public()` unchanged, so its
catalog is the public projection of the verified PUBLIC entries (minus `thumbnail`, since the image
serves no image files). Exact, count-checked rewrites make the asset, catalog and media URLs
document-relative and drop the portal's own Back to Studio link; a portal change that breaks a
rewrite marker fails the build. The approved media is copied into `dist/studio/media/`, re-hashed,
and baked into `Dockerfile.studio`; `nginx.studio.conf` has one exact-match location per approved
file (plus its legacy root URL), no generic alias, no SPA fallback, no CORS, and allows framing only
by the same origin. Unlike the public service there is no runtime re-verification: the image holds
the verified bytes. Tests: `tests/test_studio_library.py`.

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

STUDIO_URL=https://webstudio.omnibioai.org python3 scripts/build_public.py --content content --media-dir "$HOSTCONTENT" --out dist/public
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

`tests/test_video_library.py` adds browser coverage for metadata search/normalization, combined
filters, autocomplete keyboard/click/dismissal behavior, URL state, accessibility, empty/malformed
metadata, public-only suggestions, and a 100-video catalog from 320px to 1920px. It also verifies
the optional metadata projection and secret scanning. Run the full suite with its configured
98% coverage gate using `python -m pytest tests`; Docker fixtures build isolated test images and
exercise publication gates without changing the production service. Generated `dist/` is ignored;
build into a temporary output directory when reviewing changes locally.
