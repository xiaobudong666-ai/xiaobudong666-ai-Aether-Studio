# IM18–IM20 PRD—Code Alignment

| Requirement | Accepted repository evidence | Current status |
|---|---|---|
| Governed generation authority | Project APIs, durable tasks, Worker lease/token, trusted intake and rights blocking remain authoritative | Accepted / reused |
| Activation control plane | Operator mode, published owner policy, matching fresh Worker proof, quota, circuit and kill switch remain mandatory | Accepted / reused |
| Sidecar secret input | Explicit private-canary Compose override binds repository-external `/MoneyPrinterTurbo/config.toml` read-only after preflight | Implemented / accepted |
| Secret exposure boundary | Preflight rejects unsafe files/configuration; API/Worker expose sanitized proof only; CI scans fake evidence and never injects real credentials | Implemented / accepted |
| Pinned artifact contract | Fixed upstream stays on `v1.2.7` / `475f211...`; canary policy requires exact same-origin `/tasks/` handling and rejects broader prefixes | Implemented / accepted |
| Sidecar API exposure | MoneyPrinter Sidecar is removed from `aether-net`; Worker uses internal `provider-control`; Sidecar alone has `provider-egress`; no host port | Implemented / accepted |
| Provider selection | MoneyPrinter remains the outer Adapter; inner real LLM/model/material/voice profile is intentionally not selected | External decision gate |
| Cost containment | Repository canary profile enforces one task, one output, request limit 1 and generated-seconds limit 1–10 | Implemented; Provider monetary hard cap remains external gate |
| Canary orchestration | Preflight-first explicit arm/run/disarm controls, exact-SHA/owner/private-target gates and fail-closed cleanup are implemented | Implemented / accepted |
| Evidence | 40-case fake-only verification, sanitized evidence allowlist and full regression evidence are committed | Implemented / accepted |
| Upstream version | MoneyPrinterTurbo `v1.2.7` / `475f21147f0808f5ffe3f58af9ab794b28a4da2c` unchanged | Preserved boundary |
| Real credentials/calls | No real credential, Provider egress, paid call or real arm/run occurred in coding/review/CI | Prohibited / not executed |
| Rights/adoption | Generated AssetVersion remains rights-blocked; no automatic adoption/timeline/render/publish path was added | Preserved invariant |
| Deployment/public access | Production-shaped Compose/runbook exists, but no target deployment or public launch occurred | External gate |

## Accepted implementation anchors

- Coding baseline: `main@f046fab83fafd79efad5e4f49801e7514527c032`
- Functional PR: #27
- Reviewed head: `7d7d6ef3c10b64e76934c1dae58bb1e32c3523ac`
- Merge commit: `0d7275836abfef26db7180076b23529b4f974f26`
- Final reviewed-head CI: `33440891212`
- Mandatory acceptance: `40/40 PASS`

## Alignment conclusion

IM18–IM20 closed the repository-level blockers that previously prevented a controlled private-canary path: target-local read-only configuration, unauthenticated Sidecar network isolation, exact pinned `/tasks/` artifact policy, sanitized credential/network/profile proof, one-task budget and deterministic shutdown now exist and are verified in fake-only CI.

This does **not** mean a real Provider is activated. The repository still contains no approved real Provider/model/material-source/voice-path selection, no real target credential, no Provider-side monetary hard-limit evidence and no authorization to execute `arm` or `run`. Those remain external owner gates.

## Source-of-truth rule

As of `main@0d7275836abfef26db7180076b23529b4f974f26`, IM18–IM20 repository implementation is `ACCEPTED`. Repository acceptance is not Provider selection; Provider selection is not credential or paid-use approval; credential approval is not private-canary execution approval; private-canary success would still not constitute deployment, public-access or commercial-operation approval.
