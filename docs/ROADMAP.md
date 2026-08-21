# Aether Studio Product & Engineering Roadmap

This document outlines the high-level roadmap and current milestone progress for the Aether Studio project.

## Milestone Progress

### [M0-0] Engineering Baseline (ACCEPTED)
- **Status**: Completed and fully verified.
- **Commit**: `507f61d3a85b9eb6d14af13426d0353ad05a59f9`
- **Key Features**: Three-panel workbench React app, FastAPI API, isolated worker, SQLite WAL with optimistic locking, same-origin router topology, Playwright end-to-end tests, and GitHub Actions gates.

### [M1-0] MoneyPrinterTurbo Sidecar Adapter Integration (ACCEPTED)
- **Status**: Completed and fully verified.
- **Commit**: `1e916bf66e3f9e87cc2329cf6df94333d4f49744`
- **Evidence**: `docs/evidence/M1-0-VERIFICATION.md`
- **Key Features**:
  - Independent `MoneyPrinterTurboAdapter` in the background Worker and FastAPI API.
  - Pin upstream to a fixed, auditable version of MoneyPrinterTurbo (`v1.2.7`, commit `475f21147f0808f5ffe3f58af9ab794b28a4da2c`).
  - Implements capability probing, health check, API error mapping, request timeout & exponential retry backoff.
  - Graceful degradation fallback when the sidecar is unreachable.
  - Configuration examples without embedding credentials.
  - Dedicated unit and integration tests verifying HTTP endpoints, timeouts, and fallback modes.

### [M2-0] Real Media Processing and Video-Use (ACCEPTED)
- **Status**: Merged and fully verified.
- **Commit**: `8e81ba20ab33bff5d089f738fe535bb9346e6a28`
- **CI**: [GitHub Actions run 29](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/31205880579)
- **Evidence**: `docs/evidence/M2-0-VERIFICATION.md`
- **Key Features**:
  - Replace the Worker FFmpeg mock with actual probe, proxy, and audio extraction operations.
  - Pin `browser-use/video-use` `0.1.0` to commit `92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66`.
  - Isolate the upstream helpers in a non-root internal Sidecar with persistent media storage.
  - Add real upload/probe, EDL rendering, task progress, timeline-view, transcription, and artifact endpoints.
  - Connect project timelines to the Sidecar through the same-origin API and expose MP4 downloads.

### [M3-0] OpenCut Compatibility Core (ACCEPTED)
- **Status**: Merged and fully verified.
- **Commit**: `35798c63a7614cb4fa4856109d8dc2fb942450fa`
- **CI**: [GitHub Actions run 31](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/31207113712)
- **Evidence**: `docs/evidence/M3-0-VERIFICATION.md`
- **Boundary**: OpenCut Classic is officially archived and the rewrite's Editor API is not released. Aether integrates the pinned `opencut-wasm@0.2.10` core and a Classic v31 snapshot adapter without making the archived application a runtime dependency.
- **Key Features**:
  - Use the real OpenCut Rust/WASM media-time and frame-alignment implementation.
  - Translate Aether projects, tracks, clips, trims, and media references into an audited compatibility snapshot.
  - Lazy-load the 3 MB WASM chunk only when OpenCut export is requested.
  - Keep Canonical Timeline persistence and server-side video-use rendering as the source of truth.

### [M4-0] OpenReel Compatibility Adapter (ACCEPTED)
- **Status**: Merged and fully verified.
- **Commit**: `35798c63a7614cb4fa4856109d8dc2fb942450fa`
- **CI**: [GitHub Actions run 31](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/31207113712)
- **Evidence**: `docs/evidence/M4-0-VERIFICATION.md`
- **Boundary**: OpenReel remains a feature-flagged fallback, not a second default editor runtime.
- **Key Features**:
  - Export OpenReel's actual `1.0.0` project-file schema from the same Canonical Timeline and media contracts.
  - Preserve media as explicit relinkable placeholders instead of duplicating browser-local blobs.
  - Expose a separate-window editor link only when `VITE_OPENREEL_URL` is explicitly configured.
  - Keep Aether storage and server-side rendering authoritative.

### [M5-0] Production Deployment and Launch (READY FOR TARGET)
- **Status**: Repository launch path merged and verified. An actual host change remains blocked on target configuration.
- **Commit**: `35798c63a7614cb4fa4856109d8dc2fb942450fa`
- **CI**: [GitHub Actions run 31](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/31207113712)
- **Runbook**: `docs/PRODUCTION_DEPLOYMENT.md`
- **Implemented safeguards**:
  - MoneyPrinterTurbo and video-use remain internal-only; no Sidecar host ports are published.
  - Both Sidecar images shallow-fetch and verify their pinned upstream commit; MoneyPrinterTurbo avoids location-specific package mirrors.
  - API and Worker diagnostic ports bind to loopback only.
  - The public Web listener is configurable for direct or reverse-proxy deployment.
  - All services set `no-new-privileges`; persistent volume teardown is excluded from operator commands.
  - A production smoke script verifies Web, API, and both Sidecar capability paths through the same origin.
- **Required external configuration**:
  - VPS or container host and domain/TLS termination.
  - ElevenLabs key for Scribe transcription.
  - MoneyPrinterTurbo provider keys for generation and licensed stock sources.

