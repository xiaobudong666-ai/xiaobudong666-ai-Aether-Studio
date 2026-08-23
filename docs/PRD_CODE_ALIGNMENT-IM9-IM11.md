# IM9–IM11 PRD—Code Alignment

| Requirement | Current repository state | Coding status |
|---|---|---|
| IM9 generation request/preflight | Defined in approval package | Not implemented in this batch |
| IM10 task state/retry/cancel | Defined in approval package | Not implemented in this batch |
| IM11 result review/provenance/intake | Defined in approval package | Not implemented in this batch |
| Rights snapshot gate | Existing governance principle reused | Must remain mandatory |
| Deterministic fake adapter | Approval requirement | To be implemented after coding approval |
| Real provider/plugin/model | Explicitly out of scope | Not authorized |

## Alignment rule
No PRD item may be represented as implemented merely because its contract is documented. Repository acceptance of this package means only that the planned boundaries are recorded. Functional implementation requires a separate coding approval and must be evidenced by tests and a code diff.

## Dependency rule
The proposed implementation must first reuse the existing application contracts and local test adapters. Any request for a new dependency, backend endpoint, migration, worker, provider/plugin/model, paid call or deployment is an approval boundary and cannot be inferred from this document.
