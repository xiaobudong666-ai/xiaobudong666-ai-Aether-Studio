# IM18–IM20 PRD—Code Alignment

Current source-of-truth baseline: `main@7916dbdb65d89c576d7f5d04e1355c966a697266`

Current decision: `NO-GO`

| Requirement | Accepted repository evidence | Current status |
|---|---|---|
| Governed generation authority | Project APIs, durable tasks, Worker lease/token, trusted intake and rights blocking remain authoritative | Accepted / reused |
| Activation control plane | Operator mode, published owner policy, matching fresh Worker proof, quota, circuit and kill switch remain mandatory | Accepted / reused |
| Sidecar secret input | Explicit private-canary Compose override binds repository-external `/MoneyPrinterTurbo/config.toml` read-only after preflight | Implemented / accepted; real use unauthorized |
| Secret exposure boundary | Preflight rejects unsafe files/configuration; API/Worker expose sanitized proof only; CI scans fake evidence and never injects real credentials | Implemented / accepted |
| Pinned artifact contract | Fixed upstream remains `v1.2.7` / `475f21147f0808f5ffe3f58af9ab794b28a4da2c`; policy requires exact same-origin `/tasks/` handling | Implemented / accepted |
| Sidecar API exposure | MoneyPrinter Sidecar is absent from `aether-net`; Worker uses internal `provider-control`; Sidecar alone has `provider-egress`; no host port | Implemented / accepted |
| Provider selection | PR #29 locked one candidate profile; PRs #30–#36 did not elevate it to execution authorization | Configuration locked / execution unauthorized |
| Cost containment | Repository profile enforces one task, one output, request limit 1 and 1–10 generated seconds; external Provider cap remains unverified | Repository control accepted / external evidence incomplete |
| Canary orchestration | Preflight-first `arm/run/disarm`, exact-SHA/owner/private-target gates and fail-closed cleanup exist | Implemented / no real invocation authorized |
| Evidence | PRs #30–#36 added the checklist, runbook, acceptance form, redacted register, remediation guide, Phase A records, the proposed-role/Stage B–C checklist, the formal role-appointment record and the EV-16 redacted independent-review handoff | 5/17 `PRESENT`; 3 `ABSENT`; 9 `NOT_CHECKED`; independent review pending |
| CI reproducibility | PR #33 pinned `python:3.11.14-slim-bookworm` for the MoneyPrinter build without changing the fixed upstream commit | Fake-only CI stabilization accepted |
| Real credentials/calls | No real credential, Provider egress, paid call, real preflight or real `arm/run` occurred | Prohibited / not executed |
| Rights/adoption | Generated AssetVersion remains rights-blocked; no automatic adoption/timeline/render/publish path was added | Preserved invariant |
| Deployment/public access | Production-shaped Compose/runbook exists, but no target deployment or public launch occurred | Not authorized |

## Accepted implementation anchors

- Coding baseline: `main@f046fab83fafd79efad5e4f49801e7514527c032`
- Functional PR: #27
- Reviewed head: `7d7d6ef3c10b64e76934c1dae58bb1e32c3523ac`
- Implementation merge: `0d7275836abfef26db7180076b23529b4f974f26`
- Final implementation CI: `33440891212`
- Mandatory acceptance: `40/40 PASS`

## Governance continuation

| PR | Merge commit | Recorded result |
|---:|---|---|
| #29 | `e43c71166a6e525cad23c47dfd5f30a980d04625` | Candidate configuration locked; execution not authorized |
| #30 | `7087d99298b27c3b133467caa101aa0a19d44e88` | Evidence checklist, runbook and acceptance form merged; `NO-GO` |
| #31 | `845ab5d56757b20396099f6d6dea03ef11d833fa` | Redacted EV-01–EV-17 register merged; independent review pending; `NO-GO` |
| #32 | `6baae165d069d64f1c9489c40b411b83f27cbbb7` | Governance baselines aligned and remediation guide added; evidence unchanged; `NO-GO` |
| #33 | `dd6fc186adc84388c9a0d1aa0dd592ad2521fc2d` | Phase A EV-14–EV-16 records prepared but remain `ABSENT`; fake-only Docker base pinned; `NO-GO` |
| #34 | `9cc0f7e18ab10a4e7f31fd5a94b97aaf4633e441` | One-person OPC proposed-role and Stage B/C read-only human-evidence checklist merged; roles not appointed at that stage; evidence unchanged; `NO-GO` |
| #35 | `624e26e14dc3d5bd7f51e83f98248abb3b9e366b` | Jackie appointed as `AUTHORIZED_OPERATOR` and `EMERGENCY_STOP_OWNER`; `REVIEWER-01` appointed as `INDEPENDENT_REVIEWER / NOT_REVIEWED`; evidence unchanged; `NO-GO` |
| #36 | `7916dbdb65d89c576d7f5d04e1355c966a697266` | EV-16 redacted independent-review handoff prepared; material not delivered; no `ACCEPTED/REJECTED` result; evidence unchanged; `NO-GO` |

PR #33 post-merge CI Run [`34508384975`](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/34508384975) passed all three jobs: lint/build/unit/fake-only checks, Playwright and Docker Compose integration.

PR #34 post-merge CI Run [`34512370814`](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/34512370814) passed the same 3/3 jobs at `main@9cc0f7e18ab10a4e7f31fd5a94b97aaf4633e441`.

## Alignment conclusion

IM18–IM20 closed the repository-level blockers for a bounded private-canary path. The later governance chain documented a single candidate and prepared progressively more precise evidence controls without granting execution authority. The current evidence remains insufficient: five of seventeen items are `PRESENT`, three are `ABSENT`, nine are `NOT_CHECKED`, and no independent review has accepted the register.

PR #33's only non-document change pinned the MoneyPrinter Docker base image for reproducible fake-only CI and left the fixed MoneyPrinterTurbo upstream commit unchanged. That change passed post-merge CI but did not validate a private target, credential, Provider account, monetary limit or real request.

PR #34 added a proposed one-person OPC role arrangement and Stage B/C read-only human-evidence checklist. PR #35 subsequently appointed Jackie as `AUTHORIZED_OPERATOR` and `EMERGENCY_STOP_OWNER`, and appointed `REVIEWER-01` as `INDEPENDENT_REVIEWER / NOT_REVIEWED`. PR #36 prepared a redacted EV-16 handoff, but the material has not been delivered and `REVIEWER-01` has not returned `ACCEPTED` or `REJECTED`. These governance steps did not perform independent review, change any evidence status or authorize Stage B/C verification or real execution.

## Source-of-truth and next gate

`main@7916dbdb65d89c576d7f5d04e1355c966a697266` is the sole current repository baseline. IM18–IM20 implementation remains `ACCEPTED`, while the real canary remains `NO-GO`. Jackie and `REVIEWER-01` are formally appointed to their recorded roles, but independent review remains `NOT_REVIEWED` and all evidence counts remain unchanged. The next governance step is separately authorized, owner-controlled delivery of the prepared PR #36 EV-16 handoff to `REVIEWER-01` and receipt of that human reviewer's redacted `ACCEPTED` or `REJECTED` result; preparation alone is not review. Any human collection for unresolved evidence, real read-only `preflight`, credential handling, paid use, `arm/run`, deployment or public access requires separate explicit authorization.
