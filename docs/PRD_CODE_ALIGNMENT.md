# Aether Studio PRD–Code Alignment

> Baseline: `main@378e4db17ed0120a94707df48c55257f422a3fc7`
> Review date: 2026-08-21 (UTC+8)
> Status vocabulary: `IMPLEMENTED`, `PARTIAL`, `NOT_IMPLEMENTED`, `EXTERNAL_GATE`
> Boundary: this matrix records repository facts; it does not claim production deployment or commercial approval.

## Current alignment

| PRD domain | Status | Reusable repository capability | Remaining boundary |
|---|---|---|---|
| M01 project workbench | PARTIAL | Tenant-scoped project CRUD, optimistic revision, React workbench | Full content tree, templates, archive/delete governance and 13-module navigation remain |
| M02 AI comic creation | PARTIAL | MoneyPrinter adapter and structured project/timeline foundation | Story bible, shot production authority and real provider quality gates remain |
| M03 digital avatar | NOT_IMPLEMENTED | Provider-neutral adapter boundary only | Identity consent, training, voice/appearance versions and revocation chain remain |
| M04 image-to-video/action | PARTIAL | video-use/FFmpeg sidecar and media pipeline | Versioned reference/mapping inputs and controlled model adapter remain |
| M05 face/person replacement | NOT_IMPLEMENTED | No approved real provider | Consent, safety, controlled generation and review remain |
| M06 wardrobe/background | NOT_IMPLEMENTED | No approved real provider | Versioned transformation inputs, quality and review remain |
| M07 smart editing | PARTIAL | Rational timeline, tracks/clips, FFmpeg render, OpenCut/OpenReel exports, accepted candidate adoption and immutable master UI | Richer editing operations remain |
| M08 assets and rights | PARTIAL | Upload, probe, immutable `AssetVersion`, `RightsSnapshot`, rights-window decisions, tenant storage quota and accepted governance UI | Full rights-history view and hold enforcement remain |
| M09 task center | PARTIAL | Persistent leased render queue, recovery, retry, SSE, canonical status authority and accepted canonical-state UI | Full Attempt/Checkpoint/DeadLetter operations remain |
| M10 finished media | PARTIAL | Authenticated artifacts, Candidate, explicit Adoption, immutable MasterRevision and accepted candidate/master UI | Publication and withdrawal remain |
| M11 metering and plans | PARTIAL | Project/storage/concurrency/monthly render quotas | Quote/Reservation/Usage/Settlement ledger remains |
| M12 team and permissions | PARTIAL | Session auth, owner/editor/viewer RBAC, tenant isolation | Membership, policy, data scope, approval and segregation-of-duties UI remain |
| M13 settings | PARTIAL | Environment configuration and isolated adapters | ConfigVersion, SecretVersion, connector registry and publish/rollback remain |
| Production launch | EXTERNAL_GATE | Compose/runbook/CI smoke path exists | Host, TLS, backup, provider credentials, load evidence and explicit production approval remain |
| Commercial use | EXTERNAL_GATE | No claim | Independent security, legal/compliance and finance/tax review remain mandatory |

## IM-1/IM-2 accepted increment

- Correct M5-1 post-merge repository evidence.
- Add immutable typed asset versions and rights snapshots without removing legacy project materials.
- Separate render candidates, explicit adoption and immutable master revisions.
- Store new render-task writes with canonical states while preserving legacy read compatibility.
- Add tenant isolation, idempotency, rights-window and migration regression tests.

Pull request [#9](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/pull/9) passed all three CI jobs and merged to `main` as `88a8a76762e1820036dddbf61546bff0c3cf5f85`.

No real AI provider, short-video plugin, paid call, production data, deployment, or PostgreSQL big-bang migration was part of this increment.

## Accepted IM-3/IM-5 repository increment

The owner approved the bounded implementation, formal review and exact-head merge. Pull request [#11](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/pull/11) passed formal review with FR-01 resolved and no unresolved blocking feedback.

The accepted increment exposes the existing M08/M09/M10 backend foundation in the React workbench:

- asset-version and rights governance UI;
- canonical task-state presentation and explicit `UNKNOWN` recovery;
- candidate adoption and immutable master-revision UI;
- stale-response isolation across project switches;
- allowed-adoption and missing-rights production-browser evidence.

The final reviewed head `4f125209ad664f3f90f397cf386115704c6fa471` passed all three jobs in [Pipeline #64](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/32230132028) and merged to `main` as `378e4db17ed0120a94707df48c55257f422a3fc7`.

Detailed scope and evidence remain in `docs/approvals/IM3-IM5-WEB-OPERATIONS-CODING-APPROVAL.md` and `docs/evidence/IM3-IM5-WEB-OPERATIONS-VERIFICATION.md`.

This repository acceptance does not authorize or claim a real AI provider, short-video plugin, paid call, production data, deployment, public access or commercial operation. Independent security, legal/compliance and finance/tax review remain mandatory before commercial use.
