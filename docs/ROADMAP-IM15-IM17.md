# IM15–IM17 Roadmap

## Planning baseline

`main@db2a23bc95e7990f2652b5fe38c625ce232a16de`

## Evidence-based sequence

1. **IM15 — versioned provider configuration and runtime attestation**: documentation proposed; implementation not started.
2. **IM16 — hardened MoneyPrinter Adapter and restricted artifact streaming**: documentation proposed; implementation not started.
3. **IM17 — generation quota, usage, circuit breaker and emergency stop**: documentation proposed; implementation not started.

## Why this is next

IM12–IM14 established durable project generation tasks, Worker leases, trusted API intake, immutable AssetVersion creation and rights-default blocking. The remaining smallest path toward a safely activatable real Provider is not credential entry or deployment. It is a deny-by-default activation control plane that can prove API/Worker configuration agreement, constrain Adapter egress and artifact bytes, bound tenant use, trip a circuit breaker and stop new work without destroying evidence.

Current source still enables capability snapshots only for `deterministic-fake`; Worker generation rejects every other mode. The API also retains legacy `/moneyprinter/*` routes that directly probe, submit to and query the Adapter outside the governed project task/Worker path. The current MoneyPrinter Adapter has submit/status methods but no accepted restricted artifact-stream contract. Existing quotas cover projects, storage and rendering, not generation reservations or settled generated seconds.

## Gate status

- Documentation drafting: in progress in a documentation-only PR.
- Documentation formal review: not approved.
- Documentation merge: not approved.
- Functional coding: not approved and not started.
- Real-provider activation: prohibited.
- Credentials and paid calls: prohibited.
- Deployment/public access: prohibited.

## Invariants

- Runtime mode defaults to `disabled` in source, Compose and environment templates.
- Legacy API-direct MoneyPrinter routes are retired; browser generation uses only protected project-scoped APIs.
- Automated tests use a deterministic fake Sidecar only and make no public Provider request.
- Secrets remain outside repository, database, DTOs, logs and browser storage.
- MoneyPrinterTurbo remains pinned to `v1.2.7` / `475f21147f0808f5ffe3f58af9ab794b28a4da2c`.
- Generated output remains rights-blocked; no automatic adoption, timeline write, render or publish.

## Non-goals

No real provider/plugin/model, key, paid call, upstream upgrade, new dependency, external queue/object storage, digital human or identity transformation, automatic rights approval/adoption/timeline/render/publish, deployment, public access or commercial operation.
