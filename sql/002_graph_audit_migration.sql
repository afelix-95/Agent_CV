-- Migration: align audit schema with Graph polling approach
-- Safe to run multiple times (all statements are idempotent).

-- 1. Drop the unused per-user access scope table (was never referenced in code)
drop table if exists teams_user_access_scope;

-- 2. Drop the old FK-based user column from query_audit
alter table query_audit drop column if exists teams_user_id;

-- 3. Add direct AAD object ID and Teams chat ID columns
alter table query_audit add column if not exists aad_object_id text;
alter table query_audit add column if not exists chat_id text;
