// Deploy-time runtime config. This placeholder ships nulls => the app runs in
// local dev-bypass mode (no auth gate, API on same-origin/127.0.0.1). At deploy
// time `deploy.sh`'s stage_simmer() OVERWRITES this file in build/ with the real
// values from deploy/.env, and vercel.json serves it with Cache-Control:
// no-store so a rotated tunnel URL is never stuck in the CDN.
window.__EDGELANE_API_BASE__ = null;
window.__EDGELANE_SUPABASE_URL__ = null;
window.__EDGELANE_SUPABASE_ANON_KEY__ = null;
window.__EDGELANE_TURNSTILE_SITE_KEY__ = null; // unused in Simmer v1 (no anon teaser)
