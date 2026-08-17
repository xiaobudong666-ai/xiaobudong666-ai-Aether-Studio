# IM-3/IM-5 Governed Workbench Operations — Coding Approval Package

> Status: `DRAFT_FOR_OWNER_APPROVAL`
> Prepared: 2026-08-17
> Authoritative baseline: `main@88a8a76762e1820036dddbf61546bff0c3cf5f85`
> Depends on: accepted IM-1/IM-2 foundation in PR #9
> Decision owner: Aether Studio one-person OPC owner

## 1. Decision requested

Approve one bounded coding batch that makes the accepted M08/M09/M10 backend foundation usable from the existing React workbench:

1. IM-3 — asset-version and rights governance UI;
2. IM-4 — canonical task-state UI;
3. IM-5 — candidate adoption and master-revision UI.

This is the recommended next batch because the required backend records and APIs already exist. It converts accepted but hidden capability into an operator-visible closed loop without adding a model provider, plugin, paid call, migration or deployment change.

Approval of this package does not approve merge, deployment, public access, production data, real providers/plugins, final testing sign-off or commercial use.

## 2. Repository facts

| Domain | Accepted backend capability | Current workbench gap |
|---|---|---|
| M08 assets and rights | `AssetVersion`, SHA-256, probe metadata, `RightsSnapshot`, validity decisions | Upload UI discards the returned asset version and shows no rights state or rights action |
| M09 task center | Persistent queue, SSE, canonical task states plus legacy aliases | Task cards still interpret legacy `status` and do not present all canonical states explicitly |
| M07/M10 candidate and finished media | `Candidate`, explicit idempotent `Adoption`, immutable `MasterRevision`, export-rights gate | No candidate list, adoption action, rights-block explanation or master list exists in the web UI |

## 3. Operator journey

1. Owner or editor uploads media.
2. The asset card shows version number, SHA-256 fingerprint, probe metadata and current export-rights decision.
3. Owner or editor records a rights snapshot; viewer can inspect but cannot write.
4. Owner or editor places media on the timeline and submits a render.
5. Task center displays the canonical task state and progress through completion or failure.
6. A successful render appears as a candidate.
7. Owner or editor selects **Adopt as master**, supplies a reason and confirms.
8. The API enforces export rights and idempotency.
9. The resulting immutable master revision appears in the finished-media list with a download action.

## 4. IM-3 — asset-version and rights governance UI

### 4.1 Placement

- Keep the current three-panel workbench.
- Extend each item in the left **Asset Library** with a collapsible **Governance** section.
- Do not create a new top-level module navigation system in this batch.

### 4.2 Display fields

| Field | Source | Presentation |
|---|---|---|
| Asset version ID | `AssetVersion.id` | Copyable short ID with full value in title/accessible label |
| Version | `versionNo` | `v{number}` badge |
| SHA-256 | `sha256` | First 12 characters plus copy action |
| Media/content type | `mediaType`, `contentType` | Localized type label and MIME value |
| Size/duration/probe | `sizeBytes`, `probe` | Reuse current byte/duration formatters; show dimensions/codecs when present |
| Rights decision | rights-check response | `ALLOWED`, `MISSING`, `DENIED`, `REVOKED`, `UNKNOWN`, `NOT_YET_VALID`, `EXPIRED` localized badge |
| Purpose/territory | latest snapshot | Plain text; default authoring values `EXPORT` and `GLOBAL` |
| Validity | `validFrom`, `validUntil` | Local date-time or `No limit` |
| Evidence reference | `evidenceRef` | Text only; no arbitrary URL auto-navigation |

### 4.3 Actions and validation

- **Refresh rights**: all roles.
- **Record rights snapshot**: owner/editor only.
- Fields: status, purpose, territory, valid from, valid until, evidence reference.
- Require purpose and territory after trimming.
- Reject a validity end earlier than or equal to the start before API submission.
- Confirmation text must state that a new immutable snapshot is appended; previous evidence is not edited.
- No delete, overwrite or backdate-bypass action is included.

### 4.4 UI states

`LOADING`, `READY`, `EMPTY`, `SUBMITTING`, `SUCCESS`, `ERROR`, `FORBIDDEN`, `STALE_SESSION`.

## 5. IM-4 — canonical task-state UI

### 5.1 Canonical presentation

