-- Per-user Google Calendar OAuth credentials (G2a — the swappable Google
-- read provider, core/calendar_google.py). Stores only the REFRESH token;
-- the short-lived access token is never persisted — it's derived from the
-- refresh token + app-level client_id/client_secret/token_uri (.env
-- constants, not per-user) on every use, same pattern the ElevenLabs/
-- Gemini keys already use: one app-level secret, many users' own tokens.
--
-- BACKEND-ONLY, DELIBERATELY: RLS is enabled with ZERO policies for the
-- `authenticated` role — not even a owner-only select policy like
-- scheduling_preferences/calendar_snapshot have. A refresh token is a
-- standing credential with offline access to the user's real calendar;
-- the client must NEVER be able to read it directly via a user-scoped
-- Supabase call, only the backend's service-role key (which bypasses RLS
-- entirely, same as every other table core/db.py touches) may. If a
-- client-facing "is Google connected?" check is ever needed, add a
-- narrow RPC/view that returns a boolean, never a policy on this table.
create table if not exists google_calendar_credentials (
  user_id      uuid not null primary key references profiles(id) on delete cascade,
  refresh_token text not null,
  scopes       text not null,              -- space-separated, as granted (see core/calendar_google.py)
  connected_at timestamptz not null default now()
);

alter table google_calendar_credentials enable row level security;
-- No policies created on purpose — see the BACKEND-ONLY comment above.
