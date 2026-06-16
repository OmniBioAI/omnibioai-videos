# omnibioai-videos

Video library and Getting Started guide service for the OmniBioAI Studio platform.

Serves tutorial videos, workflow demonstrations, and the interactive Getting Started guide — all via a single nginx container with zero backend dependencies.

---

## Overview

| Item | Value |
|------|-------|
| Image | `ghcr.io/man4ish/omnibioai-videos:latest` |
| Port | `8086` |
| Base image | `nginx:alpine` |
| Dependencies | None |
| Auth | None (internal network only) |

### Endpoints

| Path | Description |
|------|-------------|
| `http://<host>:8086/` | Video library player (`index.html`) |
| `http://<host>:8086/guide.html` | Interactive Getting Started guide |
| `http://<host>:8086/videos.json` | Video manifest (titles, tags, ordering) |
| `http://<host>:8086/<filename>` | Individual video files (.mp4 / .webm / .mov) |

---

## Directory Structure

```
omnibioai-videos/
├── content/
│   ├── index.html        # Video library player — dark theme, manifest-driven
│   ├── guide.html        # Interactive Getting Started guide (8 sections)
│   ├── videos.json       # Video manifest — titles, descriptions, tags, ordering
│   └── *.mp4 / *.mov     # Video files (added by user, not tracked in git)
├── nginx.conf            # nginx server configuration
├── Dockerfile            # Container build definition
└── README.md             # This file
```

---

## videos.json — Video Manifest

All videos are configured via `videos.json`. Edit this file to add, rename, reorder or retag videos — **no rebuild required**, just refresh the browser.

### Format

```json
[
  {
    "filename": "intro_getting_started.mp4",
    "title": "Getting Started with OmniBioAI",
    "desc": "Complete walkthrough of the platform — setup, configuration and first run.",
    "tag": "intro",
    "order": 1
  },
  {
    "filename": "tutorial_rnaseq_workflow.mp4",
    "title": "RNA-Seq Analysis End-to-End",
    "desc": "From raw FASTQ files to differential expression results using the Workflow Runner.",
    "tag": "tutorial",
    "order": 2
  }
]
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| `filename` | ✅ | Video filename in the `content/` directory |
| `title` | ✅ | Display title shown in the player UI |
| `desc` | ✅ | Short description (1–2 sentences) |
| `tag` | ✅ | Category tag — see table below |
| `order` | ✅ | Sort position (lower number = appears first) |

### Available Tags

| Tag | Color | Use for |
|-----|-------|---------|
| `intro` | Green | Platform overview and onboarding videos |
| `tutorial` | Blue | Step-by-step feature walkthroughs |
| `workflow` | Orange | End-to-end bioinformatics pipelines |
| `demo` | Purple | Live demonstrations in research contexts |
| `hpc` | Amber | HPC cluster setup and Slurm job submission |

---

## Adding Videos

### Step 1 — Copy the video file

```bash
cp my_analysis_demo.mp4 \
   /home/manish/Desktop/machine/omnibioai-videos/content/
```

Supported formats: `.mp4` (recommended), `.webm`, `.mov`

> **Note on `.mov` files:** `.mov` recorded on Mac/iPhone works in Chrome and Safari
> but may not play in Firefox. Convert to `.mp4` for full cross-browser support:
>
> ```bash
> ffmpeg -i my_video.mov -c:v libx264 -c:a aac my_video.mp4
> ```

### Step 2 — Add entry to videos.json

```bash
nano /home/manish/Desktop/machine/omnibioai-videos/content/videos.json
```

Add a new JSON object to the array:

```json
{
  "filename": "my_analysis_demo.mp4",
  "title": "Single-Cell RNA-Seq with Scanpy",
  "desc": "Clustering, UMAP visualization and marker identification on 10X Genomics data.",
  "tag": "demo",
  "order": 7
}
```

### Step 3 — Refresh the browser

No rebuild needed. The player fetches `videos.json` on every page load.

---

## Getting Started Guide (`guide.html`)

`guide.html` is a self-contained interactive guide covering all aspects of OmniBioAI Studio setup. It is accessible at `http://<host>:8086/guide.html` and linked from the Workbench **Platform Services** section as the **Getting Started** tile.

### Sections

| Section | Content |
|---------|---------|
| Overview | Platform modes (Local / Cloud / HPC / Hybrid), 4-step quick start |
| Local Setup | Docker Desktop config, AppImage / DMG installation, data directories |
| Cloud | AWS Batch, Azure Batch, GCP Batch setup with UI screenshots |
| HPC | SSH key setup, Slurm / PBS / LSF config, shared filesystem requirements |
| LLM / API Keys | Ollama local models, Claude API, OpenAI key setup |
| Workbench Tour | All 15 platform services, module categories, expected service health |
| First Workflow | Step-by-step RNA-Seq job submission via TES |
| FAQ | Common issues — Docker, SSH, AWS permissions, Ollama model errors |

### Updating the Guide

