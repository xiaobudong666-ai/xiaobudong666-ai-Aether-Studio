# IM9–IM11 Roadmap

## Accepted baseline
`main@6bfd6f1bfae9de1681775ff6020c87cf97d30aed`

## Sequence
1. IM9 — governed generation request and preflight.
2. IM10 — task state, retry/cancel and deterministic result tracking.
3. IM11 — result review, provenance and governed asset-version intake.

## Gates
- Documentation gate: **closed / accepted** by merge of the IM9–IM11 documentation package.
- Coding gate: explicit owner approval required before functional implementation.
- Integration gate: real provider/plugin/model, paid calls, backend migration, deployment and public access remain separately prohibited until explicitly approved.
- Acceptance gate: deterministic fake adapter first; real provider validation later under separate approval.

## Current implementation status
**Not implemented.** The merged change is documentation only. No requirement in this roadmap is evidence of functional implementation.

## Mainline rationale
IM9–IM11 closes the upstream generation-to-editor gap without bypassing the existing M05–M08 rights and timeline protections. Generated outputs enter the editor only as governed references; final timeline adoption remains explicit.
