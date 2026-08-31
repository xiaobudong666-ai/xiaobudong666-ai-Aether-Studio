# IM18–IM20 Governed Private Provider Canary Verification

## Scope and baseline

- Authorized baseline: `main@f046fab83fafd79efad5e4f49801e7514527c032`.
- Implementation branch: `feat/im18-im20-governed-private-canary`.
- Fixed upstream remains MoneyPrinterTurbo `v1.2.7` at
  `475f21147f0808f5ffe3f58af9ab794b28a4da2c`.
- All canary preflight checks used disposable fake TOML only. No real Provider,
  credential, target configuration, paid request, `arm`, `run`, deployment or
  public access was used.

## Implemented controls

- The target configuration is accepted only through the explicit canary
  override and is mounted read-only to the Sidecar's fixed configuration path.
- The Sidecar is removed from `aether-net`; Worker-to-Sidecar traffic uses the
  internal `provider-control` network, while only the Sidecar has
  `provider-egress`.
- Preflight rejects a mismatched/dirty checkout, unsafe target file, unsafe
  TOML, broad policy, missing budget/license evidence and non-synthetic input.
- API and Worker require the sanitized `credentialState`, `networkIsolation`
  and fixed `canaryProfile` proof in addition to the existing operator, owner,
  attestation, quota, circuit and kill-switch gates.
- A future separately approved run records the one-POST boundary before
  transmission, never replays an ambiguous submit, and fails closed through
  disarm. CI never executes `arm` or `run`.

## Mandatory acceptance matrix

`PASS_LOCAL` is repository/unit/static evidence. `PENDING_REMOTE_CI` requires
the existing Linux Docker or Playwright job and must become PASS before a Draft
feature PR is opened.

| # | Evidence | Status |
|---:|---|---|
| 1 | Base environment templates and Compose default to `disabled`; fake-only self-test makes zero request | PASS_LOCAL |
| 2 | Required bind variable and missing-input rejections in override/preflight | PASS_LOCAL |
| 3 | Absolute, external, regular-file, non-symlink checks | PASS_LOCAL |
| 4 | Owner/readability and `0600` checks | PASS_LOCAL |
| 5 | TOML errors return only stable reason codes | PASS_LOCAL |
| 6 | Override declares fixed read-only bind; container write assertion | PENDING_REMOTE_CI |
| 7 | Override contains no secret environment, command, label, healthcheck, build arg or image change | PASS_LOCAL |
| 8 | Compose visibility is Sidecar-only; API/Worker DTOs contain only sanitized proof | PENDING_REMOTE_CI |
| 9 | Evidence scanner rejects path, metadata, digest and secret-shaped fields | PASS_LOCAL |
| 10 | `PRESENT` requires valid structural proof; invalid states fail closed | PASS_LOCAL |
| 11 | WARNING-or-stricter and auto-upload disabled checks | PASS_LOCAL |
| 12 | Provider/source/proxy/base URL/endpoint/material/concurrency/hide-config rejection set | PASS_LOCAL |
| 13 | API/Worker require exact config/policy/profile and `/tasks/` contract | PASS_LOCAL |
| 14 | `provider-canary-smoke.py self-test` uses temporary fake inputs only | PASS_LOCAL |
| 15 | Sidecar absent from `aether-net` and has no host port | PENDING_REMOTE_CI |
| 16 | `provider-control` is internal and limited to Worker/Sidecar | PENDING_REMOTE_CI |
| 17 | `provider-egress` is Sidecar-only | PENDING_REMOTE_CI |
| 18 | API cannot resolve/connect to Sidecar | PENDING_REMOTE_CI |
| 19 | Web and video-use cannot resolve/connect to Sidecar | PENDING_REMOTE_CI |
| 20 | Worker reaches deterministic Sidecar contract through control network | PENDING_REMOTE_CI |
| 21 | Adapter retains `trust_env=False` and redirect refusal | PASS_LOCAL |
| 22 | Four legacy `/moneyprinter/*` routes remain stable 410 | PASS_LOCAL |
| 23 | No Nginx, diagnostic proxy, host port or second Provider change | PASS_LOCAL |
| 24 | Adapter/log tests and evidence scanner reject sensitive bodies/headers/prompts | PASS_LOCAL |
| 25 | Invalid credential/network/profile proof prevents Adapter construction/calls/claim | PASS_LOCAL |
| 26 | Base health/render network regression | PENDING_REMOTE_CI |
| 27 | Controller defaults to preflight; `arm/run` require explicit command and exact SHA | PASS_LOCAL |
| 28 | Dirty/SHA/owner/private-target/approval controls fail closed | PASS_LOCAL |
| 29 | `arm` checks owner kill switch is disabled before recovery | PASS_LOCAL |
| 30 | API and Worker require concurrency 1, request 1, seconds 1–10 and output 1 | PASS_LOCAL |
| 31 | Budget and material-license evidence flags are mandatory | PASS_LOCAL |
| 32 | Request validator allows one project, synthetic subject and unique UUID key only | PASS_LOCAL |
| 33 | One reservation/one POST; ambiguous submission remains UNKNOWN without replay | PASS_LOCAL |
| 34 | Exact same-origin `/tasks/` artifact policy and existing idempotent intake/settlement | PASS_LOCAL |
| 35 | Existing release/UNKNOWN rules preserved and fail closed | PASS_LOCAL |
| 36 | Existing intake remains rights-blocked and does not auto-adopt/render/publish | PASS_LOCAL |
| 37 | ERR/INT/TERM trap and timeout path invoke disarm | PASS_LOCAL |
| 38 | Disarm sets kill switch, disables Worker, removes Sidecar container/mount | PENDING_REMOTE_CI |
| 39 | State/evidence allowlist scanner rejects URL, path, prompt and secret shapes | PASS_LOCAL |
| 40 | Full API/Worker/Web/contracts/editor/video-use plus Docker/Playwright/render/browser regression | PENDING_REMOTE_CI |

## Local verification

| Gate | Result |
|---|---:|
| Fake-only private-canary acceptance harness | `40/40 PASS` |
| API full regression | `109 passed` |
| Worker full regression | `53 passed` |
| video-use regression | `3 passed` |
| contracts | `11 passed` |
| editor | `4 passed` |
| Web | `56 passed` |
| Node lint | PASS |
| TypeScript/Vite production build | PASS |
| Shell syntax, Python compile, patch whitespace and secret-shape scan | PASS |
| Local Docker | unavailable in the execution environment; remote CI required |
| Local Playwright | Chromium download timed out; remote CI required |

## Remaining gate

Push the feature branch and require the branch CI to pass build/test/audits,
Playwright and Docker integration. The Docker job must prove the read-only bind,
network membership, API/Web/video-use denial, Worker-only Sidecar access,
FFmpeg, pinned video-use rendering, authenticated queue flow and production
browser flow. Only after those results are recorded may a Draft feature PR be
created. Formal review, merge, Provider/model selection, credentials, paid use,
private canary execution and deployment remain separate owner gates.
