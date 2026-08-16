# IM-1/IM-2 Foundation Verification

> Branch: `feat/im1-im2-foundation-20260816`
> Baseline: `e876958b54e2fca1af5409150d3a9a29e857a9fa`
> Local verification date: 2026-08-16
> Status: `LOCAL_GATES_PASSED / CI_PENDING`
> Boundary: this evidence does not authorize merge, deployment, real providers, plugins, production data, or commercial use.

## Implemented increment

- Corrected M5-1 from conditional acceptance to post-merge repository acceptance.
- Added a PRD–code alignment matrix with explicit implementation boundaries.
- Added immutable typed `AssetVersion` and `RightsSnapshot` records while retaining legacy project materials.
- Added rights-window decisions for missing, denied, revoked, unknown, not-yet-valid and expired snapshots.
- Added render `Candidate`, explicit idempotent `Adoption`, and immutable `MasterRevision` records.
- Added tenant/project scoping and an export-rights gate before candidate adoption.
- Stored new render-task writes with canonical states and retained legacy read aliases.
- Added additive legacy-database migration, tenant isolation, immutability, rights, adoption and contract tests.

## Local verification

| Gate | Command/result |
|---|---|
| Python syntax and repository structure | `compileall`, JSON parse and `git diff --check` passed |
| API tests | 23 passed |
| Worker tests | 23 passed |
| video-use tests | 3 passed |
| TypeScript/Web tests | contracts 11, editor 4, web 5; all passed |
| Node lint | contracts, editor and web passed |
| Build/type-check | contracts, editor and Vite production build passed |
| Node production dependency audit | no known vulnerabilities |
| Python dependency audits | API, Worker and video-use: no known vulnerabilities |
| Total local automated tests | 69 passed |

Observed non-blocking warnings:

- Starlette reports that its `httpx` TestClient bridge is deprecated in favor of a future `httpx2` package; existing pinned dependencies and tests remain functional.
- Node reports WebAssembly module imports as experimental during editor tests; all editor tests pass.

## Environment-limited gates

- Local Playwright did not run because the browser binary download endpoint returned HTTP 502 with a certificate-validity/system-clock proxy error. This is an environment download failure, not an application test failure; the GitHub Actions Playwright job remains required.
- Docker Compose was not run locally because Docker is unavailable in the current execution environment. The GitHub Actions Docker integration job remains required.

## Required CI evidence

Before merge consideration, the branch must pass all repository jobs:

1. lint/build/unit tests and dependency audits;
2. authenticated desktop and 390px Playwright flow;
3. Docker Compose integration, authenticated upload/queue/Worker flow and exact four-second MP4 smoke.

CI success still does not represent production deployment or commercial approval.
