-- Persist server-owned conversation metadata, including runtime bindings.
-- Apply once, after 20261010_base_model_field_names.sql and before starting
-- code that reads the new column. Existing conversations keep NULL detail.
-- Prepared SQL only; the application does not execute this migration.

ALTER TABLE agent_conversation
    ADD COLUMN detail JSON NULL AFTER last_message_time;

SELECT column_name, column_type, is_nullable
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND table_name = 'agent_conversation'
  AND column_name = 'detail';
