# IM-3/IM-5 Governed Workbench Operations — Verification

> Status: `CI_VERIFIED_DRAFT_CANDIDATE`
> Baseline: `main@7959759814bfe5a0d1c65a0bd5c4a85139a9427b`
> Code candidate commit: `4185f18e06b3ed74085e1c6be250e5258deaf045`
> Candidate PR: [#11](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/pull/11)
> CI run: [Pipeline #51](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/32089357299)
> Verification date: 2026-08-18

## 1. Authorization recorded

The Aether Studio one-person OPC owner authorized PR #10 to be reviewed and merged, then authorized implementation under sections 9.1 and conditional 9.2 of `docs/approvals/IM3-IM5-WEB-OPERATIONS-CODING-APPROVAL.md`.

The same authorization explicitly withheld approval for new dependencies, backend migrations, real plugins or models, paid calls, production data, deployment, public access and merge of the resulting feature pull request.

PR #10 merged to `main` as `7959759814bfe5a0d1c65a0bd5c4a85139a9427b` before this implementation branch was created.

## 2. Implemented candidate

### IM-3 — asset version and rights governance

- The upload response retains the typed `AssetVersion` instead of discarding it.
- Project selection loads the authoritative asset-version list.
- Each material exposes version, copyable SHA-256, MIME/type, size and probe metadata.
- Export-rights checks display all accepted decisions: allowed, missing, denied, revoked, unknown, not-yet-valid and expired.
- Owner/editor can append an immutable rights snapshot with purpose, territory, validity window and text-only evidence reference.
- The client rejects blank purpose/territory and an end time that is not later than the start time.
- Viewer remains read-only; no overwrite, delete or client-side rights bypass exists.

### IM-4 — canonical task state

- `canonicalStatus` is authoritative; legacy `status` is normalized only when the canonical field is absent.
- Task history and SSE updates are deduplicated by task ID using the newest `updatedAt`/`createdAt` value.
- All seven canonical states have explicit Chinese labels and terminal/nonterminal presentation.
- `UNKNOWN` exposes requery, never claims success/failure and never exposes an artifact without canonical success.
- Progress, attempts, server message, update time and authenticated artifact action remain visible when applicable.
- SSE disconnect preserves the last known data and manual refresh remains available.

### IM-5 — candidate adoption and immutable masters

- The workbench lists candidate identity, task, input revision, status, creation time and authenticated artifact action.
- Owner/editor adoption requires a reason and an explicit irreversible-action acknowledgement.
- A random idempotency key is generated once per user intent, reused after an interrupted retry and protected against double submission.
- HTTP 404/409 refresh project-scoped candidate/master data before presenting the outcome.
- HTTP 422 rights failures show each media ID, optional asset-version ID and server rights code without a bypass.
- The immutable master list shows revision, IDs, adopter, reason, timestamp, SHA-256 when available and authenticated download.
- Viewer can inspect candidates/masters but cannot start adoption.

## 3. Contract and error coverage

- Added runtime schemas for rights decisions, rights checks and render-task responses.
- Preserved the accepted API routes and server-side RBAC as the security boundary.
- Added localized handling for rights, candidate, idempotency and adoption errors.
- Session expiry clears protected project state and returns the user to login.
- No API route, API model, database migration, Worker, infrastructure or authentication file changed.

## 4. Local verification results

| Gate | Result |
|---|---|
| Web component/unit tests | 7 passed |
| Contract tests | 11 passed |
| Editor tests | 4 passed |
| API tests | 23 passed |
| Worker tests | 23 passed |
| video-use tests | 3 passed |
| Web/package ESLint | Passed |
| Web/package TypeScript checks | Passed |
| Vite production build | Passed; 55 modules transformed |
| Diff whitespace check | Passed |
| Playwright collection | 5 tests collected: 3 workbench plus 2 production governance flows |

The browser tests could not execute locally because this sandbox did not contain Chromium and its browser download proxy returned an invalid/truncated archive plus a certificate-time 502. The API and Vite servers both started successfully before browser launch. This is recorded as an environment limitation, not a passed browser gate. GitHub Actions browser and Docker jobs remain mandatory.

## 5. CI verification

GitHub Actions Pipeline #51 completed successfully against the draft-PR candidate:

- lint, compilation, type checks, production build, unit/package tests and Node/Python production dependency audits passed;
- API, Worker and video-use regression tests passed;
- Playwright workbench flow passed and uploaded evidence;
- Docker Compose built and reached a healthy stack;
- FFmpeg/ffprobe, pinned video-use, real render, authenticated upload, persistent queue, Worker and canonical task state passed;
- both production-browser governance flows passed and uploaded their evidence.

The production browser suite contains two explicit flows:

The production browser suite now contains two explicit flows:

1. real upload → asset version → missing rights → allowed immutable snapshot → timeline render → canonical success → one adoption → master display/download;
2. real upload → render candidate → adoption attempt without export rights → server rejection with per-media reason and no master.

## 6. Scope audit

- No dependency or lockfile change.
- No backend code, migration, Worker, provider, plugin or infrastructure change.
- No credential, paid call or production data.
- No deployment or public endpoint.
- No publish, withdraw, delete, replace, supersede or social-platform action.

## 7. Remaining gate

The candidate must remain a draft until the owner separately authorizes formal review. Merge requires another explicit owner approval after that review.

`CI_VERIFIED_DRAFT_CANDIDATE` must not be represented as accepted, merged, deployed, production-ready or commercially approved. Independent security, legal/compliance and finance/tax review remain mandatory before formal commercial use.