The UI must use `canonicalStatus` as authority and only fall back to the shared `canonicalTaskStatus(status)` normalizer for older responses.

| Canonical state | Chinese label | Terminal | Visual intent |
|---|---|---:|---|
| `QUEUED` | 排队中 | No | Neutral |
| `RUNNING` | 处理中 | No | Active |
| `SUCCEEDED` | 已完成 | Yes | Success |
| `FAILED` | 失败 | Yes | Error |
| `CANCELED` | 已取消 | Yes | Muted |
| `PARTIAL` | 部分完成 | Yes | Warning |
| `UNKNOWN` | 状态待确认 | No | Warning; query-first, never claim failure |

### 5.2 Behavior

- Preserve SSE updates and initial history query.
- Deduplicate by `taskId` and keep newest `updatedAt` value.
- Display progress, attempts, message and artifact action when available.
- `UNKNOWN` must prompt refresh/requery and must not expose retry, adopt or download without server evidence.
- No task cancellation or manual retry API is added in this batch.

## 6. IM-5 — candidate adoption and master-revision UI

### 6.1 Candidate list

Display candidate ID, task ID, input revision, status, created time and artifact preview/download link when authorized.

### 6.2 Adoption action

- Owner/editor only; viewer sees read-only state.
- Button: **Adopt as master**.
- Confirmation fields: reason, generated idempotency key and acknowledgement that adoption is explicit and irreversible in this batch.
- Idempotency key is generated once per user intent and reused for a retry of that same intent.
- A second click while submitting is disabled.
- On HTTP 409 idempotency replay, refresh candidates and masters before showing the outcome.
- On rights failure, show each media ID and server rights code; do not offer a client-side bypass.

### 6.3 Master list

Display revision number, master ID, source candidate, adopter, adoption reason, creation time, artifact reference and SHA-256 when present.

Actions:

- **Download master** when an authenticated artifact reference exists.
- **Copy ID**.
- No publish, withdraw, delete, replace, supersede or external-platform distribution action is included.

## 7. API contract matrix

| UI operation | Method and path | Roles | Expected result |
|---|---|---|---|
| List asset versions | `GET /api/projects/{projectId}/asset-versions` | owner/editor/viewer | Ordered asset-version list |
| Check latest rights | `GET /api/projects/{projectId}/asset-versions/{assetVersionId}/rights-check?purpose=EXPORT` | owner/editor/viewer | Decision plus latest snapshot |
| Record rights | `POST /api/projects/{projectId}/asset-versions/{assetVersionId}/rights-snapshots` | owner/editor | Immutable snapshot |
| List tasks | `GET /api/render-tasks?projectId={projectId}` | owner/editor/viewer | Task history with canonical status |
| Stream task updates | `GET /api/events` | owner/editor/viewer | Tenant-scoped SSE events |
| List candidates | `GET /api/projects/{projectId}/candidates` | owner/editor/viewer | Candidate list |
| Adopt candidate | `POST /api/projects/{projectId}/candidates/{candidateId}/adopt` | owner/editor | Master response; requires `Idempotency-Key` |
| List masters | `GET /api/projects/{projectId}/masters` | owner/editor/viewer | Immutable master list |
| Download artifact | Authenticated artifact reference returned by API | owner/editor/viewer | Streamed media or server error |

No new API route is authorized. A route change requires a separate approval delta explaining why the accepted contract is insufficient.

## 8. Permission matrix

| Operation | Owner | Editor | Viewer |
|---|---:|---:|---:|
| View assets/rights/tasks/candidates/masters | Yes | Yes | Yes |
| Upload and place media | Yes | Yes | No |
| Record rights snapshot | Yes | Yes | No |
| Submit render | Yes | Yes | No |
| Adopt candidate | Yes | Yes | No |
| Bypass rights or alter immutable records | No | No | No |

The UI is not a security boundary. Existing API enforcement remains authoritative and must be covered by regression tests.

## 9. Allowed change set

### 9.1 Code and tests

- `apps/web/src/App.tsx`
- `apps/web/src/App.test.tsx`
- `apps/web/src/components/AssetLibrary.tsx`
- `apps/web/src/components/PropertyInspector.tsx`
- New components under `apps/web/src/components/` limited to asset governance and finished-media operations
- `apps/web/src/i18n.ts` and its tests
- `apps/web/src/index.css`
- `packages/contracts/src/index.ts`
- `packages/contracts/src/schemas.ts`
- `packages/contracts/__tests__/schemas.test.ts`
- Existing `e2e/` workbench tests and fixtures required for the approved operator journey
- Documentation evidence, roadmap, alignment and implementation-ledger updates for this batch

