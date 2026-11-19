/**
 * Cloudflare Worker Cron Dispatcher for Trading 212 Market Runner.
 *
 * Automatically triggers the GitHub Actions workflow at:
 * - 07:55 UTC (08:55 CET) - 5 minutes before London market open
 * - 12:25 UTC (13:25 CET) - Afternoon session
 *
 * Dispatches directly to GitHub API (workflow_dispatch) so it runs
 * instantly with high priority (no GitHub cron queue delays or drops).
 */

export default {
  // Triggered automatically by Cloudflare Edge Cron
  async scheduled(event, env, ctx) {
    const url = "https://api.github.com/repos/shahpratik1/leveraged-etp-engine/actions/workflows/market_runner.yml/dispatches";

    if (!env.GITHUB_PAT) {
      console.error("❌ GITHUB_PAT secret is missing in Cloudflare Worker environment variables.");
      return;
    }

    try {
      const response = await fetch(url, {
        method: "POST",
        headers: {
          "Accept": "application/vnd.github+json",
          "Authorization": `Bearer ${env.GITHUB_PAT}`,
          "User-Agent": "Cloudflare-Edge-Cron-Dispatcher",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({
          ref: "main",
        }),
      });

      if (response.status === 204) {
        console.log(`✅ [${new Date().toISOString()}] Successfully triggered GitHub Actions market_runner.yml via workflow_dispatch!`);
      } else {
        const errorText = await response.text();
        console.error(`❌ Failed to trigger workflow (HTTP ${response.status}): ${errorText}`);
      }
    } catch (err) {
      console.error("❌ Network error connecting to GitHub API:", err);
    }
  },

  // Allows manual triggering / health-check by visiting worker URL in a web browser
  async fetch(request, env) {
    const url = new URL(request.url);

    // If visiting /trigger?secret=..., allow manual 1-click trigger from browser
    if (url.pathname === "/trigger") {
      const ghUrl = "https://api.github.com/repos/shahpratik1/leveraged-etp-engine/actions/workflows/market_runner.yml/dispatches";
      const resp = await fetch(ghUrl, {
        method: "POST",
        headers: {
          "Accept": "application/vnd.github+json",
          "Authorization": `Bearer ${env.GITHUB_PAT}`,
          "User-Agent": "Cloudflare-Edge-Cron-Dispatcher",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ ref: "main" }),
      });

      if (resp.status === 204) {
        return new Response("✅ Successfully triggered GitHub Actions Market Runner via Cloudflare!", {
          status: 200,
          headers: { "Content-Type": "text/plain; charset=utf-8" },
        });
      } else {
        const err = await resp.text();
        return new Response(`❌ Failed (HTTP ${resp.status}): ${err}`, { status: resp.status });
      }
    }

    return new Response(
      "⚡ Cloudflare Market Runner Cron Dispatcher is online and healthy.\n" +
      "Active Cron Schedule: 07:55 UTC and 12:25 UTC (Mon-Fri)\n" +
      "To trigger manually, visit /trigger",
      { headers: { "Content-Type": "text/plain; charset=utf-8" } }
    );
  },
};
