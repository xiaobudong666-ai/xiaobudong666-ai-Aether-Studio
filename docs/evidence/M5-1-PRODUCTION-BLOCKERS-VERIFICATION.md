# M5-1 Production Blockers Verification

## Candidate scope

This candidate closes the four blockers reproduced against `main@588ca4b9956`:

1. Missing authentication, permissions, tenant isolation, and resource quotas.
2. Canonical Timeline renders collapsing a four-second timeline to 2.048 seconds.
3. Nginx's default 1 MiB request-body limit rejecting ordinary video uploads.
4. API-memory render state disappearing across refreshes, restarts, and users.

## Implemented evidence

- Authentication uses server-side persistent sessions, HttpOnly/SameSite cookies,
  scrypt password hashes, CSRF proof, and owner/editor/viewer authorization.
- Every project, material proxy, MoneyPrinter task, render task, event stream, and
  artifact lookup is scoped to the authenticated tenant.
- Database-enforced quotas cover projects, stored bytes, concurrent renders, and
  monthly reserved render seconds.
- Canonical render requests retain integer numerator/timescale values until the
  Sidecar builds a bounded FFmpeg graph.
- The real-media test renders clips at 0s and 3s to an approximately 4s MP4 and
  samples the 2s frame to prove the gap is black. The same test covers a second
  video layer, independent audio, subtitles, and idempotent request IDs.
- Render tasks persist in SQLite. The Worker claims a lease, submits to video-use,
  renews progress, retries transient failures, and leaves completed history and
  authenticated artifact links available after refresh or API restart.
- Nginx streams request bodies with a 2 GiB ceiling; API and Sidecar apply the
  same single-file ceiling while the tenant storage quota remains authoritative.

## Local verification

- API: 19 tests passed.
- Worker: 23 tests passed.
- video-use: 3 tests passed with real FFmpeg/ffprobe.
- Contracts/editor/web: 15 tests passed.
- ESLint and TypeScript/Vite production build passed.
- JavaScript production and full dependency audits: no known vulnerabilities.
- API, Worker, and video-use Python requirement audits: no known vulnerabilities.
- Python compilation, focused Ruff import/unused checks, and focused Ruff security
  checks passed.
- Compose configuration and image/runtime integration remain pending the GitHub
  Runner because Docker is not available in the local Work Mode environment.
- Local Playwright execution reached server startup but Chromium installation was
  blocked by a CDN certificate-time 502. The PR Playwright job is therefore an
  acceptance gate and must provide the desktop/narrow screenshots.

## Acceptance gate

Do not merge until the exact PR head passes all three required GitHub Actions jobs:

1. Lint, build, JavaScript/Python unit tests, and real FFmpeg tests.
2. Playwright authenticated desktop and narrow-viewport flow with screenshots.
3. Docker Compose health, fixed-upstream checks, direct Sidecar render, and the
   authenticated Nginx upload → persistent Worker queue → four-second MP4 smoke.
