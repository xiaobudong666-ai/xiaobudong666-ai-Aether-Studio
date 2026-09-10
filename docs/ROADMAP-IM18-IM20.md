# IM18–IM20 Roadmap

## Current baseline and decision

- Source-of-truth baseline: `main@7916dbdb65d89c576d7f5d04e1355c966a697266`
- Documentation closure: PR #28 is open for formal review; this does not constitute independent evidence review
- Real private-canary decision: `NO-GO`

## Accepted implementation anchors

- Coding baseline: `main@f046fab83fafd79efad5e4f49801e7514527c032`
- Functional PR: #27
- Formally reviewed head: `7d7d6ef3c10b64e76934c1dae58bb1e32c3523ac`
- Accepted implementation merge: `0d7275836abfef26db7180076b23529b4f974f26`
- Final implementation CI: `33440891212`
- Mandatory acceptance: `40/40 PASS`

## Completed sequence

1. **IM18 — target-local secret configuration boundary**: repository controls implemented and accepted.
2. **IM19 — Provider network and interface isolation**: repository controls implemented and accepted.
3. **IM20 — private one-task canary controls and deterministic shutdown**: repository controls implemented and accepted; real execution remains unauthorized.
4. **PR #29 — candidate lock**: one runtime profile recorded as `CONFIGURATION_LOCKED / EXECUTION_NOT_AUTHORIZED`.
5. **PR #30 — evidence and preflight package**: checklist, runbook and acceptance form merged with `NO-GO`.
6. **PR #31 — redacted evidence register**: EV-01–EV-17 recorded without secrets; independent review remains pending.
7. **PR #32 — baseline alignment**: governance documents aligned and unresolved-evidence guidance merged; evidence was not elevated.
8. **PR #33 — Phase A preparation and CI stabilization**: EV-14–EV-16 records prepared but remain `ABSENT`; the fake-only MoneyPrinter Docker base was pinned reproducibly; real execution remained `NO-GO`.
9. **PR #34 — one-person OPC role draft and Stage B/C checklist**: Jackie recorded only as proposed `AUTHORIZED_OPERATOR` and proposed `EMERGENCY_STOP_OWNER`; `INDEPENDENT_REVIEWER` remained `UNASSIGNED` at that stage; evidence unchanged and `NO-GO` preserved.
10. **PR #35 — formal role appointments**: Jackie appointed as `AUTHORIZED_OPERATOR` and `EMERGENCY_STOP_OWNER`; `REVIEWER-01` appointed as `INDEPENDENT_REVIEWER / NOT_REVIEWED`; evidence unchanged and `NO-GO` preserved.
11. **PR #36 — EV-16 redacted independent-review handoff**: handoff material prepared but not delivered; `REVIEWER-01` has not returned `ACCEPTED` or `REJECTED`; evidence unchanged and `NO-GO` preserved.

## Accepted repository state

- committed/base runtime remains `disabled`;
- target-local Provider configuration is accepted only through an explicit read-only Sidecar mount after a separately authorized preflight;
- secret-safe preflight logic emits only sanitized proof;
- MoneyPrinter Sidecar is isolated from API, Web and video-use; Worker alone shares `provider-control`; Sidecar alone has `provider-egress`;
- the canary contract remains one task, one output and 1–10 generated seconds with exact `/tasks/` artifact handling;
- failure/interrupt/disarm is fail-closed and preserves rights-blocked output with no automatic adoption, timeline write, render or publish;
- MoneyPrinterTurbo remains fixed at `475f21147f0808f5ffe3f58af9ab794b28a4da2c`;
- PR #33 pins the build base to `python:3.11.14-slim-bookworm` for deterministic fake-only CI;
- PR #35 formally appoints Jackie and `REVIEWER-01` to the recorded roles; independent review remains unperformed and evidence remains unchanged;
- PR #36 prepares the redacted EV-16 independent-review handoff only; material delivery and a human `ACCEPTED/REJECTED` result have not occurred;
- PR #33 post-merge CI Run [`34508384975`](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/34508384975) passed 3/3 jobs;
- PR #34 post-merge CI Run [`34512370814`](https://github.com/xiaobudong666-ai/xiaobudong666-ai-Aether-Studio/actions/runs/34512370814) passed 3/3 jobs at `main@9cc0f7e18ab10a4e7f31fd5a94b97aaf4633e441`.

## Current gate status

- IM18–IM20 repository implementation: **accepted**.
- Candidate Provider/model/material-source/voice profile: **locked**.
- Redacted evidence: **5 `PRESENT` / 3 `ABSENT` / 0 `INVALID` / 9 `NOT_CHECKED`**.
- Phase A EV-14–EV-16: **prepared but still `ABSENT`**.
- Formal role appointments: **Jackie is `APPOINTED` as `AUTHORIZED_OPERATOR` and `EMERGENCY_STOP_OWNER`; `REVIEWER-01` is `APPOINTED / NOT_REVIEWED` as `INDEPENDENT_REVIEWER`**.
- EV-16 independent-review handoff: **prepared only; not delivered; no `ACCEPTED/REJECTED` result**.
- Independent evidence review: **`NOT_REVIEWED` / not completed**.
- Real target or Provider-console verification: **not authorized in this stage**.
- Credential reading, validation, mounting or return: **not authorized**.
- Read-only real `preflight`: **not authorized / not executed**.
- Real private-canary `arm/run`: **not authorized / not executed**.
- Paid use, deployment, public access, expanded trial and commercial operation: **not authorized**.
- Overall decision: **`NO-GO`**.

## Next gate

First obtain separate authorization for owner-controlled delivery of the prepared PR #36 EV-16 redacted handoff to `REVIEWER-01`, then receive that human reviewer's redacted `ACCEPTED` or `REJECTED` result. Handoff preparation does not mean delivery or review, and even `ACCEPTED` would not automatically elevate EV-16. Any evidence update requires separate exact authorization. Any attempt to resolve the remaining `ABSENT` or `NOT_CHECKED` evidence must be separately authorized and performed only by the appointed humans in a controlled environment, with only the existing redacted fields recorded. A real read-only `preflight` remains a later, separately approved gate; it cannot be inferred from role appointment, documentation acceptance, fake-only CI success or evidence preparation.
