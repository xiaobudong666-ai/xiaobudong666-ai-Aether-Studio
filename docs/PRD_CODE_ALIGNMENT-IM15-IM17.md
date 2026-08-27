# IM15–IM17 PRD—Code Alignment

| Requirement | Current repository evidence | Gap proposed for IM15–IM17 | Current status |
|---|---|---|---|
| Governed server generation | IM12–IM14 project API, persistent tasks, Worker leases and trusted intake are accepted | Preserve as the task/data authority | Implemented / must reuse |
| Runtime provider gate | Capability enables only for `deterministic-fake`; other modes are rejected | Non-secret config versions, operator/owner dual key and fresh Worker attestation | Not implemented |
| Legacy Provider bypass | API still exposes unauthenticated health/capability probes and API-direct generate/status routes | Retire legacy routes so only protected project APIs and Worker Adapter can access generation | Blocking gap |
| Configuration provenance | Task stores a capability snapshot hash | Bind real-mode claims to a published config version and policy hash | Not implemented |
| Secret boundary | Existing environment variables and internal Sidecar network | Prove secrets never enter Aether DB, DTO, logs, events or browser | Partial |
| MoneyPrinter contract | Pinned Adapter submits and queries | Restricted artifact stream, normalized errors, cancel capability and egress constraints | Partial |
| Ambiguous submission | IM13 preserves UNKNOWN and prevents blind repost | Preserve the rule on the real-mode Adapter path | Implemented foundation / needs activation evidence |
| Generation quota | Storage/render quotas exist | Generation concurrency, monthly requests/seconds, reservation and settlement | Not implemented |
| Failure containment | Bounded task retry and cancellation exist | Persistent tenant/provider circuit breaker and half-open probe | Not implemented |
| Emergency stop | Disabled mode prevents all real generation today | Audited owner stop/recovery that cannot override operator disable | Not implemented |
| Frontend authority | GenerationPanel consumes server tasks and rights state | Display readiness, quotas, circuit/stop reasons without secrets | Partial |
| Rights and adoption | Generated AssetVersion starts blocked; editor reference is `adopted=false` | Preserve unchanged | Implemented invariant |
| M11 metering and plans | Project/storage/render quota fields exist | Add non-monetary generation usage ledger only | Partial |
| M13 settings | Environment configuration and isolated adapters exist | Add immutable non-secret Provider ConfigVersion publish/rollback chain | Partial |
| Real provider/API key | No authorized runtime connectivity | Remains outside this documentation and coding batch | Prohibited |
| Production launch | Compose/runbook path exists | Target, TLS, credentials, load and launch evidence remain external | External gate |

## Alignment conclusion

The repository now has the durable task, Worker, artifact, provenance and rights bridge required before a Provider can be considered, but the legacy API-direct MoneyPrinter routes must first be retired so they cannot bypass that bridge. The next safe repository slice is activation readiness: configuration agreement, restricted Adapter traffic, bounded generation usage, failure containment and an auditable stop path.

This slice must be testable entirely against a deterministic fake Sidecar. It may make the code capable of accepting a future explicitly enabled MoneyPrinter mode, but it must leave every committed default disabled and must not supply credentials, make a real call or claim operational AI generation.

## Source-of-truth rule

Until the documentation package is formally reviewed and merged, and the owner later authorizes coding against an exact `main` SHA, all IM15–IM17 rows remain `NOT_IMPLEMENTED`. Documentation acceptance is not coding acceptance; coding acceptance is not real-provider activation, paid-use or deployment approval.
