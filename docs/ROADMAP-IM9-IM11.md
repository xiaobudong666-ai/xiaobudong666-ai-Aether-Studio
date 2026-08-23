# IM9–IM11 Roadmap

## Baseline
`main@96213f7389d4f665b5b165d4d5a98a29765b3f9f`

## Sequence
1. IM9 — governed generation request and preflight.
2. IM10 — task state, retry/cancel and deterministic result tracking.
3. IM11 — result review, provenance and governed asset-version intake.

## Gates
- Documentation gate: this package and evidence must be reviewed before coding.
- Coding gate: explicit owner approval required.
- Integration gate: real provider/plugin/model, paid calls, backend migration, deployment and public access remain separately prohibited until explicitly approved.
- Acceptance gate: deterministic fake adapter first; real provider validation later under separate approval.

## Mainline rationale
IM9–IM11 closes the upstream generation-to-editor gap without bypassing the existing M05–M08 rights and timeline protections. Generated outputs enter the editor only as governed references; final timeline adoption remains explicit.
