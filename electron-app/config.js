// Public Supabase config. The anon key is intentionally public — it is safe to
// ship in a desktop app. Security comes from server-side RLS policies.
// User session tokens are never stored here; they live encrypted in safeStorage.
module.exports = {
  SUPABASE_URL:      'https://xrohxpmxnrhiqfpqtfff.supabase.co',
  SUPABASE_ANON_KEY: 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inhyb2h4cG14bnJoaXFmcHF0ZmZmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA0NDc1ODcsImV4cCI6MjA5NjAyMzU4N30.CF30KDhe3oPNEbH5jw_ySX-WPa9qPk9RDOqLrBiASkw',
  API_BASE:          'http://localhost:8000',
};
