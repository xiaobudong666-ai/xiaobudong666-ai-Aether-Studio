# Aether Studio — M0-0 Engineering Baseline

Aether Studio is an AI anime and short-video editing project. This repository
contains the M0-0 engineering foundation: a runnable web workbench, API,
isolated worker, shared contracts, container topology, tests, and CI gates.

The repository is independent and does not read, import, or modify `Ai-Eos`.

## Architecture

| Area | Purpose |
| --- | --- |
| `apps/web` | React, TypeScript, and Vite three-panel editing workbench |
| `apps/api` | FastAPI project CRUD, SQLite WAL, optimistic locking, render mock, and SSE |
| `apps/worker` | Isolated Python Worker with explicitly mocked FFmpeg, AI, and recovery adapters |
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

Create one Python environment and install both service requirements:

```bash
python3 -m venv .venv
.venv/bin/pip install -r apps/api/requirements.txt
.venv/bin/pip install -r apps/worker/requirements.txt
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
pnpm e2e
```

Run the complete container stack:

```bash
docker compose -f infra/docker/docker-compose.yml config --quiet
docker compose -f infra/docker/docker-compose.yml up -d --build --wait
curl --fail http://127.0.0.1/api/health
docker compose -f infra/docker/docker-compose.yml exec -T worker ffmpeg -version
docker compose -f infra/docker/docker-compose.yml exec -T worker ffprobe -version
docker compose -f infra/docker/docker-compose.yml down --volumes
```

The GitHub Actions workflow has three required jobs:

1. Lint, build, JavaScript tests, API tests, and Worker tests.
2. Playwright browser flow with screenshots and HTML report artifacts.
3. Docker Compose build, health checks, same-origin proxy check, FFmpeg checks,
   and log artifact.

See `docs/evidence/M0-0-VERIFICATION.md` for the evidence policy and current
limitations. A green job proves only the scope asserted by that job.

## Implemented M0-0 behavior

- Project list, create, query, and update.
- Atomic revision conflict response (`409 CONCURRENCY_CONFLICT`).
- Three-panel workbench and bottom timeline.
- Materials and basic track/clip placement.
- Bounded 480p proxy specification.
- Mock render task and live `task_progress` SSE events.
- Independent Worker HTTP health endpoint.
- Explicit mock boundaries for FFmpeg, AI provider, and recovery.
- Reproducible lockfile, Node/pnpm baseline, Docker topology, and CI.

## M0-0 limitations

- FFmpeg adapter methods are mocks; the container contains FFmpeg and ffprobe,
  but M0-0 does not process real media.
- AI generation and subtitle methods are mocks and use no provider keys.
- Task state is in process memory; Redis/Celery durability is deferred.
- The preview canvas is a workbench placeholder, not a decoding or compositing
  engine.
- Offline persistence and multi-device merge UI are deferred.
- RationalTime is exact only while reduced results remain within the declared
  JavaScript safe-integer boundary.

These limitations must not be presented as completed production capabilities.

## M1-0 MoneyPrinterTurbo Sidecar Integration

We have integrated a decoupled, isolated Client Adapter for the **MoneyPrinterTurbo** upstream tool.

### Pinned Upstream Specification
- **Repository**: [MoneyPrinterTurbo/MoneyPrinterTurbo](https://github.com/MoneyPrinterTurbo/MoneyPrinterTurbo)
- **Version**: `v1.2.7`
- **Commit SHA**: `b09b0b6bc7fa05e60d3d5f3dfd68377e68e4de80`
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

## M1 entry criteria

M1 may start only after the latest feature-branch commit has:

- all three GitHub Actions jobs green;
- Playwright and Docker artifacts present;
- no uncommitted changes;
- a reviewed pull request against `main`;
- an explicit record of remaining mock boundaries.
