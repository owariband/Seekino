-- anivag controlled migration: persist the per-message Agent execution budget.
-- Existing messages predate configurable budgets and therefore remain NULL.

ALTER TABLE agent_message
    ADD COLUMN IF NOT EXISTS requested_run_options JSON NULL
    AFTER requested_top_k;

-- Verify the schema change without treating historical NULL values as errors.
SELECT column_name, column_type, is_nullable
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND table_name = 'agent_message'
  AND column_name = 'requested_run_options';
