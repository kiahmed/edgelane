// Vercel serverless function — the deployed SPA's runtime backend-URL pointer.
//
// The static UI can't self-heal a rotated Cloudflare quick-tunnel URL from a
// baked constant, so it fetches THIS same-origin endpoint at boot. We read the
// current URL from Vercel Edge Config (updated by the cloudflared container on
// every tunnel rotation — see deploy/cloudflared/publish-url.sh). No third
// party in the browser: this is our own Vercel infra, and the API base is
// public config, not a secret.
//
// Requires an Edge Config connected to the project (Vercel injects the
// EDGE_CONFIG connection string automatically). Degrades to null → the client
// falls back to its baked __EDGELANE_API_BASE__.
export const config = { runtime: 'nodejs' };

export default async function handler(_req, res) {
  res.setHeader('Cache-Control', 'no-store, must-revalidate');
  let apiBase = null;
  try {
    if (process.env.EDGE_CONFIG) {
      const { get } = await import('@vercel/edge-config');
      apiBase = (await get('api_base')) ?? null;
    }
  } catch (e) {
    // Edge Config unreachable/unconfigured — the client uses its baked fallback.
    apiBase = null;
  }
  res.status(200).json({ apiBase });
}
