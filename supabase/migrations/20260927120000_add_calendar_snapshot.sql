-- Calendar snapshot cache (CS1 — Store). A backend periodic task (CS2)
-- reads each calendar on a schedule and writes here; live reads (CS3) hit
-- this table instead of a synchronous, contention-prone multi-calendar
-- osascript read on every request. See the "Calendar snapshot cache"
-- spec for the full rationale — this migration is Part A only.

-- Per-event snapshot rows for a rolling near-term window, keyed by
-- calendar-scoped uid (an event's uid is stable but only unique WITHIN
-- its own calendar, per prior spikes — hence the composite key).
create table if not exists calendar_snapshot (
  user_id     uuid not null references profiles(id) on delete cascade,
  calendar    text not null,             -- calendar name (e.g. 'andrewzeng632@gmail.com')
  uid         text not null,             -- event uid (stable, per prior spikes)
  title       text,
  start_ts    timestamptz not null,      -- stored as a real, timezone-aware instant — see
  end_ts      timestamptz,               -- core/db.py's snapshot writer for the naive-local
                                          -- -> timestamptz conversion this depends on (Part D)
  all_day     boolean not null default false,
  updated_at  timestamptz not null default now(),
  primary key (user_id, calendar, uid)
);
create index if not exists calendar_snapshot_user_start
  on calendar_snapshot (user_id, start_ts);

-- Per-calendar refresh bookkeeping: freshness + reachability, so a live
-- read can be honest about a calendar the refresher couldn't reach
-- ("as of <last_refresh>") instead of silently presenting a stale or
-- missing snapshot as guaranteed-current truth.
create table if not exists calendar_refresh_state (
  user_id       uuid not null references profiles(id) on delete cascade,
  calendar      text not null,
  last_refresh  timestamptz,             -- last SUCCESSFUL refresh
  last_attempt  timestamptz,
  reachable     boolean not null default true,   -- false if the last attempt timed out/failed
  primary key (user_id, calendar)
);

alter table calendar_snapshot      enable row level security;
alter table calendar_refresh_state enable row level security;

-- Owner-only, all four verbs, keyed on user_id — mirrors
-- scheduling_preferences' policies (see
-- 20260919120000_add_scheduling_preferences.sql) on both tables.
create policy "users can read own calendar snapshot"
  on calendar_snapshot for select to authenticated using (auth.uid() = user_id);
create policy "users can insert own calendar snapshot"
  on calendar_snapshot for insert to authenticated with check (auth.uid() = user_id);
create policy "users can update own calendar snapshot"
  on calendar_snapshot for update to authenticated
  using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "users can delete own calendar snapshot"
  on calendar_snapshot for delete to authenticated using (auth.uid() = user_id);

create policy "users can read own calendar refresh state"
  on calendar_refresh_state for select to authenticated using (auth.uid() = user_id);
create policy "users can insert own calendar refresh state"
  on calendar_refresh_state for insert to authenticated with check (auth.uid() = user_id);
create policy "users can update own calendar refresh state"
  on calendar_refresh_state for update to authenticated
  using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "users can delete own calendar refresh state"
  on calendar_refresh_state for delete to authenticated using (auth.uid() = user_id);
