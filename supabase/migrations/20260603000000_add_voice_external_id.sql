-- Add tts_ref to voices for TTS provider mapping (e.g. ElevenLabs voice ID)
alter table voices add column if not exists tts_ref text;

-- Seed default voice if not already present
insert into voices (name, description, is_default, tts_ref)
values ('Tachi', 'Default companion voice', true, 'zmcVlqmyk3Jpn5AVYcAL')
on conflict (name) do update set tts_ref = excluded.tts_ref;
