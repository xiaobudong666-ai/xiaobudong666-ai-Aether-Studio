# IM12–IM14 PRD—Code Alignment

| Requirement | Merged implementation evidence | Coding status |
|---|---|---|
| IM12 project-scoped capability and preflight | Protected MoneyPrinter capability snapshot plus project validate endpoint | Implemented in authorized server-bridge scope |
| IM12 durable task API | Authenticated create/list/detail/cancel/retry routes with tenant, project, RBAC, CSRF and idempotency checks | Implemented and tested |
| IM13 canonical persistence | Additive `DBGenerationTask`, `DBGenerationAttempt` and append-only `DBGenerationEvent` records | Implemented and tested |
| IM13 Worker ownership | Internal claim/heartbeat/transition APIs require Worker token and current lease | Implemented and tested |
| IM13 cancel/retry/recovery | Persisted cancel intent, bounded retry, expired-lease recovery, immutable attempt history and ambiguous-response isolation | Implemented and tested |
| IM14 trusted artifact intake | Lease-protected multipart byte stream; URL/path/JSON references rejected; quota, probe and SHA-256 checks enforced | Implemented and tested |
| IM14 governed materialization | Exactly one project Material and immutable AssetVersion with generation provenance and idempotent completion | Implemented and tested |
| Rights handoff | New output stores `RIGHTS_BLOCKED`; usability is derived only from an existing allowed RightsSnapshot | Mandatory and tested |
| Frontend server authority | `GenerationPanel` consumes server task/attempt/result state and isolates project-switch late responses | Implemented and tested |
| Editor handoff | Rights-allowed output creates only an `adopted=false` governed reference | Implemented; no automatic final timeline |
| Runtime Provider | Defaults to `disabled`; tests inject only `deterministic-fake` | Real activation not authorized |
| MoneyPrinter Adapter/upstream | No Adapter or pinned-upstream change in PR #21 | Unchanged by design |
| Automatic adoption/timeline/render/publish | Explicitly excluded from PR #21 | Not implemented by design |
| Deployment/public access | No deployment or public-access change in PR #21 | Not authorized |

## Accepted implementation

PR #21 implemented the approved IM12–IM14 functional slice on top of `main@b9852257076ccad2ac8aed8b1e04cefab5e0d901`. The formally reviewed head `3b900e4909566dcced9cd10b870d64df38724ee0` was squash-merged as `d6c39593cf25856f4b411cbe909d8fa9b54403c0`.

The accepted implementation is limited to the existing repository architecture, deterministic fake Adapter tests and the governed server bridge. It does not claim real Provider connectivity, production AI generation, deployment or commercial operation.

## Verification alignment

- Mandatory IM12–IM14 acceptance cases: 40/40 passed.
- API full regression: 63/63 passed.
- Worker generation tests: 8/8 passed; Worker full regression: 31/31 passed.
- Web full regression: 56/56 passed; contracts/editor: 15/15 passed.
- TypeScript, ESLint with zero warnings, production build, Ruff checks and patch checks: passed.
- CI Pipeline #114 passed lint/build/unit, Playwright workbench and Docker Compose integration.
- Docker integration passed healthy-stack, same-origin, FFmpeg/Worker/video-use, real-render, persistent-queue and production-browser upload-to-download checks.
- Formal-review blockers: 0.
- Changed scope: 13 files, all within approval-package section 11.

## Remaining activation gate

Any real provider/plugin/model/API key, paid call, MoneyPrinter Adapter or upstream-pin change, new dependency, external queue/object storage, automatic rights approval/adoption/timeline/render/publish, deployment or public-access change requires separate owner authorization and new executable evidence.