`guide.html` is a standalone file — edit it directly, no build step needed:

```bash
nano /home/manish/Desktop/machine/omnibioai-videos/content/guide.html
```

To redeploy after editing:

```bash
cd /home/manish/Desktop/machine/omnibioai-videos
docker build -t ghcr.io/man4ish/omnibioai-videos:latest .
docker push ghcr.io/man4ish/omnibioai-videos:latest

cd /home/manish/Desktop/machine/omnibioai-studio
docker compose up -d --force-recreate videos
```

---

## nginx Configuration

```nginx
server {
    listen 8086;
    root /usr/share/nginx/html;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location ~* \.(mp4|webm|mov)$ {
        add_header Accept-Ranges bytes;
        add_header Cache-Control "public, max-age=86400";
    }

    location /videos.json {
        add_header Cache-Control "no-cache";
        add_header Access-Control-Allow-Origin "*";
    }
}
```

Key behaviours:
- Video files served with `Accept-Ranges: bytes` — enables seeking and scrubbing in the browser player
- `videos.json` served with `no-cache` — content changes appear immediately on refresh
- All other routes fall back to `index.html` (SPA-style routing)

---

## Docker Build

### Build locally

```bash
cd /home/manish/Desktop/machine/omnibioai-videos
docker build -t ghcr.io/man4ish/omnibioai-videos:latest .
```

### Push to registry

```bash
docker push ghcr.io/man4ish/omnibioai-videos:latest
```

### Dockerfile

```dockerfile
FROM nginx:alpine

COPY content/ /usr/share/nginx/html/
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8086
```

---

## Docker Compose Integration

Defined in `omnibioai-studio/docker-compose.yml`:

```yaml
videos:
  image: ghcr.io/man4ish/omnibioai-videos:latest
  ports:
    - "${HOST_IP:-0.0.0.0}:8086:8086"
  restart: unless-stopped
```

Start with the full stack:

```bash
cd /home/manish/Desktop/machine/omnibioai-studio
docker compose up -d
```

Restart after content update:

```bash
docker compose up -d --force-recreate videos
```

---

## Video Player Features

`index.html` implements a full-featured dark-theme video library:

| Feature | Description |
|---------|-------------|
| Manifest-driven | Reads `videos.json` for titles, descriptions, order. Falls back to nginx autoindex if no manifest found. |
| Tag filtering | Filter bar: All / Intro / Tutorial / Workflow / Demo / HPC |
| Full-text search | Searches across video titles and descriptions in real time |
| Modal player | Click any card to open a full-width video modal with autoplay |
| Keyboard shortcut | `Esc` closes the modal |
| Thumbnail preview | Video frames at t=2s load as card thumbnails automatically |
| Order badges | Cards show `#1`, `#2`... from the `order` field in videos.json |
| Source badge | `FROM MANIFEST` (green) or `AUTO-DETECTED` (amber) shown in header |
| Count display | Live video count updates as filters are applied |

---

## Planned Content

| Video | Tag | Status |
|-------|-----|--------|
| Getting Started with OmniBioAI | intro | 🔲 To record |
| RNA-Seq Analysis End-to-End | tutorial | 🔲 To record |
| Single-Cell RNA-Seq with Scanpy | demo | 🔲 To record |
| Variant Calling Pipeline (WGS) | workflow | 🔲 To record |
| Running Jobs on HPC with Slurm | hpc | 🔲 To record |
| Cloud Setup — AWS Batch | tutorial | 🔲 To record |
| Using the Dev Hub RAG Search | demo | 🔲 To record |
| Registering and Running Workflows | tutorial | 🔲 To record |

---

## Recording Guidelines

To maintain consistency across videos:

- **Resolution:** 1080p or higher (1920×1080 minimum)
- **Format:** Export as `.mp4`, H.264 video + AAC audio
- **Duration:** 3–10 minutes per video (shorter is better)
- **Naming convention:** `{tag}_{topic}_{version}.mp4` e.g. `tutorial_rnaseq_v1.mp4`
- **Screen recording:** Use OBS or QuickTime. Show the full browser window.
- **Audio:** Use a microphone. Clear narration is essential.
- **Intro:** Start with a 3-second title card showing the video title and OmniBioAI logo.

---

## Troubleshooting

### Video not appearing in the player
- Check the filename in `videos.json` matches exactly (case-sensitive)
- Verify the file exists: `ls content/*.mp4`
- Hard-refresh the browser: `Cmd+Shift+R` / `Ctrl+Shift+R`

### Video plays but can't seek/scrub
- The nginx config must include `Add-Header Accept-Ranges bytes` for video files
- Verify `nginx.conf` has the video location block

### guide.html not loading
- Check the file is in `content/`: `ls content/guide.html`
- Verify the container was rebuilt after adding the file

### Container not starting
```bash
docker compose logs videos --tail=20
```

---

## License

Apache 2.0 — see root `LICENSE` file.

---

*Part of the [OmniBioAI](https://github.com/man4ish/omnibioai-studio) platform.*