### 9.2 Conditional test-only changes

- `apps/api/test_main.py` only when needed to add regression coverage for already-accepted endpoints.
- `.github/workflows/ci.yml` only when an existing test command cannot collect the new approved evidence; no permission expansion, secret, deployment or paid service may be added.

## 10. Explicitly prohibited

- Changes to API models, migrations, Worker, video-use, MoneyPrinter, infrastructure, authentication, quotas or deployment.
- New runtime or development dependencies.
- Real face/person replacement, wardrobe/background replacement, digital-avatar or image/video model integration.
- Short-video plugins, social-platform publishing, scraping, account automation or platform-evasion behavior.
- Paid calls, credentials, production data, production host changes or public access.
- Deleting legacy compatibility fields or performing a PostgreSQL migration.
- Marking the package complete without passing required tests and CI.

## 11. Error and recovery requirements

| Condition | Required UI behavior |
|---|---|
| 401 session expired | Clear protected state and return to login |
| 403 role denied | Preserve data, show read-only/permission message |
| 404 project/asset/candidate | Refresh project-scoped data and show not-found message |
| 409 idempotency/conflict | Requery candidate/master state before deciding success or conflict |
| 422 rights or validation failure | Show field or per-media reasons; no bypass |
| Network/SSE loss | Show disconnected state; preserve last data and allow manual refresh |
| `UNKNOWN` task state | Requery; never map to failed or succeeded |
| Duplicate user click | Disable during request and reuse the same intent key |

## 12. Required tests and acceptance evidence

### 12.1 Unit and component tests

- Asset version is retained after upload and rendered with version/hash.
- Every rights decision maps to the correct label and accessibility text.
- Rights form validates dates and immutable-snapshot confirmation.
- Viewer cannot see enabled write actions.
- Canonical status is authoritative; legacy alias fallback remains compatible.
- `UNKNOWN` remains nonterminal.
- Adoption reuses its idempotency key for same-intent retry and prevents double submission.
- Rights failures render per-media codes.
- Master revision renders and downloads through the authenticated reference.

### 12.2 Full regression

- Existing pnpm lint, build, unit tests and production dependency audit.
- Existing API, Worker and video-use tests plus any approved regression-only API tests.
- Existing Playwright desktop and 390px flows.
- Existing Docker Compose integration.

### 12.3 New end-to-end evidence

At least one CI browser flow must demonstrate:

1. upload a real fixture;
2. view asset version and missing-rights state;
3. record an allowed export-rights snapshot;
4. render the timeline;
5. observe canonical success;
6. adopt the candidate exactly once;
7. view and download the resulting master.

A second flow must demonstrate that missing or expired rights block adoption and expose the server reason.

## 13. Completion and rollback

Completion requires:

- all scoped behavior implemented;
- all existing and new gates passing;
- evidence document linked to exact commit and CI run;
- a draft PR reviewed and separately approved for merge.

Rollback is a normal Git revert of the batch. The batch must not delete accepted IM-1/IM-2 data or require a destructive database rollback.

## 14. Estimate and sequence

Recommended sequence:

1. shared DTO and canonical-state UI update;
2. asset governance UI;
3. candidate/master UI;
4. component and browser coverage;
5. full CI and evidence.

Expected size: one focused coding batch, approximately 6–10 agent execution hours plus CI time. This is an estimate, not a deadline or production-readiness claim.

## 15. Exact owner authorization text

To authorize implementation, the owner must provide this exact or materially equivalent approval:

> I approve implementation of IM-3/IM-5 exactly as defined in `docs/approvals/IM3-IM5-WEB-OPERATIONS-CODING-APPROVAL.md` section 9.1, including the conditional test-only changes in section 9.2 when demonstrably necessary. I do not authorize new dependencies, backend models or migrations, real providers/plugins, paid calls, production data, deployment, public access, merge to main or commercial operation. Any scope delta requires separate approval.

Until that approval is recorded, this file is planning evidence only and no IM-3/IM-5 feature code may be written.
