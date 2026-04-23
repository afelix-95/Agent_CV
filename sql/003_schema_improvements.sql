-- Migration 003: schema improvements for LLM agent
-- All statements are idempotent (use IF EXISTS / IF NOT EXISTS).

-- ------------------------------------------------------------------ --
-- 1. Drop tables that served the old keyword-heuristic pipeline only. --
-- ------------------------------------------------------------------ --
drop table if exists certification_skill_map;
drop table if exists skill_terms;
drop table if exists skill_categories;
drop table if exists certification_status_history;

-- ------------------------------------------------------------------ --
-- 2. Rename normalized_intent_json -> agent_tool_calls in query_audit --
--    and add the column if it doesn't exist under the new name yet.   --
-- ------------------------------------------------------------------ --
do $$
begin
    -- rename old column if it exists
    if exists (
        select 1 from information_schema.columns
        where table_name = 'query_audit' and column_name = 'normalized_intent_json'
    ) then
        alter table query_audit rename column normalized_intent_json to agent_tool_calls;
    end if;

    -- add new column if neither old nor new exists
    if not exists (
        select 1 from information_schema.columns
        where table_name = 'query_audit' and column_name = 'agent_tool_calls'
    ) then
        alter table query_audit add column agent_tool_calls jsonb;
    end if;
end
$$;

-- ------------------------------------------------------------------ --
-- 3. New indexes                                                       --
-- ------------------------------------------------------------------ --

-- Trigram index on cert_code (AZ-900, SC-200, RHCE, etc.)
create index if not exists idx_cert_code_trgm
    on certifications using gin (cert_code gin_trgm_ops)
    where cert_code is not null;

-- GIN index on vendors.aliases (text[]) for array-containment lookups
create index if not exists idx_vendor_aliases_gin
    on vendors using gin (aliases);

-- HNSW vector index for CV chunks (fast approximate cosine search)
create index if not exists idx_cv_chunks_embedding_hnsw
    on cv_chunks using hnsw (embedding vector_cosine_ops)
    with (m = 16, ef_construction = 64);

-- HNSW vector index for certification chunks
create index if not exists idx_cert_chunks_embedding_hnsw
    on certification_chunks using hnsw (embedding vector_cosine_ops)
    with (m = 16, ef_construction = 64);

-- Descending index on query_audit.created_at for the audit log endpoint
create index if not exists idx_query_audit_created
    on query_audit (created_at desc);
