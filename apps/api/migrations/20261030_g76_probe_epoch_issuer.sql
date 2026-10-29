-- Draft-only PostgreSQL additive migration for G76 durable probe epoch/issuer.
-- Do not apply to production without explicit Owner approval.
ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS lease_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS circuit_probe_epoch INTEGER NULL;
ALTER TABLE generation_circuit_states
    ADD COLUMN IF NOT EXISTS probe_epoch INTEGER NOT NULL DEFAULT 0;
ALTER TABLE generation_circuit_states
    ADD COLUMN IF NOT EXISTS half_open_lease_epoch INTEGER NULL;
ALTER TABLE generation_circuit_states
    ADD COLUMN IF NOT EXISTS half_open_issuer VARCHAR NULL;
