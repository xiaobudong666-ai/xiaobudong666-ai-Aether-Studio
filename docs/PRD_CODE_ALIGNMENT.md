# Aether Studio PRD–Code Alignment

> Baseline: `main@7959759814bfe5a0d1c65a0bd5c4a85139a9427b`
> Review date: 2026-08-18
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
| M07 smart editing | PARTIAL | Rational timeline, tracks/clips, FFmpeg render, OpenCut/OpenReel exports, candidate/adoption/master authority; candidate adoption UI is present in the local IM-5 candidate | Richer editing UI and remote candidate acceptance remain |
| M08 assets and rights | PARTIAL | Upload, probe, immutable `AssetVersion`, `RightsSnapshot`, rights-window decisions and tenant storage quota; governance UI is present in the local IM-3 candidate | Full rights-history view, hold enforcement and remote candidate acceptance remain |
| M09 task center | PARTIAL | Persistent leased render queue, recovery, retry, SSE and canonical status compatibility; canonical-state UI is present in the local IM-4 candidate | Full Attempt/Checkpoint/DeadLetter and remote candidate acceptance remain |
| M10 finished media | PARTIAL | Authenticated artifacts, Candidate, explicit Adoption and immutable MasterRevision; candidate/master UI is present in the local IM-5 candidate | Publication, withdrawal and remote candidate acceptance remain |
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

## Approved IM-3/IM-5 implementation candidate

The owner approved the bounded batch on 2026-08-18. Draft pull request [#11](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/pull/11) now exposes the accepted M08/M09/M10 backend foundation in the existing React workbench:

- asset-version and rights governance UI;
- canonical task-state presentation;
- candidate adoption and master-revision UI.

The detailed scope and approval record are in `docs/approvals/IM3-IM5-WEB-OPERATIONS-CODING-APPROVAL.md`. GitHub Actions [Pipeline #51](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/32089357299) passed all three jobs; exact verification is in `docs/evidence/IM3-IM5-WEB-OPERATIONS-VERIFICATION.md`.

This candidate is not accepted or merged. Separate owner approval is required before formal review and again before merge. No real AI provider, short-video plugin, paid call, production data, deployment or commercial operation is included.