### [M5-1] Production Blocker Remediation (ACCEPTED)
- **Status**: PR #8 merged to `main` after all three GitHub Actions jobs passed. This repository milestone acceptance does not represent an external production deployment.
- **PR**: [#8](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/pull/8)
- **Code CI**: [GitHub Actions run 37](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/31244761577)
- **Code Head**: `489ba8220feb46916a3a700a654828b55b72e768`
- **Evidence**: `docs/evidence/M5-1-PRODUCTION-BLOCKERS-VERIFICATION.md`
- **Key Features**:
  - Persistent HttpOnly sessions, scrypt password hashes, owner/editor/viewer RBAC,
    tenant-scoped projects/media/tasks, CSRF proof, and first-start owner bootstrap.
  - Enforced project, storage, concurrent-render, and monthly-render-second quotas.
  - Canonical Timeline FFmpeg composition that preserves exact rational positions,
    black gaps, overlapping video layers, independent audio, and subtitles.
  - A database-backed leased render queue consumed by the Worker, with retry,
    Sidecar idempotency, API restart recovery, persistent history, SSE updates,
    and authenticated artifact access.
  - A 2 GiB Nginx/API/Sidecar transport ceiling plus a tenant storage quota.
  - Authenticated full-stack CI smoke with a media upload larger than 1 MiB and a
    four-second gap-render assertion.
  - Production and development JavaScript dependencies plus all three Python
    requirement sets report no known vulnerabilities at the candidate audit date.

### [IM-1/IM-2] Typed Asset and Adoption Foundation (ACCEPTED)
- **Status**: Merged and fully verified as a repository milestone.
- **PR**: [#9](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/pull/9)
- **Merge commit**: `88a8a76762e1820036dddbf61546bff0c3cf5f85`
- **CI**: [GitHub Actions run 46](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/31959023181)
- **Evidence**: `docs/evidence/IM1-IM2-FOUNDATION-VERIFICATION.md`
- **Key Features**:
  - Immutable, tenant-scoped asset versions with SHA-256 and probe metadata.
  - Immutable rights snapshots with purpose, territory and validity-window decisions.
  - Canonical task-state persistence with legacy response compatibility.
  - Render candidates, explicit idempotent adoption and immutable master revisions.
  - Rights enforcement before candidate adoption plus migration and tenant-isolation coverage.
- **Boundary**: The accepted increment does not include a workbench UI for the new records, a real AI provider, a short-video plugin, production deployment or commercial approval.

### [IM-3/IM-5] Governed Workbench Operations (ACCEPTED)
- **Status**: PR #11 passed formal review, all three final-head CI jobs, and merged to `main` as a repository milestone.
- **Baseline**: `7959759814bfe5a0d1c65a0bd5c4a85139a9427b`
- **PR**: [#11](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/pull/11)
- **Reviewed head**: `4f125209ad664f3f90f397cf386115704c6fa471`
- **Merge commit**: `378e4db17ed0120a94707df48c55257f422a3fc7`
- **Final CI**: [Pipeline #64](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/32230132028)
- **Approval package**: `docs/approvals/IM3-IM5-WEB-OPERATIONS-CODING-APPROVAL.md`
- **Evidence**: `docs/evidence/IM3-IM5-WEB-OPERATIONS-VERIFICATION.md`
- **Accepted controls**:
  - Asset-version, hash, probe and export-rights governance in the existing asset library.
  - Owner/editor immutable rights-snapshot capture with viewer read-only behavior.
  - Canonical task states, newest-update deduplication, preserved SSE and explicit `UNKNOWN` recovery.
  - Candidate adoption with stable idempotency, rights-failure details and immutable master revisions.
  - Project-switch isolation guards plus three delayed-response regression tests.
  - Production-browser coverage for allowed adoption and missing-rights blocking.
- **Formal review**: FR-01 was resolved; unresolved blocking feedback is 0.
- **Boundary**: Acceptance records repository state only. It does not authorize or claim a real provider/plugin/model, paid call, production data, deployment, public access or commercial operation.

### [IM-6/IM-8] One-Click Short-Video Workbench (ACCEPTED)
- **Status**: PR #14 passed formal review and final-head CI, then merged to `main` as a repository milestone.
- **Implementation baseline**: `main@d9a95f811d2874410679ac2fff27306cfbbeb605`
- **PR**: [#14](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/pull/14)
- **Reviewed head**: `9563d0af76e93f25d30be60b2806392749da6358`
- **Merge commit**: `526ddcf354571c68bf54ea4e3ea057592fdd472d`
- **Final CI**: [Pipeline #83](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/32416784651)
- **Approval package**: `docs/approvals/IM6-IM8-ONE-CLICK-SHORT-VIDEO-CODING-APPROVAL.md`
- **Evidence**: `docs/evidence/IM6-IM8-ONE-CLICK-SHORT-VIDEO-VERIFICATION.md`
- **Accepted controls**:
  - governed current/new-project quick-create flow with owner/editor writes and viewer read-only behavior;
  - ordered existing-media selection plus bounded sequential uploads;
  - deterministic Canonical Timeline 1.1 layout using original or fixed clip duration;
  - explicit existing-timeline replacement confirmation and optimistic-revision conflict handling;
  - AssetVersion mapping and mandatory export-rights preflight;
  - zero timeline saves and zero render submissions while rights are blocked;
  - governance resume that reuses uploaded versions without duplicate upload;
  - one client-side render submission with project/request-generation isolation;
  - Candidate handoff while Adoption and MasterRevision remain separate explicit operations.
- **Formal review**: The exact final head passed with no unresolved blocking findings.
- **Boundary**: Acceptance records repository state only. It does not authorize or claim AI generation, digital avatar, face/person replacement, wardrobe/background transformation, a real provider/plugin/model, paid calls, new dependencies, backend changes, deployment, public access or commercial operation.

## Security & Upgrade Boundaries
- **No hardcoded credentials**: Environment variables handle sidecar configurations.
- **Upgrades**: Upgrading the sidecar must be done by explicitly bumping the pinned upstream version/commit and running compatibility validation tests.
- **Media isolation**: `video-use` is internal-only and receives validated IDs rather than arbitrary host paths.
