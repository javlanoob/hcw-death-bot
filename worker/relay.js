// Relay for the HC Worlds hiscores (GitHub's servers are blocked by RuneScape; Cloudflare's are not).
// Only forwards requests to the hardcore hiscores. Public data, nothing secret here.
export default {
  async fetch(request) {
    const u = new URL(request.url).searchParams.get("u") || "";
    let t;
    try { t = new URL(u); } catch { return new Response("bad url", { status: 400 }); }
    if (t.hostname !== "secure.runescape.com" || !t.pathname.startsWith("/m=hiscore_oldschool_hardcore_mains/")) {
      return new Response("forbidden", { status: 403 });
    }
    const r = await fetch(t.toString(), { headers: { "User-Agent": "hcw-death-feed/1.0 (hiscores death tracker)" } });
    return new Response(r.body, { status: r.status, headers: { "content-type": r.headers.get("content-type") || "text/html" } });
  },
};
