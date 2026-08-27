# IM12–IM14 PRD—Code Alignment

| Requirement | Current repository evidence | Gap proposed for IM12–IM14 | Current status |
|---|---|---|---|
| Governed generation UI | IM9–IM11 `GenerationPanel` and deterministic local adapter | Replace browser-local task authority with protected project API | Local-only / accepted |
| Provider capability gate | MoneyPrinter Adapter exposes health/capabilities | Authenticated, short-lived, sanitized server capability snapshot | Partial |
| Project-scoped generation task | `DBExternalTask` stores tenant/requester/engine/status | Dedicated project task, immutable request snapshot and idempotency | Not implemented |
| Durable orchestration | RenderTask already proves leased Worker pattern | Generation claim, heartbeat, recovery, cancel, bounded retry and UNKNOWN reconciliation | Not implemented |
| Worker authority boundary | Existing render Worker uses protected internal API contracts | Claim, heartbeat, transition and multipart artifact-intake APIs; Worker never writes DB directly | Not implemented |
| Attempt and state audit | Local adapter preserves attempts in browser snapshot | Dedicated immutable attempt rows and append-only sanitized events | Not implemented |
| Safe generated artifact intake | Upload path already proves quota/probe/hash/AssetVersion | Trusted Sidecar source plus idempotent generated-artifact intake | Not implemented |
| Rights handoff | RightsSnapshot and QuickCreate rights preflight exist | Server-created asset begins `RIGHTS_MISSING`; downstream rechecks current rights | Not implemented for generation |
| Adoption/timeline protection | IM9–IM11 references are `adopted=false` | Preserve explicit Adoption and prohibit automatic timeline/render side effects | Required invariant |
| Real provider/API key | Pinned Adapter contract exists | Activation remains outside this batch | Prohibited |

## Alignment conclusion

The next smallest functional gap is not another generation UI or a new AI provider. It is the missing durable bridge between the accepted local workflow and existing server-side project, Worker, media and rights controls.

IM12–IM14 intentionally reuses current repository components. It permits a future additive generation-task model/API/Worker implementation only after separate coding approval, while keeping all tests on a fake Adapter and keeping runtime provider mode disabled.

## Source-of-truth rule

Until this documentation package is formally reviewed and merged, and the owner later authorizes coding against an exact `main` SHA, all IM12–IM14 rows remain `NOT_IMPLEMENTED`. Documentation completion must never be presented as provider connectivity or usable production AI generation.
