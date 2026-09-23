-- Persist optional caller-provided context on the originating user message.
-- Existing messages remain NULL because no historical context can be reconstructed.
-- Apply once, before starting code that reads the new column.

ALTER TABLE agent_message
    ADD COLUMN extra_context JSON NULL AFTER reference;

SELECT column_name, column_type, is_nullable
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND table_name = 'agent_message'
  AND column_name = 'extra_context';
