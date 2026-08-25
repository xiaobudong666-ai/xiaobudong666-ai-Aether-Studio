# IM9–IM11 PRD—Code Alignment

| Requirement | Current repository state | Coding status |
|---|---|---|
| IM9 generation request/preflight | Defined in merged approval package | Not implemented |
| IM10 task state/retry/cancel | Defined in merged approval package | Not implemented |
| IM11 result review/provenance/intake | Defined in merged approval package | Not implemented |
| Rights snapshot gate | Existing governance principle reused | Must remain mandatory |
| Deterministic fake adapter | Approval requirement | To be implemented after coding approval |
| Real provider/plugin/model | Explicitly out of scope | Not authorized |

## Post-merge alignment
The documentation gate is accepted because the approval package is merged into `main`. This does **not** convert any PRD item into implemented functionality. The repository has no functional-code evidence for IM9–IM11 yet.

## Implementation gate
Before coding, the owner must explicitly approve the IM9–IM11 coding scope. The first implementation pass must use deterministic local/fake adapters and existing contracts. Any new dependency, backend endpoint, migration, worker, provider/plugin/model, paid call, deployment or public-access change remains outside this authorization.

## Acceptance evidence rule
A future implementation claim requires a code diff plus executable test evidence. Documentation, contracts, screenshots or roadmap entries alone cannot be counted as implementation completion.
