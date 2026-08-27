# IM15–IM17 Roadmap

## Accepted implementation baseline

`main@ad7e505d6d131d12e2c18c5c255a6ae034b62fbd`

## Sequence

1. **IM15 — versioned provider configuration and runtime attestation**: **implemented**.
2. **IM16 — hardened MoneyPrinter Adapter and restricted artifact streaming**: **implemented**.
3. **IM17 — generation quota, usage, circuit breaker and emergency stop**: **implemented**.

## Closed gates

- Documentation gate: **closed / accepted** through the IM15–IM17 approval-package workflow and squash merge of PR #23 as `16d987d4265e4fa4aea346f493277b7869585d55`.
- Coding authorization gate: **closed** by the owner's explicit authorization against `main@16d987d4265e4fa4aea346f493277b7869585d55`, including the separately approved CI-only file-scope extension.
- Functional implementation gate: **closed / accepted** by squash merge of PR #24.
- Verification gate: **closed** with 48/48 mandatory acceptance cases, all full regressions and CI Pipeline #126 passing.
- Formal-review blockers: **0** on reviewed head `54dbbd676426b08325f826a83fde26cbecd66659`; `FR24-01` was fixed before acceptance.

## Current implementation status

**Implemented within the authorized activation-readiness scope.** Aether now retires the legacy API-direct `/moneyprinter/*` bypass; stores immutable non-secret Provider configuration versions; requires exact operator mode, published owner policy and a fresh matching Worker attestation; binds claimed tasks to configuration and policy hashes; constrains Adapter requests, redirects, proxy inheritance and artifact streams; accounts for generation reservations, releases and settlements; persists circuit-breaker state and audited emergency stop/recovery; and exposes server-authoritative readiness, quota, circuit and stop state to the frontend.

Worker queue rejections for cancellation, lease loss and emergency stop preserve the API's stable governance code and stop local processing without overwriting the authoritative terminal state.

## Open activation gate

The runtime Provider remains **disabled by default** in source, Compose and environment templates. The merged implementation makes a future explicitly governed activation technically checkable; it does not activate MoneyPrinter, provide credentials, make a paid/public Provider request or authorize production use.

Real Provider/plugin/model activation, API keys or other credentials, paid usage, target-environment configuration, production security/load evidence, deployment, public access and commercial operation each remain separate owner gates.

## Preserved invariants

- Browser generation uses only protected project-scoped APIs; API runtime does not submit to, poll or download from the Provider.
- Tests use deterministic fake behavior only and make no public Provider request.
- Secrets remain outside repository, database, DTOs, logs, events and browser storage.
- MoneyPrinterTurbo remains pinned to `v1.2.7` / `475f21147f0808f5ffe3f58af9ab794b28a4da2c`.
- Generated output remains rights-blocked; there is no automatic adoption, timeline write, render or publish.
- No dependency, lockfile, external queue, object-storage, secret-management or billing-system change was accepted.
