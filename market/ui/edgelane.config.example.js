// edgelane.config.js — runtime config baked into the deployed market UI.
//
// Copy to `edgelane.config.js` for local testing, or let deploy.sh generate it
// from the EDGELANE_* env vars (see deploy.sh -> stage_frontend). All values
// here are browser-safe: the Supabase anon key is RLS-gated and the Turnstile
// site key is public. NEVER put SERVICE_KEY / TURNSTILE_SECRET / JWT_SECRET here.
//
// If the Supabase values are null/absent the UI runs in DEV-BYPASS: the auth
// gate is disabled, the full dashboard is open, and no login is required
// (matches the backend's AUTH_ENABLED=false). Fill them in to enable the
// signup/signin gate + per-user broker connections.

window.__EDGELANE_API_BASE__          = null;  // e.g. "https://api.edgelane.example" (Cloudflare tunnel)
window.__EDGELANE_SUPABASE_URL__      = null;  // e.g. "https://abcd1234.supabase.co"
window.__EDGELANE_SUPABASE_ANON_KEY__ = null;  // Supabase project anon/public key
window.__EDGELANE_TURNSTILE_SITE_KEY__= null;  // Cloudflare Turnstile SITE key (public)
