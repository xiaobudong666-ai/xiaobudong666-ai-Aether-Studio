# IM12–IM14 Roadmap

## Accepted implementation baseline

`main@d6c39593cf25856f4b411cbe909d8fa9b54403c0`

## Sequence

1. **IM12 — project-scoped generation API and capability gate**: **implemented**.
2. **IM13 — leased Worker generation orchestration and recovery**: **implemented**.
3. **IM14 — trusted artifact intake and rights handoff**: **implemented**.

## Closed gates

- Documentation gate: **closed / accepted** before coding through the IM12–IM14 approval-package workflow.
- Coding authorization gate: **closed** by the owner's explicit authorization against `main@b9852257076ccad2ac8aed8b1e04cefab5e0d901`.
- Functional implementation gate: **closed / accepted** by squash merge of PR #21.
- Verification gate for the authorized server-bridge scope: **closed** with 40/40 mandatory acceptance cases, all full regressions and CI Pipeline #114 passing.
- Formal-review blockers: **0** on reviewed head `3b900e4909566dcced9cd10b870d64df38724ee0`.

## Current implementation status

**Implemented within the authorized governed server-bridge scope.** Aether now provides project-scoped generation capability, validation, task creation/list/detail, cancel and retry APIs; additive task/attempt/event persistence; token- and lease-protected Worker operations; deterministic fake-only orchestration; trusted multipart artifact intake; Material plus immutable AssetVersion creation; rights-default blocking; and server-authoritative GenerationPanel recovery.

Generated AssetVersions remain blocked until the existing rights decision explicitly allows use. Editor references remain `adopted=false`; the implementation does not automatically write the final timeline, render or publish.

## Open activation gate

The governed runtime Provider remains **disabled by default**. Real provider/plugin/model/API-key activation, MoneyPrinter Adapter or pinned-upstream changes, paid calls, new dependencies, external queue/object-storage infrastructure, automatic rights approval/adoption/timeline/render/publish, deployment and public access remain separately prohibited until explicitly approved.

## Mainline rationale

IM12–IM14 replaces browser-local task authority with a durable project/API/Worker bridge while preserving the existing media, quota, provenance and rights controls. The accepted implementation proves recovery, idempotency, lease ownership and trusted intake semantics without activating a real generation provider.
