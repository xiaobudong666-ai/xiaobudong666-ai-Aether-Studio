# IM9–IM11 Roadmap

## Accepted implementation baseline

`main@8f64a172bf740578dba0fcfe451f1464b9a54028`

## Sequence

1. IM9 — governed generation request and preflight: **implemented**.
2. IM10 — task state, retry/cancel and deterministic result tracking: **implemented**.
3. IM11 — result review, provenance and governed asset-version intake: **implemented**.

## Closed gates

- Documentation gate: **closed / accepted** by merge of the IM9–IM11 documentation package.
- Coding authorization gate: **closed** by the owner's explicit authorization against `main@adf2a81f07a890d74fbb1cad80ea71e7290bfbd4`.
- Functional implementation gate: **closed / accepted** by squash merge of PR #18.
- Verification gate for the authorized frontend/local scope: **closed** with 28/28 approval cases, 51/51 Web tests and CI Pipeline #105 passing.
- Formal-review blocker FR18-01: **resolved** with versioned local snapshot recovery.

## Current implementation status

**Implemented within the authorized frontend/local scope.** The existing React workbench now provides governed generation preflight, a deterministic fake/local task state machine, cancel/retry and attempt history, page-close recovery, provenance and rights-gated result review, and governed editor references.

Generated results remain `adopted=false`; the implementation does not automatically write to the final timeline.

## Open integration gate

Real provider/plugin/model/API-key integration, paid calls, new dependencies, backend APIs, migrations, Worker or queue infrastructure, automatic adoption, ungoverned timeline writes, deployment and public access remain separately prohibited until explicitly approved.

## Mainline rationale

IM9–IM11 closes the upstream generation-to-editor gap without bypassing the existing M05–M08 rights and timeline protections. The accepted local implementation proves the governed workflow and recovery semantics before any real provider or production infrastructure is introduced.
