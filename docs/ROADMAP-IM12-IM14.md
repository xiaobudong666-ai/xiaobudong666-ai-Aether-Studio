# IM12–IM14 Roadmap

## Planning baseline

`main@a41bdf194d92158abb49f83c45189c52b1e9ebd1`

## Evidence-based sequence

1. **IM12 — project-scoped generation API and capability gate**: planned, implementation not started.
2. **IM13 — leased Worker generation orchestration and recovery**: planned, implementation not started.
3. **IM14 — trusted artifact intake and rights handoff**: planned, implementation not started.

## Why this is next

IM9–IM11 proved the governed workflow in the existing React frontend with a deterministic fake/local adapter. The remaining smallest path to durable, cross-session generation is to move task authority to the existing API/SQLite/Worker boundary and safely turn generated output into an immutable AssetVersion.

The existing `/moneyprinter/generate` and `/moneyprinter/status/{task_id}` routes are synchronous demonstration paths backed by `external_tasks`; they do not provide project scope, idempotency, leases, restart recovery, safe artifact intake or rights-blocked handoff.

## Gate status

- Documentation drafting: in progress in a documentation-only PR.
- Documentation formal review: not approved.
- Documentation merge: not approved.
- Functional coding: not approved and not started.
- Real provider activation: prohibited.
- Deployment/public access: prohibited.

## Non-goals

No new provider, dependency, queue service, real credential, paid call, digital avatar, voice clone, face/person replacement, wardrobe/background transformation, automatic adoption, ungoverned timeline write, deployment or public access.

