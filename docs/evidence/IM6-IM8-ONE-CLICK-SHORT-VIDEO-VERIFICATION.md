# IM-6/IM-8 One-Click Short-Video Workbench — Verification

> Status: `ACCEPTED_REPOSITORY_MILESTONE`
> Implementation baseline: `main@d9a95f811d2874410679ac2fff27306cfbbeb605`
> Final reviewed head: `9563d0af76e93f25d30be60b2806392749da6358`
> Candidate PR: [#14](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/pull/14)
> Final-head CI: [Pipeline #83](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/32416784651)
> Merge commit: `main@526ddcf354571c68bf54ea4e3ea057592fdd472d`
> Merged at: `2026-08-21T04:07:06Z`
> Verification updated: 2026-08-21 (UTC+8)

## 1. Authorization recorded

The Aether Studio one-person OPC owner approved implementation under sections 11.1 and 12 of `docs/approvals/IM6-IM8-ONE-CLICK-SHORT-VIDEO-CODING-APPROVAL.md`, while preserving all prohibitions in section 11.2.

The owner separately authorized PR #14 to enter formal review and separately authorized merge of exact head `9563d0af76e93f25d30be60b2806392749da6358`. The merge authorization explicitly excluded deployment, public access, real plugins/models, paid calls and scope expansion.

## 2. Accepted implementation

### IM-6 — governed quick-create entry and media orchestration

- Owner/editor can use the current project or create a new project; viewer remains read-only.
- Existing media and up to twenty local files can be arranged in a confirmed deterministic order.
- Uploads execute sequentially and partial completion remains visible instead of claiming rollback.
- A run token, project identity and request generation prevent late responses from contaminating another project.
- Existing project/timeline/task/candidate/master capabilities remain authoritative; the implementation does not create a second workbench or API client.

### IM-7 — deterministic Canonical Timeline 1.1 layout

- Original-duration and fixed one-to-thirty-second modes produce deterministic clip order and rational positions.
- Existing non-empty timelines require explicit replacement confirmation.
- Optimistic revision conflict stops the run and requires refresh; the client does not overwrite a newer server revision.
- The generated timeline is saved once only after the applicable rights checks pass.

### IM-8 — mandatory rights, single render and Candidate handoff

- Every selected media item is mapped to an AssetVersion and receives an export-rights decision.
- A missing, denied, revoked, unknown, not-yet-valid or expired result blocks the flow.
- Newly uploaded media without a RightsSnapshot produces zero timeline saves and zero render POSTs.
- After explicit governance through the existing asset interface, resume re-reads project/version/rights state and reuses the uploaded version without duplicate upload.
- The client submits at most one render request for a confirmed run; an ambiguous response is not automatically retried.
- Success hands off to the existing canonical task and Candidate views. Adoption and MasterRevision creation remain separate explicit operations.

## 3. Acceptance-case coverage

| Approval case | Verified repository behavior |
|---|---|
| QC-01/QC-03 | Ordered existing media and original/fixed-duration deterministic timeline layout |
| QC-02/QC-06 | New upload enters rights block with zero timeline save/render, then resumes after explicit governance without re-upload |
| QC-04 | Existing non-empty timeline requires explicit replacement confirmation |
| QC-05 | Viewer sees read-only state and cannot issue write requests |
| QC-07 | Duplicate activation produces exactly one render POST |
| QC-08 | Ambiguous render response refreshes task state without automatic repost |
| QC-09 | Project switch invalidates the prior run and ignores late results |
| QC-10 | Partial upload reports the successful immutable version and failed item |
| QC-11 | HTTP 409 stops without overwriting the server project |
| QC-12 | `UNKNOWN` does not display success, download or a successful Candidate |
| QC-13 | Render success exposes a Candidate while adoption calls remain zero |
| QC-14/QC-15 | Production browser proves real render/download and the rights-block/governance-resume path |
| QC-16 | Diff contains only the nine approved function/test files |

## 4. Final-head validation

The final reviewed head passed the following repository gates:

| Gate | Result |
|---|---|
| Web ESLint and TypeScript | Passed |
| Vite production build | Passed |
| Web unit/component tests | 20 passed |
| Contract tests | 11 passed |
| Editor tests | 4 passed |
| Node/Python dependency audits | Passed |
| Playwright workbench flow | Passed |
| Docker Compose and service health | Passed |
| FFmpeg, Worker, video-use and persistent queue | Passed |
| Real production-browser upload-to-download flow | Passed |
| Diff whitespace and file allowlist | Passed |

GitHub Actions [Pipeline #83](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/32416784651) completed successfully against exact head `9563d0af76e93f25d30be60b2806392749da6358`.

The production-browser evidence exercises a real upload, missing-rights block, explicit governance, reuse of the original uploaded version, one render submission and authenticated download.

## 5. Scope audit

The accepted implementation changes exactly these nine approved files:

- `apps/web/src/components/QuickCreatePanel.tsx`
- `apps/web/src/components/QuickCreatePanel.test.tsx`
- `apps/web/src/App.tsx`
- `apps/web/src/index.css`
- `apps/web/src/i18n.ts`
- `apps/web/src/i18n.test.ts`
- `apps/web/src/App.test.tsx`
- `e2e/workbench.spec.ts`
- `e2e/production.spec.ts`

It contains:

- no dependency or lockfile change;
- no API, data model, migration, Worker, authentication or infrastructure change;
- no real provider, plugin, model, credential, paid call or production data;
- no deployment, public endpoint, automatic adoption, publication or withdrawal.

## 6. Formal review and merge

Formal review inspected the exact final head and recorded no unresolved blocking findings. Because the repository owner also authored the PR, the review result was recorded as a non-approving GitHub review comment rather than a self-approval; this does not change the inspected head, finding count or separate owner merge authorization.

After the owner separately authorized merge of exact head `9563d0af76e93f25d30be60b2806392749da6358`, PR #14 was squash-merged to protected `main` as `526ddcf354571c68bf54ea4e3ea057592fdd472d` at `2026-08-21T04:07:06Z`.

## 7. Remaining boundaries

This closes the IM-6/IM-8 repository milestone. It does **not** represent or authorize:

- AI image/video generation, a digital avatar, face/person replacement or wardrobe/background transformation;
- a real provider, plugin or model;
- a new dependency, backend API/model, migration or Worker change;
- paid calls, production data or production credentials;
- deployment, TLS/domain work or public access;
- production readiness or commercial operation.

Independent security, legal/compliance and finance/tax review remain mandatory before formal commercial use. Future functional scope and its merge remain subject to the applicable approval gates.
