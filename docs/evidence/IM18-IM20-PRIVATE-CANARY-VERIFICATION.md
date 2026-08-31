# IM18–IM20 governed private Provider canary verification

Status: implementation evidence for the fake-only coding slice.

## Baseline and boundaries

- Coding baseline: `main@f046fab83fafd79efad5e4f49801e7514527c032`.
- Pinned MoneyPrinterTurbo upstream remains `v1.2.7 / 475f21147f0808f5ffe3f58af9ab794b28a4da2c`.
- No real Provider/model/material-source credential was read, mounted or contacted during implementation or CI.
- No paid request, real arm/run, target deployment or public access is authorized by this evidence.
- Base Compose and committed environment templates remain `AETHER_GENERATION_PROVIDER_MODE=disabled`.

## Implemented repository controls

- Target-local MoneyPrinter config is accepted only by the explicit provider-canary override and is bind-mounted read-only to `/MoneyPrinterTurbo/config.toml`.
- The preflight rejects repository-local, relative, symlink, non-regular, over-permissive and malformed config inputs, and emits only sanitized readiness state.
- MoneyPrinter Sidecar is removed from `aether-net`, shares an internal `provider-control` network only with Worker, and is the sole service on `provider-egress`.
- API, Web and video-use are denied direct Sidecar connectivity; Worker retains fixed-service-name access.
- Future real canary commands require an exact approved Git SHA, a clean worktree, explicit owner approval and a second `AETHER_PROVIDER_CANARY_REAL_EXECUTION_APPROVED=YES` gate.
- The canary budget is one task, one output and 1-10 seconds, with `/tasks/` as the only published artifact prefix.
- Error/interrupt/explicit disarm follows fail-closed cleanup and returns the stack to the committed disabled mode.
- CI uses only a disposable fake TOML and public fake policy/evidence inputs.

## 40 mandatory acceptance cases

The fake-only acceptance runner `infra/docker/provider-canary-smoke.py self-test --repo-root <checkout>` returns exactly 40 sequential checks. The suite covers the approval package IM18 cases 1-14, IM19 cases 15-26 and IM20 cases 27-40, including secret boundaries, network topology, fixed artifact policy, deterministic shutdown and preservation of the existing full-stack regression suite.

Expected result:

```text
{"mode":"fake-only","passed":40,"total":40}
```

## Full regression expectation

GitHub CI must continue to pass:

- Node lint/build/unit tests;
- API generation and legacy-route regressions;
- Worker generation/Adapter tests;
- video-use tests;
- Playwright workbench flow;
- Docker Compose configuration and isolated Provider-network assertions;
- FFmpeg, authenticated queue/render, canonical gap render and production browser upload-to-download flow.

The canary override is config-validated with a temporary fake TOML only. CI does not run `provider-canary.sh arm` or `provider-canary.sh run` and does not inject real Provider secrets.
