# IM18–IM20 PRD—Code Alignment

| Requirement | Accepted repository evidence | Current status |
|---|---|---|
| Governed generation authority | Project APIs, durable tasks, Worker lease/token, trusted intake and rights blocking remain authoritative | Accepted / reused |
| Activation control plane | Operator mode, published owner policy, matching fresh Worker proof, quota, circuit and kill switch remain mandatory | Accepted / reused |
| Sidecar secret input | Explicit private-canary Compose override binds repository-external `/MoneyPrinterTurbo/config.toml` read-only after preflight | Implemented / accepted |
| Secret exposure boundary | Preflight rejects unsafe files/configuration; API/Worker expose sanitized proof only; CI scans fake evidence and never injects real credentials | Implemented / accepted |
| Pinned artifact contract | Fixed upstream stays on `v1.2.7` / `475f211...`; canary policy requires exact same-origin `/tasks/` handling and rejects broader prefixes | Implemented / accepted |
| Sidecar API exposure | MoneyPrinter Sidecar is removed from `aether-net`; Worker uses internal `provider-control`; Sidecar alone has `provider-egress`; no host port | Implemented / accepted |
| Provider selection | PR #29 locks one candidate profile in `REAL-PROVIDER-PRIVATE-CANARY-EXECUTION-APPROVAL.md`; the lock is configuration-only and does not authorize execution | Candidate locked / execution gate remains |
| Cost containment | Repository canary profile enforces one task, one output, request limit 1 and generated-seconds limit 1–10; PR #29 records a CNY 5 candidate hard cap that must still be verified externally before execution | Repository control accepted / external verification pending |
| Canary orchestration | Preflight-first explicit arm/run/disarm controls, exact-SHA/owner/private-target gates and fail-closed cleanup are implemented | Implemented / accepted |
| Evidence | 40-case fake-only verification, sanitized evidence allowlist and full regression evidence are committed | Implemented / accepted |
| Upstream version | MoneyPrinterTurbo `v1.2.7` / `475f21147f0808f5ffe3f58af9ab794b28a4da2c` unchanged | Preserved boundary |
| Real credentials/calls | No real credential, Provider egress, paid call or real arm/run occurred in coding/review/CI; PR #29 explicitly keeps those actions unauthorized | Prohibited / not executed |
| Rights/adoption | Generated AssetVersion remains rights-blocked; no automatic adoption/timeline/render/publish path was added | Preserved invariant |
| Deployment/public access | Production-shaped Compose/runbook exists, but no target deployment or public launch occurred | External gate |

## Accepted implementation anchors

- Coding baseline: `main@f046fab83fafd79efad5e4f49801e7514527c032`
- Functional PR: #27
- Reviewed head: `7d7d6ef3c10b64e76934c1dae58bb1e32c3523ac`
- Merge commit: `0d7275836abfef26db7180076b23529b4f974f26`
- Final reviewed-head CI: `33440891212`
- Mandatory acceptance: `40/40 PASS`
- Subsequent candidate-lock PR: #29, merged as `e43c71166a6e525cad23c47dfd5f30a980d04625`
- Candidate-lock status: `CONFIGURATION_LOCKED / EXECUTION_NOT_AUTHORIZED`

## Alignment conclusion

IM18–IM20 closed the repository-level blockers that previously prevented a controlled private-canary path: target-local read-only configuration, unauthenticated Sidecar network isolation, exact pinned `/tasks/` artifact policy, sanitized credential/network/profile proof, one-task budget and deterministic shutdown now exist and are verified in fake-only CI.

PR #29 subsequently locked one candidate Provider/model/material-source/voice profile and a bounded canary budget. That documentation change does **not** activate a Provider and does not authorize target access, credential handling, Provider egress, paid use, `preflight`, `arm`, `run`, deployment or public access. Those remain explicit owner gates.

## Source-of-truth rule

As of `main@0d7275836abfef26db7180076b23529b4f974f26`, IM18–IM20 repository implementation is `ACCEPTED`. As of the later candidate-lock merge `e43c71166a6e525cad23c47dfd5f30a980d04625`, one real-canary configuration is `LOCKED` but execution remains `NOT_AUTHORIZED`. Repository acceptance is not credential approval; candidate locking is not paid-use or private-canary execution approval; any future private-canary success would still not constitute deployment, public-access or commercial-operation approval.
