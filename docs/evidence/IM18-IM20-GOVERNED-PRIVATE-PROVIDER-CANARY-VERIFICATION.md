# IM18–IM20 Governed Private Provider Canary Verification

## Scope and accepted baselines

- Current source-of-truth baseline: `main@9cc0f7e18ab10a4e7f31fd5a94b97aaf4633e441`.
- Coding baseline: `main@f046fab83fafd79efad5e4f49801e7514527c032`.
- Functional PR: #27.
- Formally reviewed head: `7d7d6ef3c10b64e76934c1dae58bb1e32c3523ac`.
- Accepted implementation merge: `0d7275836abfef26db7180076b23529b4f974f26`.
- Fixed upstream remains MoneyPrinterTurbo `v1.2.7` at `475f21147f0808f5ffe3f58af9ab794b28a4da2c`.
- Documentation closure PR #28 remains Draft and requires independent review.
- Current real-canary decision: `NO-GO`.

## Implemented controls

- Target-local Provider configuration is accepted only through the explicit private-canary override and a separately authorized read-only mount.
- Preflight logic rejects repository-local, relative, symlink, non-regular, over-permissive and malformed config inputs and emits only sanitized readiness state.
- MoneyPrinter Sidecar is absent from `aether-net`, shares internal `provider-control` only with Worker, and is the sole service on `provider-egress`.
- API, Web and video-use are denied direct Sidecar connectivity; Worker retains fixed-service-name access.
- API/Worker readiness requires sanitized `credentialState`, `networkIsolation` and fixed `canaryProfile` proof in addition to operator, owner, attestation, quota, circuit and kill-switch gates.
- The canary contract constrains any future separately approved run to one task, one output and 1–10 generated seconds with the exact `/tasks/` artifact prefix.
- Failures, interrupts and explicit disarm follow fail-closed cleanup and return the runtime to the committed disabled default.
- CI uses disposable fake inputs only and never executes real `preflight`, `arm/run` or injects real Provider secrets.

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
| FFmpeg / authenticated queue / local render / production browser flow | PASS |

## Verification and governance history

| Event | Result |
|---|---|
| PR #27 / Run `33440891212` | Final reviewed implementation head passed 40/40 acceptance, regressions, Playwright and Docker Compose integration; merged as `0d7275836abfef26db7180076b23529b4f974f26` |
| PR #29 | Candidate runtime configuration locked; execution explicitly not authorized; merged as `e43c71166a6e525cad23c47dfd5f30a980d04625` |
| PR #30 | Evidence checklist, read-only preflight runbook and pre-execution acceptance form merged as `7087d99298b27c3b133467caa101aa0a19d44e88`; decision `NO-GO` |
| PR #31 | Redacted EV-01–EV-17 register merged as `845ab5d56757b20396099f6d6dea03ef11d833fa`; independent review pending |
| PR #32 | Governance baselines aligned and remaining-evidence remediation guide merged as `6baae165d069d64f1c9489c40b411b83f27cbbb7`; evidence unchanged |
| PR #33 | Phase A EV-14–EV-16 records prepared but kept `ABSENT`; MoneyPrinter Docker base pinned to `python:3.11.14-slim-bookworm` without changing the fixed upstream commit; merged as `dd6fc186adc84388c9a0d1aa0dd592ad2521fc2d` |
| PR #33 post-merge Run [`34508384975`](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/34508384975) | 3/3 jobs passed: lint/build/unit/fake-only checks, Playwright and Docker Compose integration |
| PR #34 | One-person OPC proposed-role and Stage B/C read-only human-evidence checklist merged as `9cc0f7e18ab10a4e7f31fd5a94b97aaf4633e441`; roles not appointed; evidence unchanged; `NO-GO` |
| Latest post-merge Run [`34512370814`](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/34512370814) | At `main@9cc0f7e18ab10a4e7f31fd5a94b97aaf4633e441`, 3/3 jobs passed: lint/build/unit/fake-only checks, Playwright and Docker Compose integration |

## Current redacted evidence state

| Status | Count |
|---|---:|
| `PRESENT` | 5 |
| `ABSENT` | 3 |
| `INVALID` | 0 |
| `NOT_CHECKED` | 9 |

Independent review is `NOT_REVIEWED`. Read-only preflight is `NOT_AUTHORIZED / NOT_EXECUTED`. Phase A preparation did not elevate EV-14, EV-15 or EV-16 from `ABSENT`.

## Current conclusion and remaining gates

The IM18–IM20 repository controls remain accepted on the current baseline, and latest post-merge fake-only CI Run `34512370814` is green. PR #34 records only proposed roles and a Stage B/C read-only checklist; it does not appoint roles or elevate evidence. The Docker base-image pin does not constitute target, credential, Provider, monetary-limit or real-request validation. The real private canary therefore remains `NO-GO`.

The next step is independent review of this refreshed closure and the redacted evidence register. Jackie remains proposed—not appointed—as `AUTHORIZED_OPERATOR` and `EMERGENCY_STOP_OWNER`, while `INDEPENDENT_REVIEWER` remains `UNASSIGNED`. Any unresolved evidence must be collected by authorized humans in controlled environments under the existing redaction rules. Real target access, Provider-console access, credential handling, read-only `preflight`, paid use, `arm/run`, deployment and public access remain separate, unapproved gates.
