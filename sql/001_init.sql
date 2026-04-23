create extension if not exists vector;
create extension if not exists pg_trgm;
create extension if not exists pgcrypto;

create table if not exists employees (
    employee_id uuid primary key default gen_random_uuid(),
    external_ref text unique,
    full_name text not null unique,
    department text,
    role text,
    country text,
    primary_language text default 'pt',
    active_flag boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists employee_localizations (
    employee_localization_id uuid primary key default gen_random_uuid(),
    employee_id uuid not null references employees(employee_id) on delete cascade,
    language_code text not null,
    localized_summary text,
    localized_headline text,
    last_translated_at timestamptz,
    unique (employee_id, language_code)
);

create table if not exists source_documents (
    document_id uuid primary key default gen_random_uuid(),
    employee_id uuid references employees(employee_id) on delete set null,
    source_system text not null,
    source_path text not null,
    original_filename text not null,
    mime_type text,
    sha256_hash text not null unique,
    detected_language text,
    ingest_status text not null,
    created_at timestamptz not null default now()
);

create table if not exists document_versions (
    document_version_id uuid primary key default gen_random_uuid(),
    document_id uuid not null references source_documents(document_id) on delete cascade,
    version_number integer not null,
    blob_uri text,
    text_snapshot text,
    extraction_confidence numeric(5,4),
    extracted_at timestamptz not null default now(),
    is_current boolean not null default true,
    unique (document_id, version_number)
);

create table if not exists cv_sections (
    cv_section_id uuid primary key default gen_random_uuid(),
    employee_id uuid not null references employees(employee_id) on delete cascade,
    document_version_id uuid not null references document_versions(document_version_id) on delete cascade,
    section_type text not null,
    section_text text,
    start_offset integer,
    end_offset integer,
    language_code text
);

create table if not exists vendors (
    vendor_id uuid primary key default gen_random_uuid(),
    vendor_name text not null unique,
    vendor_family text,
    aliases text[],
    canonical_domain text
);

create table if not exists certifications (
    certification_id uuid primary key default gen_random_uuid(),
    employee_id uuid not null references employees(employee_id) on delete cascade,
    vendor_id uuid references vendors(vendor_id) on delete set null,
    document_version_id uuid not null references document_versions(document_version_id) on delete cascade,
    cert_name text not null,
    cert_level text,
    cert_code text,
    issue_date date,
    expiry_date date,
    status text not null,
    verification_url text,
    confidence_score numeric(5,4),
    extracted_language text,
    last_verified_at timestamptz,
    check (expiry_date is null or issue_date is null or expiry_date >= issue_date)
);

-- certification_status_history, skill_categories, skill_terms, certification_skill_map
-- are intentionally omitted: they were designed for the old keyword-heuristic pipeline
-- which has been replaced by the LLM agent. They can be re-added if needed.

create table if not exists teams_users (
    teams_user_id uuid primary key default gen_random_uuid(),
    aad_object_id text unique,
    user_principal_name text unique,
    department text,
    locale text
);

create table if not exists query_audit (
    query_audit_id uuid primary key default gen_random_uuid(),
    aad_object_id text,
    chat_id text,
    query_text text not null,
    query_language text,
    agent_tool_calls jsonb,           -- array of {tool, args, result_count} objects
    response_language text,
    result_count integer,
    latency_ms integer,
    created_at timestamptz not null default now()
);

create table if not exists cv_chunks (
    cv_chunk_id uuid primary key default gen_random_uuid(),
    employee_id uuid not null references employees(employee_id) on delete cascade,
    document_version_id uuid not null references document_versions(document_version_id) on delete cascade,
    chunk_text text not null,
    chunk_order integer not null,
    token_count integer,
    embedding vector(1536),
    language_code text
);

create table if not exists certification_chunks (
    certification_chunk_id uuid primary key default gen_random_uuid(),
    certification_id uuid not null references certifications(certification_id) on delete cascade,
    document_version_id uuid not null references document_versions(document_version_id) on delete cascade,
    chunk_text text not null,
    token_count integer,
    embedding vector(1536),
    language_code text
);

create index if not exists idx_cert_status_expiry on certifications (status, expiry_date);
create index if not exists idx_cert_vendor_status on certifications (vendor_id, status);
create index if not exists idx_cert_employee on certifications (employee_id);
create index if not exists idx_employees_department on employees (department);
create index if not exists idx_source_documents_status_created on source_documents (ingest_status, created_at);
create index if not exists idx_cert_name_trgm on certifications using gin (cert_name gin_trgm_ops);
create index if not exists idx_cert_code_trgm on certifications using gin (cert_code gin_trgm_ops)
    where cert_code is not null;
create index if not exists idx_vendor_name_trgm on vendors using gin (vendor_name gin_trgm_ops);
create index if not exists idx_vendor_aliases_gin on vendors using gin (aliases);

create index if not exists idx_cert_expiry_active on certifications (expiry_date)
where status in ('active', 'expiring_90d');

-- HNSW vector indexes for fast approximate nearest-neighbour search.
-- m=16, ef_construction=64 are sensible defaults for datasets under ~1M rows.
create index if not exists idx_cv_chunks_embedding_hnsw
    on cv_chunks using hnsw (embedding vector_cosine_ops)
    with (m = 16, ef_construction = 64);

create index if not exists idx_cert_chunks_embedding_hnsw
    on certification_chunks using hnsw (embedding vector_cosine_ops)
    with (m = 16, ef_construction = 64);

create index if not exists idx_query_audit_created on query_audit (created_at desc);
