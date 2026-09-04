# IM18–IM20 Roadmap

## Accepted implementation anchors

- Coding baseline: `main@f046fab83fafd79efad5e4f49801e7514527c032`
- Functional PR: #27
- Formally reviewed head: `7d7d6ef3c10b64e76934c1dae58bb1e32c3523ac`
- Accepted merge commit: `0d7275836abfef26db7180076b23529b4f974f26`
- Final reviewed-head CI: `33440891212`
- Mandatory acceptance: `40/40 PASS`
- Subsequent candidate-lock PR: #29, merged as `e43c71166a6e525cad23c47dfd5f30a980d04625`

## Evidence-based sequence

1. **IM18 — target-local secret configuration boundary**: implemented and accepted.
2. **IM19 — Provider network and interface isolation**: implemented and accepted.
3. **IM20 — private one-task canary controls and deterministic shutdown**: repository controls implemented and accepted; real private-canary execution remains separately unauthorized.
4. **Real-canary candidate lock**: one candidate runtime profile is recorded by PR #29 as `CONFIGURATION_LOCKED / EXECUTION_NOT_AUTHORIZED`.

## Accepted repository state

IM18–IM20 now provides the bounded repository path required before any real Provider credential can be considered:

- committed/base runtime remains `disabled`;
- target-local Provider configuration is accepted only through an explicit read-only Sidecar mount;
- secret-safe preflight rejects unsafe files/configuration and emits only sanitized proof;
- MoneyPrinter Sidecar is removed from `aether-net`, Worker alone shares `provider-control`, and Sidecar alone has `provider-egress`;
- API/Worker readiness requires sanitized credential/network/profile proof in addition to the existing operator/owner/attestation/quota/circuit/kill-switch gates;
- the future canary profile is constrained to one task, one output and 1–10 generated seconds with exact `/tasks/` artifact handling;
- failure/interrupt/disarm is fail-closed and preserves rights-blocked output with no automatic adoption, timeline write, render or publish;
- CI remains fake-only and performs zero real Provider credential or paid call.

## Gate status

- IM15–IM17 repository implementation and documentation: accepted.
- IM18–IM20 approval package: accepted.
- IM18–IM20 repository implementation: accepted via PR #27.
- IM18–IM20 40-case acceptance and full regression: passed.
- Real-canary candidate Provider/model/material-source/voice-path: **locked via PR #29**.
- Candidate lock status: **configuration only; execution not authorized**.
- Real target-local credentials or credential inspection: **not approved**.
- Provider-side hard monetary-limit evidence and any paid use: **not approved for execution**.
- Target access or real `preflight`: **not approved**.
- Real private-canary `arm`/`run`: **not approved**.
- Deployment, public access, expanded trial and commercial operation: **not approved**.

## Invariants

- Source, base Compose, environment templates and CI default to `disabled`.
- Real configuration remains outside the Git worktree and is read-only to the Sidecar only.
- API, Web and video-use cannot connect to the unauthenticated MoneyPrinter Sidecar.
- CI uses disposable fake configuration only and performs no real Provider request.
- MoneyPrinterTurbo remains pinned to `v1.2.7` / `475f21147f0808f5ffe3f58af9ab794b28a4da2c`.
- A future real canary requires a new owner authorization tied to an exact accepted `main` SHA, the locked runtime profile, external evidence completion, monetary hard cap and rollback window.
- Generated output remains rights-blocked; no automatic adoption, timeline write, render or publish.

## Next gate

The repository implementation is accepted and PR #29 has locked a single candidate configuration, but neither fact authorizes a real canary. The next decision is to complete the external execution evidence required by `docs/approvals/REAL-PROVIDER-PRIVATE-CANARY-EXECUTION-APPROVAL.md`, then request a new explicit authorization for target access, target-local credential mounting, paid-use allowance, one `preflight`, one `arm`, one `run` and mandatory `disarm`. Deployment and expanded use remain later gates.
