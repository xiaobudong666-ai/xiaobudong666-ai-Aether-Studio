# M0-0 Verification Evidence

This file defines what evidence is required before the Aether Studio M0-0
foundation can be accepted. It intentionally does not copy terminal output or
claim a check passed without a reproducible repository or GitHub Actions
record.

## Runtime baseline

- Node.js: 24 LTS (`>=24 <25`)
- pnpm: 10.30.3
- Python: 3.12
- SQLite: WAL mode asserted by the API test
- FFmpeg and ffprobe: installed and queried inside the Worker container

## Required commands

```bash
pnpm install --frozen-lockfile
pnpm lint
pnpm build
pnpm test
PYTHONPATH=apps/api python -m pytest apps/api/test_main.py -q
PYTHONPATH=apps/worker python -m pytest apps/worker/test_worker.py -q
pnpm e2e
docker compose -f infra/docker/docker-compose.yml config --quiet
docker compose -f infra/docker/docker-compose.yml build
docker compose -f infra/docker/docker-compose.yml up -d --wait
git diff --check
```

## GitHub Actions evidence

The authoritative CI run for the accepted commit must contain:

| Job | Required evidence |
| --- | --- |
| `Lint, build, and unit tests` | Frozen install, lint, build/type-check, JS tests, API tests, Worker tests |
| `Playwright workbench flow` | Browser test result, HTML report, screenshot/test-result artifact |
| `Docker Compose integration` | Compose parse, three image builds, healthy services, `/api/health`, FFmpeg and ffprobe, log artifact |

The exact run URL and commit SHA belong in the release or pull-request
acceptance note. They are not hard-coded here because reruns and later commits
must not inherit an earlier green result.

## Test assertions

### Contracts

- Rational time operands and timescales are safe integers.
- Exact comparisons use BigInt cross-products.
- Addition and subtraction use gcd/lcm reduction.
- Unsafe reduced results fail instead of silently losing precision.
- 24, 24000/1001, 30000/1001, and 60000/1001 rates are covered.
- Long timelines and near-safe-integer comparisons are covered.

### API

- Every test uses a unique SQLite database under pytest `tmp_path`.
- `/health` must report `journal_mode=WAL`.
- Project list, create, query, update, and 404 behavior are covered.
- Two updates racing on the same revision produce exactly one success and one
  conflict.
- Render creation is explicitly marked as a mock.
- SSE evidence contains `task_progress` for the created task, not only a
  heartbeat.

### Worker

- FFmpeg, AI provider, and recovery tests are explicitly mock tests.
- `BACKEND_URL` is read from the environment.
- The HTTP health test uses an OS-assigned port.
- The HTTP client uses `trust_env=False`, so host proxy variables do not alter
  the test.

### Web and Playwright

- Unit tests assert the same-origin `/api` and `/api/events` paths.
- Playwright verifies the three workbench panels, timeline, project creation,
  render trigger, and visible task progress.
- The screenshot and HTML report are uploaded from repository-defined tests;
  no `/home/jules/verification` path is valid evidence.

## Local environment note

Docker is not installed in every review sandbox. When unavailable locally,
container results must remain marked unverified until the `Docker Compose
integration` GitHub Actions job succeeds. A Compose file parse alone is not a
container acceptance result.

## Mock boundary

The following are scaffolds, not production capabilities:

- FFmpeg proxy generation and audio extraction.
- AI style transfer and subtitle generation.
- Interrupted-task recovery.
- Durable render queue and distributed execution.
- Real media preview, decoding, compositing, and export.

Passing adapter tests verifies interface behavior only. It does not prove real
media or AI processing.
