# Aether Studio Product & Engineering Roadmap

This document outlines the high-level roadmap and current milestone progress for the Aether Studio project.

## Milestone Progress

### [M0-0] Engineering Baseline (ACCEPTED)
- **Status**: Completed and fully verified.
- **Commit**: `507f61d3a85b9eb6d14af13426d0353ad05a59f9`
- **Key Features**: Three-panel workbench React app, FastAPI API, isolated worker, SQLite WAL with optimistic locking, same-origin router topology, Playwright end-to-end tests, and GitHub Actions gates.

### [M1-0] MoneyPrinterTurbo Sidecar Adapter Integration (IN_PROGRESS)
- **Status**: In Progress (Refactoring & Verification Phase).
- **Key Features**:
  - Independent `MoneyPrinterTurboAdapter` in the background Worker and FastAPI API.
  - Pin upstream to a fixed, auditable version of MoneyPrinterTurbo (`v1.2.7`, commit `b09b0b6bc7fa05e60d3d5f3dfd68377e68e4de80`).
  - Implements capability probing, health check, API error mapping, request timeout & exponential retry backoff.
  - Graceful degradation fallback when the sidecar is unreachable.
  - Configuration examples without embedding credentials.
  - Dedicated unit and integration tests verifying HTTP endpoints, timeouts, and fallback modes.

### [M2-0] Real Media Processing and Video-Use (FUTURE)
- **Status**: Planned.
- **Key Features**:
  - Transition from mock FFmpeg processing to actual media composition.
  - Introduce `video-use` capabilities.
  - Expand preview and decoding pipelines.

## Security & Upgrade Boundaries
- **No hardcoded credentials**: Environment variables handle sidecar configurations.
- **Upgrades**: Upgrading the sidecar must be done by explicitly bumping the pinned upstream version/commit and running compatibility validation tests.
