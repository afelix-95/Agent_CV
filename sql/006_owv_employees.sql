-- OWV employee roster table.
-- Populated and kept up-to-date by OWVSyncService (daily background sync).
-- Separate from the CV-centric `employees` table; linked via display_name matching.
create table if not exists owv_employees (
    owv_employees_id        uuid primary key default gen_random_uuid(),
    -- Integer ID from the OWV API (stable identifier across syncs)
    owv_id                  integer not null,
    -- Short display format from API "name" field, e.g. "ABEL Rui"
    name                    text not null,
    -- Proper full name from API "fullName" field, e.g. "Rui Manuel Mateus Abel"
    full_name               text not null,
    -- Computed short name: first + last word of full_name, e.g. "Rui Abel".
    -- Used for fuzzy matching against employees.full_name (filename-derived, typically "Firstname Lastname").
    display_name            text not null,
    email                   text,
    team                    text,
    -- Short format, e.g. "GOMES Bruno"
    manager_name            text,
    -- Short format, e.g. "BARBOSA Elisabete"
    do_executive_manager_name text,
    date_started            date,
    date_end                date,
    active                  boolean not null default true,
    last_synced_at          timestamptz not null default now(),
    constraint owv_employees_owv_id_unique unique (owv_id)
);

create index if not exists idx_owv_employees_owv_id      on owv_employees (owv_id);
create index if not exists idx_owv_employees_email       on owv_employees (email);
create index if not exists idx_owv_employees_active      on owv_employees (active);
create index if not exists idx_owv_employees_manager     on owv_employees (manager_name);
create index if not exists idx_owv_employees_team        on owv_employees (team);
create index if not exists idx_owv_employees_display     on owv_employees (lower(display_name));
