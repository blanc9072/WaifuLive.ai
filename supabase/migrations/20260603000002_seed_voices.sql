-- Add the missing tts_ref column to voices
alter table voices add column tts_ref text;

-- Enforce at most one default voice at the DB level.
-- Partial unique index: only one row where is_default=true is allowed;
-- any number of is_default=false rows are fine.
create unique index voices_single_default_idx
  on voices (is_default) where is_default = true;

-- Seed the default voice. ElevenLabs ID lives only in this row — never in code.
insert into voices (name, description, tts_ref, is_default) values (
  'Pistachio Default',
  'Warm, natural voice for Pistachio.',
  'zmcVlqmyk3Jpn5AVYcAL',
  true
);

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

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute procedure handle_new_user();
