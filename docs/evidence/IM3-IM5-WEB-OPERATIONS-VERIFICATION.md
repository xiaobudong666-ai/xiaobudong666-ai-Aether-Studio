# IM-3/IM-5 Governed Workbench Operations — Verification

> Status: `FORMAL_REVIEW_REMEDIATED_AWAITING_FINAL_HEAD_CI`
> Baseline: `main@7959759814bfe5a0d1c65a0bd5c4a85139a9427b`
> Reviewed code candidate: `02e3854912ab6eb6030b0d72b214dd7c81f9857e`
> Candidate PR: [#11](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/pull/11)
> Last complete CI run before review remediation: [Pipeline #52](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/32090298013)
> Review-remediation run: [Pipeline #59](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/32228398136)
> Verification updated: 2026-08-19

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
| Web component/unit tests | 10 passed, including 3 project-switch isolation regressions |
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

1. real upload → asset version → missing rights → allowed immutable snapshot → timeline render → canonical success → one adoption → master display/download;
2. real upload → render candidate → adoption attempt without export rights → server rejection with per-media reason and no master.

## 6. Scope audit

- No dependency or lockfile change.
- No backend code, migration, Worker, provider, plugin or infrastructure change.
- No credential, paid call or production data.
- No deployment or public endpoint.
- No publish, withdraw, delete, replace, supersede or social-platform action.

## 7. Formal review finding and remediation

The owner authorized PR #11 to enter formal review. Review identified one blocking frontend isolation defect: a response started for a previously selected project could arrive after a project switch and overwrite the newly selected project's detail, task, candidate or master state.

The remediation:

- invalidates prior project-detail, task-history and finished-media request generations;
- verifies both the request generation and current project before committing asynchronous results;
- clears project-scoped UI state during a switch;
- filters rendered task state to the selected project;
- prevents late upload/save/adoption continuations from overwriting another project's UI;
- adds three delayed-response regression tests covering project detail, canonical task history and candidate/master lists.

Pipeline #59 passed ESLint, TypeScript compilation, the production build, all web/package tests and the Node dependency audit. Its FFmpeg package-install step remained in progress because of runner/package-source delay when this evidence update was committed; no code-test failure was reported.

## 8. Remaining gate

The new final-head CI run triggered by this evidence commit must complete successfully. After that, formal review may be recorded as passed with no unresolved blocking feedback.

Merge still requires a separate explicit owner approval. The candidate must not be represented as merged, deployed, production-ready or commercially approved. Independent security, legal/compliance and finance/tax review remain mandatory before formal commercial use.
