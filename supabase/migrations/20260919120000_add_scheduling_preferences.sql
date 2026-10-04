-- Scheduling preferences the AI learns per activity, per user (Scheduler
-- feature, Part A). Read ONLY by the calendar-blind activity-inference
-- step to override its defaults, and by the see/reset valve.
--
-- THE notes WALL: this table — `notes` especially — must NEVER be read
-- into the Stage-0 persona prompt (core/prompts.py build_dynamic_prompt).
-- That is a structural wall, not a convention: build_dynamic_prompt has
-- no import of or call into core.db's scheduling_preferences readers,
-- and no caller of fetch_scheduling_preference/list_scheduling_preferences
-- may pass their result toward that prompt. `notes` is free-text
-- breakdown copy for phrasing a scheduling proposal ("no shower time for
-- gym") ONLY — it is not a general persona-facing notes field. When in
-- doubt, generate breakdown copy from the numeric columns below and leave
-- `notes` unused.
create table if not exists scheduling_preferences (
  user_id              uuid not null references profiles(id) on delete cascade,
  activity             text not null,               -- normalized key: lowercase, singular ("gym")
  default_duration_min integer,                     -- base activity length she should assume
  pad_before_min       integer not null default 0,  -- commute/setup before
  pad_after_min        integer not null default 0,  -- shower/cleanup after
  tod_pref             text,                         -- 'morning'|'afternoon'|'evening'| null
  notes                text,                         -- FENCED: breakdown copy ONLY — see wall comment above; never reaches build_dynamic_prompt
  updated_at           timestamptz not null default now(),
  primary key (user_id, activity)
);

alter table scheduling_preferences enable row level security;

-- Owner-only, mirroring profiles'/messages' policies but keyed on the
-- user_id column — scheduling_preferences has no separate `id` column;
-- (user_id, activity) together are the primary key.
create policy "users can read own scheduling prefs"
  on scheduling_preferences for select to authenticated using (auth.uid() = user_id);

create policy "users can insert own scheduling prefs"
  on scheduling_preferences for insert to authenticated with check (auth.uid() = user_id);

create policy "users can update own scheduling prefs"
  on scheduling_preferences for update to authenticated
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create policy "users can delete own scheduling prefs"
  on scheduling_preferences for delete to authenticated using (auth.uid() = user_id);
