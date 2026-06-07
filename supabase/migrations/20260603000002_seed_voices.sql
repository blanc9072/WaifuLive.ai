-- Add tts_ref column if not already present (earlier migrations may have added it).
alter table voices add column if not exists tts_ref text;

-- Enforce at most one default voice at the DB level.
create unique index if not exists voices_single_default_idx
  on voices (is_default) where is_default = true;

-- Rename the existing default voice and set its canonical tts_ref.
-- UPDATE keeps the row ID intact so existing profiles.voice_id FKs stay valid.
update voices
  set name = 'Sapphire',
      description = 'Warm, natural Chinese-American voice.',
      tts_ref = 'zmcVlqmyk3Jpn5AVYcAL'
  where is_default = true;

-- Add Lily as a second (non-default) voice if not already present.
insert into voices (name, description, tts_ref, is_default)
  select 'Lily', 'Soft, expressive voice.', 'L1QogKoobNwLy4IaMsyA', false
  where not exists (select 1 from voices where tts_ref = 'L1QogKoobNwLy4IaMsyA');

-- Seed the Nicole Live2D avatar if not already present.
insert into avatars (name, file_path, is_default)
  select 'Nicole', '../../assets/live2d/Nicole/Nicole.model3.json', true
  where not exists (select 1 from avatars where name = 'Nicole');

-- Auto-create a profile for every new Supabase Auth user, pointing
-- at the is_default rows so a new user always has a working voice.
-- security definer lets this function write profiles even though RLS
-- is on — it runs as the function owner (postgres), not the new user.
create or replace function handle_new_user()
returns trigger
language plpgsql
security definer set search_path = public
as $$
declare
  v_persona_id uuid;
  v_voice_id   uuid;
  v_avatar_id  uuid;
begin
  select id into v_persona_id from personas where is_default = true limit 1;
  select id into v_voice_id   from voices   where is_default = true limit 1;
  select id into v_avatar_id  from avatars  where is_default = true limit 1;

  insert into profiles (id, persona_id, voice_id, avatar_id)
  values (new.id, v_persona_id, v_voice_id, v_avatar_id);

  return new;
end;
$$;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure handle_new_user();
