# Cloudflare Worker Cron Dispatcher for Market Runner

This lightweight, 100% free serverless Cloudflare Worker solves GitHub Actions' unreliable cron scheduler.

### Why use Cloudflare Cron Triggers instead of GitHub Actions `schedule:`?
* **Zero Delay / 0s Jitter**: Cloudflare's distributed edge triggers crons at the exact second (`07:55` and `12:25 UTC`).
* **Never Dropped**: GitHub Actions often drops scheduled events on busy weekdays; Cloudflare never drops cron triggers.
* **Dispatches as High Priority**: Calls GitHub's `POST .../actions/workflows/market_runner.yml/dispatches` (`workflow_dispatch`), so GitHub spins up an Ubuntu VM immediately within 5 seconds.
* **100% Free**: Uses ~2 requests/day out of Cloudflare's 100,000 free requests/day limit.

---

### Method 1: 2-Minute Setup in Cloudflare Dashboard (No Terminal)

1. Go to [dash.cloudflare.com](https://dash.cloudflare.com) → **Workers & Pages** → **Create Application** → **Create Worker**.
2. Name it `market-runner-cron` and click **Deploy**.
3. Click **Edit code**, paste the contents of [`src/index.js`](./src/index.js), and click **Deploy**.
4. Go to **Settings** → **Variables and Secrets**:
   * Add secret: `GITHUB_PAT`
   * Value: A GitHub Personal Access Token with `actions:write` or `repo` permissions (generate at [github.com/settings/tokens](https://github.com/settings/tokens)).
5. Go to **Settings** → **Triggers** → **Cron Triggers** → **Add Cron Trigger**:
   * `55 7 * * 1-5` (Morning Session: 07:55 UTC / 08:55 CET)
   * `25 12 * * 1-5` (Afternoon Session: 12:25 UTC / 13:25 CET)

---

### Method 2: Deploy with Wrangler CLI

```bash
cd cloudflare-cron
npx wrangler secret put GITHUB_PAT
npx wrangler deploy
```
