# Aether Studio — AI Anime and Short-Video Workbench

Aether Studio is an AI anime and short-video editing project. This repository
contains the M0-0 engineering foundation: a runnable web workbench, API,
isolated worker, shared contracts, container topology, tests, and CI gates.

The repository is independent and does not read, import, or modify `Ai-Eos`.

## Architecture

| Area | Purpose |
| --- | --- |
| `apps/web` | React, TypeScript, and Vite three-panel editing workbench |
| `apps/api` | FastAPI project CRUD, uploads, same-origin Sidecar adapters, real render jobs, SQLite WAL, optimistic locking, and SSE |
| `apps/worker` | Isolated Python Worker with real FFmpeg operations, Sidecar clients, AI provider boundary, and recovery adapter |
| `apps/video_use` | Internal non-root service for the pinned video-use render, timeline-view, and transcription helpers |
| `packages/contracts` | Canonical Timeline v1.1 DTOs, validation, error codes, and RationalTime |
| `packages/editor` | Editor-agnostic material, canvas, and timeline adapter interfaces |
| `infra/docker` | API, Worker, and same-origin Nginx Web deployment |
| `e2e` | Playwright workbench and render-progress flow |

### Canonical time

`RationalTime` stores `value / timescale` as safe integers. Exact operations use
BigInt cross-products plus gcd/lcm reduction; comparisons do not convert to
floating-point seconds. `toSeconds()` and `toMilliseconds()` are display
conveniences, not exact comparison primitives. An operation fails explicitly
if its reduced result cannot fit JavaScript's safe-integer range.

The tests cover 24 fps, 24000/1001, 30000/1001, 60000/1001, long timelines,
safe-integer boundaries, exact comparisons, and arithmetic overflow.

### SQLite WAL and optimistic locking

Each SQLite connection applies:

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=30000;
```

Project writes use one atomic statement whose condition includes both project
ID and `expectedRevision`. The affected-row count distinguishes a successful
write from a stale revision, preventing two competing updates from both
succeeding.

### Same-origin Web routing

The browser calls `/api`. In development, Vite proxies that path to FastAPI. In
Docker, Nginx proxies `/api/` to the API container and disables buffering for
SSE. Vite build-time variables are not incorrectly supplied as container
runtime variables.

## Requirements

- Node.js 24 LTS
- pnpm 10.30.3
- Python 3.12
- Docker with Compose v2 for container verification

## Local development

Install Node dependencies:

```bash
corepack enable
corepack prepare pnpm@10.30.3 --activate
pnpm install --frozen-lockfile
```

Create one Python environment and install all service requirements:

```bash
python3 -m venv .venv
.venv/bin/pip install -r apps/api/requirements.txt
.venv/bin/pip install -r apps/worker/requirements.txt
.venv/bin/pip install -r apps/video_use/requirements.txt
```

Run the API:

```bash
PYTHONPATH=apps/api DATABASE_URL=sqlite:///aether.db \
  .venv/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8000
```

Run the Worker:

```bash
PYTHONPATH=apps/worker BACKEND_URL=http://127.0.0.1:8000 \
  WORKER_PORT=8001 .venv/bin/python -m app.main
```

The production Compose stack builds `browser-use/video-use` at the pinned
commit recorded below. Local direct Sidecar development additionally requires
an explicit checkout path:

```bash
PYTHONPATH=apps/video_use \
  VIDEO_USE_UPSTREAM_ROOT=/absolute/path/to/video-use \
  VIDEO_USE_MEDIA_ROOT=./.local/video-use-media \
  .venv/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8002
```

Run the Web workbench:

```bash
pnpm --filter @aether/web dev
```

Open `http://127.0.0.1:5173`.

## Verification

Run the deterministic workspace checks:

```bash
pnpm install --frozen-lockfile
pnpm lint
pnpm build
pnpm test
```

Run Python tests:

```bash
PYTHONPATH=apps/api .venv/bin/python -m pytest apps/api/test_main.py -q
PYTHONPATH=apps/worker .venv/bin/python -m pytest apps/worker/test_worker.py -q
```

Run Playwright:

```bash
pnpm exec playwright install chromium
AETHER_PYTHON=.venv/bin/python pnpm e2e
```

