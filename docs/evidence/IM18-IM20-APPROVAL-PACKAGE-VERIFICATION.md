# IM18–IM20 Approval-Package Verification

## Baseline

- Repository baseline: `main@221540aa2fcb64df4012aa37f8bd017da8e29a9c`
- Prior accepted scope: IM15–IM17 Provider activation readiness and its post-merge documentation closure
- Verification type: documentation and source-gap audit only

## Repository findings

1. `.env.example` and `infra/docker/.env.example` default `AETHER_GENERATION_PROVIDER_MODE=disabled` and provide no real Provider credential.
2. `infra/docker/docker-compose.yml` starts the pinned MoneyPrinter Sidecar without a target `config.toml` bind mount.
3. The API receives only the generation operator mode; the Worker receives non-secret configVersionId/policyHash and the fixed internal Sidecar URL.
4. `moneyprinter-sidecar`, API, Worker and video-use currently share `aether-net`; the Sidecar publishes no host port but is reachable from other containers on that network.
5. Worker `MoneyPrinterTurboAdapter` disables proxy inheritance and redirects, validates same-origin artifacts and caps artifact bytes.
6. The Adapter defaults permit `/artifacts/` and `/api/v1/artifacts/`, while the pinned upstream completes files under `/tasks/...`; an exact published policy override is required before a canary can ingest an artifact.
7. Old Aether `/moneyprinter/*` endpoints return 410; API business code no longer performs direct Provider submit/status calls.
8. Existing Provider config policy stores only non-secret fields; secrets, URLs and secret-shaped values are rejected.
9. Existing readiness proves operator/config/policy/Adapter/pin/health agreement but does not prove target credentials or Provider account limits.
10. Existing quota tracks requests and generated seconds, not currency; a Provider-side hard cost limit remains external.
11. Existing rights flow blocks generated versions by default and requires explicit later governance/adoption.

## Exact pinned-upstream findings

The following files were read at the fixed commit `475f21147f0808f5ffe3f58af9ab794b28a4da2c`:

- `app/config/config.py` loads root `config.toml` and copies `config.example.toml` when absent.
- `config.example.toml` contains LLM Provider/model, Pexels/Pixabay and related credential settings.
- `app/controllers/v1/video.py` creates its router without the commented token dependency and records task parameters at normal log levels.
- `app/controllers/v1/video.py` resolves completed output URLs below `/tasks/...`; this differs from the current Adapter default artifact prefixes.
- The upstream Compose expects the source/config directory to be mounted; Aether's custom image does not currently mount a target config.

These facts justify a target-local read-only file boundary, network isolation, warning-level logging and non-sensitive canary inputs before any real request.

## Package completeness

- [x] Approval document defines IM18, IM19 and IM20 separately.
- [x] Baseline and fixed upstream commit are exact.
- [x] Secret ownership, mount, permission, logging and evidence rules are explicit.
- [x] Pinned runtime profile and exact same-origin `/tasks/` artifact policy are explicit.
- [x] Network topology and its limitations are explicit.
- [x] Private-canary state sequence, one-request budget and fail-closed cleanup are explicit.
- [x] 40 mandatory acceptance cases are numbered and executable in fake-only CI.
- [x] Coding file allowlist and stop-on-scope-delta rule are explicit.
- [x] Real Provider/model selection, credentials, paid call, target execution, deployment and commercial operation remain separate gates.

## Document-only scope assertion

This package adds five documentation files. It does not modify code, Compose, workflow, dependencies, APIs, migrations, Worker behavior, Provider configuration, credentials, target infrastructure, deployment or public access.

## Current result

`DOCUMENTATION_DRAFT_ONLY`. IM18–IM20 implementation has not started. No real Provider/model/material source has been selected or contacted; no key has been read; no paid call, private canary, deployment or public access is claimed.
