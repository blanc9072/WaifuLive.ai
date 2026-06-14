alter table avatars add column if not exists expression_map jsonb;

update avatars set expression_map = '{
  "chill":    ["Milk Tea", null, "sitting position"],
  "happy":    ["Y Hand Posture", null, "shyness"],
  "excited":  ["Y Hand Posture", "Money eye", null],
  "loving":   ["Love eye", "shyness", null, "Love eye"],
  "romantic": ["Love eye", "shyness", "Love eye", null],
  "sad":      ["cry", null, "cry"],
  "annoyed":  ["black face", null, "black face"],
  "jealous":  ["black face", "black face", null],
  "playful":  ["Money Hand Posture", "Money eye", "Y Hand Posture", null],
  "curious":  ["Phone Hand Posture", null, "shyness", "sitting position"],
  "shy":      ["shyness", null, "shyness"],
  "flirty":   ["Love eye", "shyness", "Love eye", null],
  "default":  ["Milk Tea", null, "shyness", null]
}'::jsonb
where name = 'Nicole' and expression_map is null;