Run the complete container stack:

```bash
docker compose -f infra/docker/docker-compose.yml config --quiet
docker compose -f infra/docker/docker-compose.yml up -d --build --wait
curl --fail http://127.0.0.1/api/health
docker compose -f infra/docker/docker-compose.yml exec -T worker ffmpeg -version
docker compose -f infra/docker/docker-compose.yml exec -T worker ffprobe -version
docker compose -f infra/docker/docker-compose.yml down
```

The final command intentionally omits `-v`/`--volumes` so local and production
SQLite data in the `sqlite-db` named volume is preserved. The only permitted
`--volumes` teardown is the GitHub Actions Docker job: that job assigns a
unique `COMPOSE_PROJECT_NAME` for each workflow run and removes only its own
disposable, run-scoped volumes.

For a production-shaped launch, copy `infra/docker/.env.example`, keep its
Compose project name stable, start with `--wait`, and run
`infra/docker/production-smoke.sh`. The target-specific TLS, backup, provider,
and acceptance gates are documented in `docs/PRODUCTION_DEPLOYMENT.md`.

The GitHub Actions workflow has three required jobs:

1. Lint, build, JavaScript tests, API tests, and Worker tests.
2. Playwright browser flow with screenshots and HTML report artifacts.
3. Docker Compose build, health checks, same-origin proxy check, FFmpeg checks,
   and log artifact.

See `docs/evidence/M0-0-VERIFICATION.md` for the evidence policy and current
limitations. A green job proves only the scope asserted by that job.

## Implemented behavior

- Project list, create, query, and update.
- Atomic revision conflict response (`409 CONCURRENCY_CONFLICT`).
- Three-panel workbench and bottom timeline.
- Real media upload/probe and basic track/clip placement.
- Real FFmpeg proxy, audio extraction, and metadata probing.
- Pinned video-use EDL rendering with live `task_progress` SSE events and MP4 download.
- Independent Worker HTTP health endpoint.
- Explicit remaining mock boundaries for AI provider and recovery.
- Reproducible lockfile, Node/pnpm baseline, Docker topology, and CI.

## M0-0 limitations

- AI generation and subtitle methods are mocks and use no provider keys.
- Task state is in process memory; Redis/Celery durability is deferred.
- The preview canvas is a workbench placeholder, not a decoding or compositing
  engine.
- Offline persistence and multi-device merge UI are deferred.
- RationalTime is exact only while reduced results remain within the declared
  JavaScript safe-integer boundary.

These limitations must not be presented as completed production capabilities.

## M2-0 video-use and real media

