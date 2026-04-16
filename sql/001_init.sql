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

create table if not exists certification_status_history (
    certification_status_history_id uuid primary key default gen_random_uuid(),
    certification_id uuid not null references certifications(certification_id) on delete cascade,
    old_status text,
    new_status text not null,
    changed_at timestamptz not null default now(),
    change_reason text
);

create table if not exists skill_categories (
    skill_category_id uuid primary key default gen_random_uuid(),
    category_name text not null unique,
    description text
);

create table if not exists skill_terms (
    skill_term_id uuid primary key default gen_random_uuid(),
    skill_category_id uuid not null references skill_categories(skill_category_id) on delete cascade,
    term text not null,
    term_type text not null,
    weight numeric(5,4) not null default 1.0,
    unique (skill_category_id, term)
);

create table if not exists certification_skill_map (
    certification_id uuid not null references certifications(certification_id) on delete cascade,
    skill_category_id uuid not null references skill_categories(skill_category_id) on delete cascade,
    mapping_source text not null,
    mapping_confidence numeric(5,4),
    primary key (certification_id, skill_category_id)
);

create table if not exists teams_users (
    teams_user_id uuid primary key default gen_random_uuid(),
    aad_object_id text unique,
    user_principal_name text unique,
    department text,
    locale text
);

create table if not exists teams_user_access_scope (
    teams_user_access_scope_id uuid primary key default gen_random_uuid(),
    teams_user_id uuid not null references teams_users(teams_user_id) on delete cascade,
    scope_type text not null,
    scope_value text not null
);

create table if not exists query_audit (
    query_audit_id uuid primary key default gen_random_uuid(),
    teams_user_id uuid references teams_users(teams_user_id) on delete set null,
    query_text text not null,
    query_language text,
    normalized_intent_json jsonb,
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
create index if not exists idx_vendor_name_trgm on vendors using gin (vendor_name gin_trgm_ops);
create index if not exists idx_skill_term_trgm on skill_terms using gin (term gin_trgm_ops);

create index if not exists idx_cert_expiry_active on certifications (expiry_date)
where status in ('active', 'expiring_90d');

insert into skill_categories (category_name, description)
values
    ('storage', 'Storage technologies and certifications'),
    ('networking', 'Network infrastructure certifications'),
    ('cloud', 'Cloud platform certifications'),
    ('virtualization', 'Virtualization technologies'),
    ('security', 'Security certifications')
on conflict (category_name) do nothing;
