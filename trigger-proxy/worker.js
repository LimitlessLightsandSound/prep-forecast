// Cloudflare Worker — turns a Re-sync click on the (public) dashboard into a
// GitHub Actions rebuild, without exposing any secret on the page.
//
// The page can't hold a GitHub token (it's public), so it POSTs here instead.
// This Worker holds the token as an encrypted Cloudflare secret and fires the
// repository_dispatch that runs .github/workflows/rebuild.yml.
//
// Deploy: see trigger-proxy/README.md. Set two secrets on the Worker:
//   GH_TOKEN  — a fine-grained PAT, this repo only, "Contents: write"
//   REPO      — "LimitlessLightsandSound/prep-forecast"
// Optional: ALLOW_ORIGIN — the dashboard origin, to lock CORS down
//   (defaults to "*", which is fine; the only action exposed is a harmless rebuild).

export default {
  async fetch(request, env) {
    const origin = env.ALLOW_ORIGIN || "*";
    const cors = {
      "Access-Control-Allow-Origin": origin,
      "Access-Control-Allow-Methods": "POST, OPTIONS",
      "Access-Control-Allow-Headers": "content-type",
    };
    if (request.method === "OPTIONS") return new Response(null, { headers: cors });
    if (request.method !== "POST")
      return new Response("POST to rebuild", { status: 405, headers: cors });

    const r = await fetch(`https://api.github.com/repos/${env.REPO}/dispatches`, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${env.GH_TOKEN}`,
        "Accept": "application/vnd.github+json",
        "User-Agent": "prep-forecast-rebuild",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ event_type: "rebuild" }),
    });

    // GitHub returns 204 on success; pass back a clean JSON result.
    const ok = r.status === 204;
    return new Response(JSON.stringify({ ok, status: r.status }), {
      status: ok ? 202 : 502,
      headers: { ...cors, "Content-Type": "application/json" },
    });
  },
};
