-- Expand calendar permissions from a single master flag to a master switch
-- plus per-action grants. calendar_enabled remains the master switch.

alter table profiles add column if not exists calendar_create_enabled boolean not null default false;
alter table profiles add column if not exists calendar_list_enabled   boolean not null default false;
alter table profiles add column if not exists calendar_move_enabled   boolean not null default false;
alter table profiles add column if not exists calendar_delete_enabled boolean not null default false;

-- Backfill: users who had calendar_enabled=true were granted create under the
-- old single-flag model, so preserve that grant. list/move/delete are new
-- capabilities and stay false until explicitly granted.
update profiles set calendar_create_enabled = calendar_enabled;
