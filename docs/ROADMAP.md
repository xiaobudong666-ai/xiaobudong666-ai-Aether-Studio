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

### [M2-0] Real Media Processing and Video-Use (IN REVIEW)
- **Status**: Implemented on `agent/m2-0-real-media-video-use`; awaiting PR/CI acceptance.
- **Evidence**: `docs/evidence/M2-0-VERIFICATION.md`
- **Key Features**:
  - Replace the Worker FFmpeg mock with actual probe, proxy, and audio extraction operations.
  - Pin `browser-use/video-use` `0.1.0` to commit `92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66`.
  - Isolate the upstream helpers in a non-root internal Sidecar with persistent media storage.
  - Add real upload/probe, EDL rendering, task progress, timeline-view, transcription, and artifact endpoints.
  - Connect project timelines to the Sidecar through the same-origin API and expose MP4 downloads.

### [M3-0] OpenCut Compatibility Core (IN DEVELOPMENT)
- **Status**: Implemented locally after M2-0; PR/CI acceptance remains pending.
- **Evidence**: `docs/evidence/M3-0-VERIFICATION.md`
- **Boundary**: OpenCut Classic is officially archived and the rewrite's Editor API is not released. Aether integrates the pinned `opencut-wasm@0.2.10` core and a Classic v31 snapshot adapter without making the archived application a runtime dependency.
- **Key Features**:
  - Use the real OpenCut Rust/WASM media-time and frame-alignment implementation.
  - Translate Aether projects, tracks, clips, trims, and media references into an audited compatibility snapshot.
  - Lazy-load the 3 MB WASM chunk only when OpenCut export is requested.
  - Keep Canonical Timeline persistence and server-side video-use rendering as the source of truth.

### [M4-0] OpenReel Compatibility Adapter (PLANNED FALLBACK)
- **Status**: Planned after OpenCut integration.
- **Boundary**: OpenReel remains a feature-flagged fallback, not a second default editor runtime.
- **Key Features**:
  - Provide a compatibility adapter for the same Canonical Timeline and media contracts.
  - Prove import/export and preview capability without duplicating Aether project storage.

### [M5-0] Production Deployment and Launch (BLOCKED ON TARGET CONFIG)
- **Status**: Deployment manifests and health gates are in progress; a production host/domain and secrets are still required for an actual public launch.
- **Required external configuration**:
  - VPS or container host and domain/TLS termination.
  - ElevenLabs key for Scribe transcription.
  - MoneyPrinterTurbo provider keys for generation and licensed stock sources.

## Security & Upgrade Boundaries
- **No hardcoded credentials**: Environment variables handle sidecar configurations.
- **Upgrades**: Upgrading the sidecar must be done by explicitly bumping the pinned upstream version/commit and running compatibility validation tests.
- **Media isolation**: `video-use` is internal-only and receives validated IDs rather than arbitrary host paths.
