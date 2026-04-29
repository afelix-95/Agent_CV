-- Track the last-modified timestamp of each SharePoint item so the watcher
-- can skip files whose content has not changed since the last ingest.
ALTER TABLE source_documents
    ADD COLUMN IF NOT EXISTS sharepoint_modified_at timestamptz;
