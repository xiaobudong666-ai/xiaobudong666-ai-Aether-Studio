# IM18–IM20 Governed Private Provider Canary Verification

## Scope and accepted baseline

- Coding baseline: `main@f046fab83fafd79efad5e4f49801e7514527c032`.
- Functional PR: #27.
- Formally reviewed head: `7d7d6ef3c10b64e76934c1dae58bb1e32c3523ac`.
- Accepted merge commit: `0d7275836abfef26db7180076b23529b4f974f26`.
- Fixed upstream remains MoneyPrinterTurbo `v1.2.7` at `475f21147f0808f5ffe3f58af9ab794b28a4da2c`.
- No real Provider/model/material-source credential was read, mounted or contacted during implementation, review or CI.
- No paid request, real `arm`/`run`, target deployment, public access or commercial operation was authorized or executed.
- Subsequent PR #29 locked one real-canary candidate configuration and merged as `e43c71166a6e525cad23c47dfd5f30a980d04625`; its status is `CONFIGURATION_LOCKED / EXECUTION_NOT_AUTHORIZED`.

## Implemented controls

- Target-local Provider configuration is accepted only through the explicit private-canary override and is mounted read-only to `/MoneyPrinterTurbo/config.toml`.
- Preflight rejects repository-local, relative, symlink, non-regular, over-permissive and malformed config inputs and emits only sanitized readiness state.
- MoneyPrinter Sidecar is removed from `aether-net`, shares an internal `provider-control` network only with Worker, and is the sole service on `provider-egress`.
- API, Web and video-use are denied direct Sidecar connectivity; Worker retains fixed-service-name access.
- API/Worker readiness requires sanitized `credentialState`, `networkIsolation` and fixed `canaryProfile` proof in addition to operator, owner, attestation, quota, circuit and kill-switch gates.
- The canary profile constrains a future separately approved run to one task, one output and 1–10 generated seconds with the exact `/tasks/` artifact prefix.
- Failures, interrupts and explicit disarm follow fail-closed cleanup and return the runtime to the committed disabled default.
- CI uses disposable fake TOML only and never executes real `arm`/`run` or injects real Provider secrets.

## Mandatory acceptance matrix

