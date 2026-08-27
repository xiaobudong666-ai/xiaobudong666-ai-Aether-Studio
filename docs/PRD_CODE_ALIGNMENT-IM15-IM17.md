# IM15–IM17 PRD—Code Alignment

| Requirement | Merged implementation evidence | Repository status |
|---|---|---|
| Governed server generation | Existing project API, durable tasks, Worker leases and trusted intake remain the only task/data authority | Preserved and regression-tested |
| Runtime Provider gate | Exact operator mode, published owner policy and fresh matching Worker attestation are all required | Implemented and tested; default disabled |
| Legacy Provider bypass | `/moneyprinter/health`, `/capabilities`, `/generate` and `/status` return stable 410; API runtime has no Adapter call path | Retired and tested |
| Configuration provenance | Immutable config versions, supersedes chain, policy hash and task claim binding | Implemented and tested |
| Secret boundary | Policy rejects secret/address-shaped fields; readiness, DTOs, events and errors are sanitized | Implemented and tested |
| MoneyPrinter contract | Allowlisted submit payload, normalized status/error/cancel capability, fixed Adapter/upstream identity | Implemented and tested |
| Restricted artifact stream | Same-origin path/origin allowlist, traversal/query rejection, no redirects/proxy inheritance, bounded MP4 stream | Implemented and tested |
| Ambiguous submission/recovery | POST ambiguity becomes `UNKNOWN`; saved upstream ID is queried without repost | Preserved and tested |
| Generation quota | Atomic task reservation plus idempotent release/settlement for concurrency, monthly requests and generated seconds | Implemented and tested |
| Failure containment | Persistent CLOSED/OPEN/HALF_OPEN circuit with one half-open probe and atomic transitions | Implemented and tested |
| Emergency stop | Owner-only audited stop/recovery blocks validate/create/claim without deleting evidence | Implemented and tested |
| Worker governance rejection | Cancellation, lease loss and emergency stop keep stable API codes and do not overwrite server state | Implemented by FR24-01 and tested |
| Frontend authority | GenerationPanel consumes server readiness, quota, circuit, stop and rights state; late project responses are isolated | Implemented and tested |
| Rights and adoption | Generated AssetVersion remains rights-blocked; editor reference remains `adopted=false` | Preserved invariant |
| Runtime defaults and CI | Source, Compose and environment templates default to `disabled`; CI uses deterministic fake behavior | Implemented and tested |
| Dependencies/upstream/infrastructure | No dependency or lockfile, MoneyPrinter pin/Dockerfile, queue or external-object-storage change | Preserved boundary |
| Real Provider/API key/paid use | No real connectivity, credential or paid request supplied or activated | Not authorized by design |
| Automatic timeline/render/publish | No automatic rights approval, adoption, timeline write, render or publish | Not implemented by design |
| Production launch | No target deployment or public-access change | Separate external gate |

## Accepted implementation

PR #24 implemented the owner-approved IM15–IM17 activation-readiness slice on top of `main@16d987d4265e4fa4aea346f493277b7869585d55`. The formally reviewed head `54dbbd676426b08325f826a83fde26cbecd66659` was squash-merged as `ad7e505d6d131d12e2c18c5c255a6ae034b62fbd`.

The implementation establishes a deny-by-default control plane around the existing governed generation bridge. It makes future activation auditable and bounded, but does not itself activate a Provider or claim production AI generation.

## Verification alignment

- Mandatory IM15–IM17 acceptance cases: 48/48 passed.
- API full regression: 99/99; Worker full regression: 48/48; Web: 56/56.
- contracts: 11/11; editor: 4/4; video-use: 3/3.
- TypeScript, ESLint with zero warnings, production build, Python compile and patch checks: passed.
- CI Pipeline #126 passed lint/build/unit, dependency audits, Playwright and Docker Compose integration.
- Docker integration passed healthy-stack, same-origin, Worker/video-use, FFmpeg, fixed-upstream, real-render, persistent-queue and production-browser upload-to-download checks.
- Formal review fixed `FR24-01`; final blockers: 0; unresolved review threads: 0.
- Changed scope: 18 files, all within the owner-approved allowlist including the separately approved CI workflow extension.

## Remaining external gates

Real Provider/plugin/model activation, credentials or API keys, paid use, production configuration, security/load evidence, deployment, public access and commercial operation require separate owner authorization and target-environment evidence.
