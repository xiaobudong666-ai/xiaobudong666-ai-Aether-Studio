# IM18–IM20 PRD—Code Alignment

| Requirement | Current repository evidence | Gap proposed for IM18–IM20 | Current status |
|---|---|---|---|
| Governed generation authority | Project APIs, durable tasks, Worker lease/token, trusted intake and rights blocking are accepted | Preserve unchanged | Implemented / must reuse |
| Activation control plane | Operator mode, published owner policy, matching fresh Worker proof, quota, circuit and kill switch | Preserve as mandatory precondition | Implemented / must reuse |
| Sidecar secret input | Pinned upstream reads `/MoneyPrinterTurbo/config.toml`; Aether has no target secret mount | Explicit target-local read-only override with repository-external file checks | Not implemented |
| Secret exposure boundary | Aether policy rejects secret-shaped values and DTOs are sanitized | Prove target config is visible only to Sidecar and absent from inspect/log/evidence/database | Partial |
| Pinned artifact contract | Fixed upstream returns completed files under `/tasks/...`; current Adapter defaults allow only `/artifacts/` and `/api/v1/artifacts/` | Require empty upstream `endpoint` and exact published `artifactPathPrefixes=["/tasks/"]`; reject broad `/`, cross-origin and config/policy mismatch | Blocking gap |
| Sidecar API exposure | No host port, but pinned unauthenticated API shares `aether-net` with API/Worker/video-use | Separate internal control and Sidecar-only egress networks | Blocking gap |
| Provider selection | MoneyPrinter is fixed as the outer Adapter; inner LLM/material/TTS path remains unspecified | Require exactly one separately approved runtime profile before real execution | External decision gate |
| Cost containment | Aether limits concurrency, request count and generated seconds | Add one-request canary profile; require Provider-side monetary hard cap separately | Partial + external gate |
| Canary orchestration | Owner kill switch and provider mode exist | Preflight/arm/run/disarm order with fail-closed cleanup | Not implemented |
| Evidence | Task/attempt/event/usage/provenance/rights records exist | Produce allowlisted secret-free private-canary evidence | Partial |
| Upstream version | MoneyPrinterTurbo `v1.2.7` / `475f211...` fixed | No upgrade or patch in this slice | Preserved boundary |
| Real credentials/calls | None authorized or supplied | Remain outside coding and CI; require separate target approval | Prohibited |
| Rights/adoption | Generated AssetVersion defaults blocked; editor reference is `adopted=false` | Preserve unchanged | Implemented invariant |
| Deployment/public access | Production-shaped Compose/runbook exists | No target/public launch in this slice | External gate |

## Alignment conclusion

The repository has the task, governance, artifact, quota and emergency controls required around a Provider, but it does not yet have a safe way to supply the pinned Sidecar with target-local credentials, prevent other application containers from reaching that unauthenticated Sidecar, or bind the pinned upstream `/tasks/...` output contract to the Adapter's allowlist. Turning on `moneyprinter` now would therefore be incomplete and would not constitute a controlled activation.

IM18–IM20 is the smallest repository slice that closes those operational gaps without selecting a real model, reading a key or making a paid request. Its tests must use a deterministic fake Sidecar and temporary fake TOML only. A future real canary remains a separate owner decision with one explicit runtime profile, monetary hard cap, private target and immediate rollback.

## Source-of-truth rule

Until this documentation package is formally reviewed and merged, and the owner later authorizes coding against an exact new `main` SHA, every IM18–IM20 row remains `NOT_IMPLEMENTED`. Documentation acceptance is not coding approval; coding acceptance is not Provider/model selection; selection is not credential, paid-use, private-canary, deployment or commercial approval.
