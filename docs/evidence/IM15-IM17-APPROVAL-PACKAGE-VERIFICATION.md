# IM15–IM17 Approval-Package Verification

## Baseline

- Repository: `xiaobudong666-ai/xiaobudong666-ai-Aether-Studio`
- Audited commit: `main@db2a23bc95e7990f2652b5fe38c625ce232a16de`
- Audit date: 2026-08-27 (UTC)
- Audit type: read-only source and documentation gap audit followed by documentation-only drafting

## Accepted prerequisites

- PR #21 implemented the IM12–IM14 governed server-generation bridge and merged as `d6c39593cf25856f4b411cbe909d8fa9b54403c0`.
- PR #22 closed the IM12–IM14 roadmap, ledger, PRD alignment and verification evidence and merged as `db2a23bc95e7990f2652b5fe38c625ce232a16de`.
- Final functional evidence records 40/40 mandatory acceptance cases, API 63/63, Worker 31/31, Web 56/56 and CI Pipeline #114 passing.
- Runtime Provider remains disabled by default and functional tests inject only deterministic fake behavior.

## Source findings

1. `apps/api/app/generation_tasks.py` enables capability only when mode is exactly `deterministic-fake`; disabled mode returns `PROVIDER_DISABLED`.
2. `apps/worker/app/main.py` rejects any claimed generation task whose `providerMode` is not `deterministic-fake`.
3. API and Worker do not share a published provider configuration version or attested policy hash.
4. The MoneyPrinter Adapter implements health, capability, submit and status behavior, but the accepted source does not implement a restricted `stream_artifact()` method even though the governed Worker path expects a stream.
5. Adapter requests disable environment proxy inheritance with `trust_env=False`, but artifact-origin, redirect and bounded-stream evidence does not yet exist.
6. `.env.example`, `infra/docker/.env.example` and `infra/docker/docker-compose.yml` do not expose a governed real-generation runtime mode; therefore no committed default can currently prove dual-key activation.
7. Tenant quotas cover project count, storage, concurrent rendering and monthly render seconds; generation reservations and settled generated seconds are absent.
8. Existing task retries and leases limit individual work, but there is no persistent Provider circuit breaker or audited owner emergency stop.

## Proposed documentation package

- `docs/approvals/IM15-IM17-PROVIDER-ACTIVATION-READINESS-CODING-APPROVAL.md`
- `docs/ROADMAP-IM15-IM17.md`
- `docs/IMPLEMENTATION_LEDGER-IM15-IM17.json`
- `docs/PRD_CODE_ALIGNMENT-IM15-IM17.md`
- `docs/evidence/IM15-IM17-APPROVAL-PACKAGE-VERIFICATION.md`

## Draft assertions

- The package contains exactly 48 numbered mandatory acceptance cases.
- The proposed code scope is allowlisted; any additional functional file requires renewed owner approval.
- The package authorizes no code by itself.
- Runtime defaults remain disabled in source, Compose and environment templates.
- CI and local tests must use a deterministic fake Sidecar and must not contact a public Provider.
- No credential, paid call, dependency/lockfile change, upstream pin change, external queue/object storage, automatic rights/adoption/timeline/render/publish, deployment or public access is included.

## Required gate sequence

1. Documentation Draft PR validation.
2. Owner approval to enter formal documentation review.
3. Owner approval to merge documentation.
4. Separate exact-SHA coding authorization.
5. Functional Draft PR with all 48 cases and full regression.
6. Separate functional formal-review and merge approvals.
7. Separate real-provider activation, credential/paid-use and deployment approvals.

## Current conclusion

`DOCUMENTATION_DRAFT_ONLY`. IM15–IM17 implementation has not started. No real Provider, plugin, model, key, paid call, deployment or public access is claimed.