| # | Evidence | Status |
|---:|---|---|
| 1 | Base environment templates and Compose default to `disabled`; fake-only self-test makes zero Provider request | PASS |
| 2 | Required bind variable and missing-input rejections in override/preflight | PASS |
| 3 | Absolute, external, regular-file and non-symlink checks | PASS |
| 4 | Owner/readability and `0600` checks | PASS |
| 5 | TOML errors return only stable reason codes | PASS |
| 6 | Override declares fixed read-only bind; container write assertion | PASS |
| 7 | Override contains no secret environment, command, label, healthcheck, build arg or image change | PASS |
| 8 | Compose visibility is Sidecar-only; API/Worker DTOs contain only sanitized proof | PASS |
| 9 | Evidence scanner rejects path, metadata, digest and secret-shaped fields | PASS |
| 10 | `PRESENT` requires valid structural proof; invalid states fail closed | PASS |
| 11 | WARNING-or-stricter and auto-upload disabled checks | PASS |
| 12 | Provider/source/proxy/base URL/endpoint/material/concurrency/hide-config rejection set | PASS |
| 13 | API/Worker require exact config/policy/profile and `/tasks/` contract | PASS |
| 14 | `provider-canary-smoke.py self-test` uses temporary fake inputs only | PASS |
| 15 | Sidecar absent from `aether-net` and has no host port | PASS |
| 16 | `provider-control` is internal and limited to Worker/Sidecar | PASS |
| 17 | `provider-egress` is Sidecar-only | PASS |
| 18 | API cannot resolve/connect to Sidecar | PASS |
| 19 | Web and video-use cannot resolve/connect to Sidecar | PASS |
| 20 | Worker reaches Sidecar contract through control network | PASS |
| 21 | Adapter retains `trust_env=False` and redirect refusal | PASS |
| 22 | Four legacy `/moneyprinter/*` routes remain stable 410 | PASS |
| 23 | No Nginx, diagnostic proxy, host port or second Provider change | PASS |
| 24 | Adapter/log tests and evidence scanner reject sensitive bodies/headers/prompts | PASS |
| 25 | Invalid credential/network/profile proof prevents Provider readiness/claim | PASS |
| 26 | Base health/render network regression | PASS |
| 27 | Controller defaults to preflight; `arm/run` require explicit command and exact SHA | PASS |
| 28 | Dirty/SHA/owner/private-target/approval controls fail closed | PASS |
| 29 | `arm` checks owner kill switch is disabled before recovery | PASS |
| 30 | API and Worker require concurrency 1, request 1, seconds 1–10 and output 1 | PASS |
| 31 | Budget and material-license evidence flags are mandatory | PASS |
| 32 | Request validator allows one project, synthetic subject and unique UUID key only | PASS |
| 33 | One reservation/one POST; ambiguous submission remains UNKNOWN without replay | PASS |
| 34 | Exact same-origin `/tasks/` artifact policy and existing idempotent intake/settlement | PASS |
| 35 | Existing release/UNKNOWN rules preserved and fail closed | PASS |
| 36 | Existing intake remains rights-blocked and does not auto-adopt/render/publish | PASS |
| 37 | ERR/INT/TERM trap and timeout path invoke disarm | PASS |
| 38 | Disarm sets kill switch, disables Worker and removes Sidecar container/mount | PASS |
| 39 | State/evidence allowlist scanner rejects URL, path, prompt and secret shapes | PASS |
| 40 | Full API/Worker/Web/contracts/editor/video-use plus Docker/Playwright/render/browser regression | PASS |

## Verification totals

| Gate | Result |
|---|---:|
| Mandatory fake-only acceptance | `40/40 PASS` |
| API full regression | `109 passed` |
| Worker full regression | `53 passed` |
| video-use regression | `3 passed` |
| contracts | `11 passed` |
| editor | `4 passed` |
| Web | `56 passed` |
| Node lint / TypeScript / production build | PASS |
| Shell syntax / Python compile / whitespace / secret-shape scan | PASS |
| Playwright workbench flow | PASS |
| Docker Compose network/read-only-mount integration | PASS |
| FFmpeg / authenticated queue / real local render / production browser flow | PASS |

## Remote verification history

- Run `33438948517`: failed before API execution because a connector upload corrupted `apps/api/app/main.py`; the file was restored from the locally verified source before review.
- Run `33440230599`: passed the Draft feature branch verification.
- Final reviewed-head run `33440891212`: PASS on `7d7d6ef3c10b64e76934c1dae58bb1e32c3523ac`, including lint/build/audits/unit/fake-only tests, Playwright, Docker network isolation, read-only mount, Worker-only Sidecar access, FFmpeg/render, authenticated queue and browser flow.

## Accepted repository state and subsequent candidate lock

PR #27 has been formally reviewed and merged as `main@0d7275836abfef26db7180076b23529b4f974f26`. IM18–IM20 repository implementation is therefore accepted.

PR #29 later locked one candidate runtime configuration in `docs/approvals/REAL-PROVIDER-PRIVATE-CANARY-EXECUTION-APPROVAL.md` and merged as `e43c71166a6e525cad23c47dfd5f30a980d04625`. That record explicitly remains `CONFIGURATION_LOCKED / EXECUTION_NOT_AUTHORIZED`; it does not alter the 40-case repository acceptance result and does not authorize any real execution.

The following remain separate and are **not** authorized by this acceptance or the candidate lock:

- target-local real credential mounting or inspection;
- target access or real preflight;
- Provider-side monetary-limit verification for execution and any paid call;
- real private-canary `arm`/`run` execution;
- deployment, public access, expanded trial or commercial operation.
