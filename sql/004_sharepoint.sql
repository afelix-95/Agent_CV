-- SharePoint integration: extend source_documents with SharePoint metadata
-- and add a sync_state table to persist the Graph delta link between restarts.

ALTER TABLE source_documents
    ADD COLUMN IF NOT EXISTS sharepoint_item_id  text,
    ADD COLUMN IF NOT EXISTS sharepoint_web_url  text;

CREATE TABLE IF NOT EXISTS sync_state (
    key         text        PRIMARY KEY,
    value       text        NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_source_documents_sp_item
    ON source_documents (sharepoint_item_id)
    WHERE sharepoint_item_id IS NOT NULL;
