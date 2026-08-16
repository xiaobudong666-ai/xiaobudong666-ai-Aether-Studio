# Aether Studio PRD–Code Alignment

> Baseline: `main@e876958b54e2fca1af5409150d3a9a29e857a9fa`
> Review date: 2026-08-16
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
| M07 smart editing | PARTIAL | Rational timeline, tracks/clips, FFmpeg render, OpenCut/OpenReel exports | Candidate/adoption/master authority and richer editing UI remain |
| M08 assets and rights | PARTIAL | Upload, probe, project material JSON, tenant storage quota | `AssetVersion`, `RightsSnapshot`, evidence and hold enforcement are introduced in IM-2 and require validation |
| M09 task center | PARTIAL | Persistent leased render queue, recovery, retry, SSE | Canonical status compatibility is introduced in IM-2; full Attempt/Checkpoint/DeadLetter/UI remains |
| M10 finished media | PARTIAL | Authenticated render artifact download | Candidate, Adoption and MasterRevision are introduced in IM-2; publication/withdrawal remains |
| M11 metering and plans | PARTIAL | Project/storage/concurrency/monthly render quotas | Quote/Reservation/Usage/Settlement ledger remains |
| M12 team and permissions | PARTIAL | Session auth, owner/editor/viewer RBAC, tenant isolation | Membership, policy, data scope, approval and segregation-of-duties UI remain |
| M13 settings | PARTIAL | Environment configuration and isolated adapters | ConfigVersion, SecretVersion, connector registry and publish/rollback remain |
| Production launch | EXTERNAL_GATE | Compose/runbook/CI smoke path exists | Host, TLS, backup, provider credentials, load evidence and explicit production approval remain |
| Commercial use | EXTERNAL_GATE | No claim | Independent security, legal/compliance and finance/tax review remain mandatory |

## IM-1/IM-2 approved increment

- Correct M5-1 post-merge repository evidence.
- Add immutable typed asset versions and rights snapshots without removing legacy project materials.
- Separate render candidates, explicit adoption and immutable master revisions.
- Store new render-task writes with canonical states while preserving legacy read compatibility.
- Add tenant isolation, idempotency, rights-window and migration regression tests.

No real AI provider, short-video plugin, paid call, production data, deployment, or PostgreSQL big-bang migration is part of this increment.
