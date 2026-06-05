-- Rename external_id -> tts_ref on the voices table
alter table voices rename column external_id to tts_ref;

-- Remove the placeholder Rachel row and upsert the real default voice
delete from voices where name = 'Rachel';

insert into voices (name, description, is_default, tts_ref)
values ('Tachi', 'Default companion voice', true, 'zmcVlqmyk3Jpn5AVYcAL')
on conflict (name) do update set tts_ref = excluded.tts_ref, is_default = true;
