# IM18–IM20 Roadmap

## Planning baseline

`main@221540aa2fcb64df4012aa37f8bd017da8e29a9c`

## Evidence-based sequence

1. **IM18 — target-local secret configuration boundary**: documentation proposed; implementation not started.
2. **IM19 — Provider network and interface isolation**: documentation proposed; implementation not started.
3. **IM20 — private one-task canary and deterministic shutdown**: documentation proposed; implementation not started.

## Why this is next

IM15–IM17 established the deny-by-default activation control plane, restricted Worker Adapter, quota ledger, circuit breaker and emergency stop. The exact pinned MoneyPrinterTurbo upstream still requires a root `config.toml` for LLM/material credentials, while Aether currently provides no target-local read-only mount. The pinned upstream API is unauthenticated and currently shares the general application network. Those are operational blockers before any real Provider credential or paid request can be considered.

The smallest safe next slice is therefore not real activation. It is an independently testable private-canary path: keep the base stack disabled, mount a target-local secret file only through an explicit override, isolate the Sidecar network, constrain a future authorized canary to one request/one output/at most ten generated seconds, and guarantee fail-closed shutdown with sanitized evidence.

## Gate status

- IM15–IM17 repository implementation and documentation: accepted.
- IM18–IM20 documentation drafting: in progress in a documentation-only PR.
- IM18–IM20 documentation formal review: not approved.
- IM18–IM20 coding: not approved and not started.
- Real Provider/model/material-source selection: not approved.
- Credentials, paid call and private target execution: prohibited.
- Deployment, public access and commercial operation: prohibited.

## Invariants

- Source, base Compose, environment templates and CI default to `disabled`.
- Real configuration remains outside the Git worktree and is read-only to the Sidecar only.
- API, Web and video-use cannot connect to the unauthenticated MoneyPrinter Sidecar.
- CI uses deterministic fake config/Sidecar only and has zero public Provider egress.
- MoneyPrinterTurbo remains pinned to `v1.2.7` / `475f21147f0808f5ffe3f58af9ab794b28a4da2c`.
- A future real canary is one task, one output and at most ten generated seconds, then returns to disabled.
- Generated output remains rights-blocked; no automatic adoption, timeline write, render or publish.

## Non-goals

No real Provider/plugin/model choice, real config or key, paid call, upstream upgrade, new dependency, external queue/object storage, public Sidecar endpoint, digital human, voice clone, face/person replacement, wardrobe/background transformation, automatic rights approval/adoption/timeline/render/publish, deployment, public access or commercial operation.