- **Repository**: [browser-use/video-use](https://github.com/browser-use/video-use)
- **Version**: `0.1.0`
- **Pinned commit**: `92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66`
- **License**: MIT

The Docker image verifies the full upstream SHA before installing it. The
Sidecar is internal-only, runs as a non-root user, stores media in a dedicated
named volume, and exposes only validated media/job contracts. An
`ELEVENLABS_API_KEY` enables the real Scribe transcription path; without it the
capability is reported as unconfigured and no transcript is fabricated.

See `docs/evidence/M2-0-VERIFICATION.md` for candidate verification and the
CI evidence still required before acceptance.

## M3-0 OpenCut compatibility core

- **Official rewrite repository**: [opencut-app/opencut](https://github.com/opencut-app/opencut)
- **Audited rewrite commit**: `400f097becba5db0fbc305d5a65348cb81c20356`
- **Compatibility source**: [opencut-app/opencut-classic](https://github.com/opencut-app/opencut-classic)
- **Audited Classic commit**: `cf5e79e919144200294fb9fed22a222592a0aeea`
- **Pinned package**: `opencut-wasm@0.2.10`
- **License**: MIT

Aether uses OpenCut's published Rust/WASM timing core for deterministic media
ticks, frame alignment, and Classic v31 compatibility snapshots. The WebAssembly
chunk is loaded only when the user exports a snapshot, so it does not increase
the initial workbench JavaScript payload. The snapshot contains the translated
scene, tracks, clips, and a media manifest while Aether's Canonical Timeline and
server-side video-use render remain the source of truth.

The Classic application is archived and the official rewrite has not released
its Editor API yet. Aether therefore does not embed or depend on the archived
runtime. See `docs/evidence/M3-0-VERIFICATION.md` for the exact boundary.

## M4-0 OpenReel fallback compatibility

- **Repository**: [Augani/openreel-video](https://github.com/Augani/openreel-video)
- **Version**: `0.1.1` beta
- **Audited commit**: `8459024d4c82ee16a2e14537553884a623ae9c4e`
- **Project schema**: `1.0.0`
- **License**: MIT

Aether exports a real OpenReel project file containing settings, media
placeholders, tracks, clips, trims, transforms, and the authoritative timeline
duration. OpenReel can relink placeholder assets through its existing project
import flow. If `VITE_OPENREEL_URL` is configured at build time, the workbench
also exposes a separate-window link; no URL or untrusted iframe is enabled by
default.

OpenReel remains a fallback editor. It never owns Aether project persistence,
server credentials, or final server-side rendering. See
`docs/evidence/M4-0-VERIFICATION.md`.

## M1-0 MoneyPrinterTurbo Sidecar Integration

We have integrated a decoupled, isolated Client Adapter for the **MoneyPrinterTurbo** upstream tool.

### Pinned Upstream Specification
- **Repository**: [MoneyPrinterTurbo/MoneyPrinterTurbo](https://github.com/MoneyPrinterTurbo/MoneyPrinterTurbo)
- **Version**: `v1.2.7`
- **Commit SHA**: `475f21147f0808f5ffe3f58af9ab794b28a4da2c`
- **License**: MIT

### Architecture & Security Boundary
The MoneyPrinterTurbo sidecar runs as a completely isolated container next to Aether Studio. Communication happens exclusively through local/same-origin HTTP network requests made by the background Worker and the API:
- No secrets, personal credentials, or API keys are committed. All configurations are retrieved cleanly via environment variables.
- **Same-Origin API Routing**: The FastAPI API exposes proxy endpoints under `/api/moneyprinter/*` mapped to `/moneyprinter/*`, maintaining same-origin integrity.

### Integration Capabilities & Health Checks
- **Health Probing**: The endpoint `/api/moneyprinter/health` verifies connection status and responsiveness of the sidecar.
- **Capability Detection**: `/api/moneyprinter/capabilities` dynamically probes and reports the sidecar's features (video generation, subtitles, voiceover, and aspect ratio support).
- **Video Generation**: `POST /api/moneyprinter/generate` submits automated video creation tasks securely with validation schemas.
- **Status Checking**: `/api/moneyprinter/status/{task_id}` queries progress and handles failures.

### Failure, Timeout & Degradation Boundaries
- **Timeouts**: Configurable HTTP timeouts protect calls to the sidecar (default `10.0s`).
- **Retries**: Implements automatic, exponential backoff retries (up to `3` times) for temporary server-side failures (HTTP 429, >=500) or connection timeouts.
- **Error Mapping**: Converts generic HTTP errors and task failures into explicit, typed exceptions (`MoneyPrinterTimeoutError`, `MoneyPrinterConnectionError`, `MoneyPrinterTaskFailedError`).
- **Graceful Degradation**: If the MoneyPrinterTurbo sidecar is unreachable or undergoes catastrophic failure, a graceful fallback degradation is triggered. The adapter returns a clean, structured degraded status response and transitions features gracefully without crashing the core worker or API.

### M1-0 Mock Boundary
- The adapter client interfaces are fully implemented, validated, and integrated.
- The actual generation of video files depends on a correctly deployed MoneyPrinterTurbo container configured with necessary Pexels/LLM API credentials in production.
- `video-use` is reserved for M2 and not integrated in M1-0.

## M1-0 acceptance record

M1-0 is accepted on implementation merge commit
`1e916bf66e3f9e87cc2329cf6df94333d4f49744` after:

- PR #3 was reviewed and merged into `main`;
- all three required GitHub Actions jobs were green;
- Playwright and Docker artifacts present;
- no uncommitted changes;
- an explicit record of remaining mock boundaries.

The canonical evidence is `docs/evidence/M1-0-VERIFICATION.md`. This acceptance
does not start M2: real media processing and `video-use` remain future work and
require separate authorization.
