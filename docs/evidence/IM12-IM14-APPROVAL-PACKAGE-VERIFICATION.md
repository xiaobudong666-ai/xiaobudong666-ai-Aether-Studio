# IM12–IM14 Approval Package Verification

## Baseline

- Authoritative repository baseline: `main@a41bdf194d92158abb49f83c45189c52b1e9ebd1`.
- Predecessor: accepted IM9–IM11 frontend/local implementation and closure.

## Read-only gap evidence

- `apps/web/src/generation.ts` is a deterministic local adapter with browser snapshot persistence.
- `apps/api/app/main.py` exposes synchronous `/moneyprinter/generate` and status demonstration routes.
- `DBExternalTask` lacks project, request snapshot, idempotency, progress, lease, error and artifact fields.
- `process_m1_moneyprinter_task` is not connected to the persistent Worker task-claim loop.
- Existing media upload, RenderTask leases, AssetVersion, RightsSnapshot and QuickCreate preflight provide reusable controls.
- README still states AI generation/subtitle methods are mocks and production generation needs separately configured credentials.

## Package self-check

- [x] IM12–IM14 each have a bounded responsibility.
- [x] API, fields, state transitions, permissions, data model and module interactions are defined.
- [x] Idempotency, leases, retry, cancellation, ambiguous response and restart recovery are defined.
- [x] Internal Worker claim, heartbeat, transition and multipart artifact-intake contracts are defined.
- [x] Worker-to-API authority is explicit; Worker direct database writes are prohibited.
- [x] Immutable attempt records and append-only sanitized task events are defined.
- [x] Trusted source, streaming limit, quota, probe, checksum, transaction and cleanup controls are defined.
- [x] Rights default-deny and no-auto-adoption/timeline/render invariants are defined.
- [x] 40 executable acceptance cases are enumerated.
- [x] Exact allowed coding files and stop conditions are enumerated.
- [x] Real provider activation, credentials, paid calls, dependencies and deployment remain prohibited.

## Documentation-only scope

This package adds five documentation files. It does not modify code, dependencies, APIs, migrations, Worker behavior, provider configuration, credentials, deployment or public access.

## Verification limitation

No functional test passage or provider operation is claimed because implementation has not started. Formal review, merge, coding, functional review, activation and deployment remain separate gates.
