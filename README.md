# Western Washington Outage Watch — GitHub Actions edition

This package runs the MassOutage v1 county monitor as a **scheduled GitHub Actions job**. It is a low-cost, best-effort alternative to an always-on cloud server; it is not a continuously running process and GitHub may delay or drop scheduled events during high load.

## Important: public repository required for free runner usage

GitHub states that standard GitHub-hosted runners are free for public repositories. GitHub Free includes 2,000 Actions minutes/month for private repositories. A five-minute schedule is approximately 8,640 workflow starts per 30-day month, so a private Free repository is unlikely to stay within its included runner allowance. To use GitHub-hosted runners without runner-minute charges, create a **public repository**.

That means the bot's Python source and the `bot-state` branch will be publicly visible. The state branch contains only the SQLite alert state derived from public outage data; it does not contain API keys or Slack tokens. All credentials must be stored as GitHub Actions secrets. Never commit `.env`, API keys, Slack tokens, or screenshots containing them.

## What this package does

- Runs once every five minutes using a scheduled workflow, offset to minute 3, 8, 13, ... 58 of each hour.
- Uses MassOutage's free API key to check all 19 Western Washington county proxies.
- Runs `python bot.py --once` for each workflow invocation.
- Saves the SQLite alert state to a dedicated `bot-state` branch, then restores it on the next run. This preserves the 15-minute hold timer, event state, Slack thread timestamp, 30-minute growth cooldown, and rolling hourly cap across separate GitHub-hosted runners.
- Uses GitHub Actions `concurrency` to avoid overlapping poll jobs.
- Keeps the initial deployment in `DRY_RUN=true`; no automatic outage alerts are sent until you deliberately edit the workflow.

## Limitations you should understand

1. **Not guaranteed to run exactly every five minutes.** GitHub documents that scheduled workflows can be delayed during high load, and jobs may occasionally be dropped. A sampling gap longer than the bot's configured maximum resets unconfirmed duration. This is best-effort monitoring, not emergency notification infrastructure.
2. **The repository must be public for free hosted-runner minutes.** Code and the state branch are visible. Secrets remain encrypted in GitHub and are only injected into this workflow, but you must not add untrusted third-party actions or print secret environment variables.
3. **State is committed to the `bot-state` branch.** The workflow force-updates that branch to keep only the latest database snapshot rather than accumulating a commit every five minutes. Keep the branch dedicated to this bot; do not use it for other files.
4. **Do not run a second live worker** on your Windows PC or another host using the same Slack destination/state. GitHub concurrency prevents overlapping workflow runs, but it cannot coordinate with a separate local process.
5. The 19 whole counties are a geographic proxy for Western Washington, not an exact Cascade-crest boundary. MassOutage is an unofficial aggregation and may report degraded upstream feeds. Missing/stale counties remain UNKNOWN and cannot be treated as zero.
6. Scheduled workflow support uses UTC cron. The `3-58/5` schedule avoids the top of the hour, but delays remain possible.

## 1. Create an empty public GitHub repository

1. Sign in at https://github.com.
2. Create a new repository, for example `western-wa-outage-watch`.
3. Choose **Public** (required for free GitHub-hosted standard runner usage).
4. Do not initialize it with a README, license, or `.gitignore`; this package already contains those files.

## 2. Upload this package to GitHub from Windows

Extract the ZIP. Open PowerShell in the extracted `massoutage_rebuild` folder (the folder containing `bot.py` and `.github`). Confirm Git is installed:

```powershell
git --version
```

If Git is installed, run these commands, replacing the URL with your repository's HTTPS URL:

```powershell
git init -b main
git add .
git status
git commit -m "Add Western WA outage monitor"
git remote add origin https://github.com/YOUR-USERNAME/western-wa-outage-watch.git
git push -u origin main
```

Check `git status` before committing. `.env` is ignored by `.gitignore`; do not force-add it. This ZIP does not include your local `.env` or live database.

If Git asks you to authenticate, complete GitHub's browser/device sign-in. Do not put a password or token in the remote URL.

## 3. Add the three GitHub Actions secrets

In the repository on GitHub:

1. Open **Settings**.
2. Open **Secrets and variables → Actions**.
3. Select **New repository secret**.
4. Add these three secrets, one at a time:

| Secret name | Value |
|---|---|
| `MASSOUTAGE_API_KEY` | Your MassOutage free API key |
| `SLACK_BOT_TOKEN` | Your existing Slack bot token (`xoxb-...`) |
| `SLACK_CHANNEL_ID` | Your Slack destination channel ID (`C...`) |

Do not add values to workflow YAML or repository variables. GitHub encrypts Actions secrets and exposes them only when a workflow explicitly references them. Never echo them to logs.

## 4. Enable Actions and run the first dry-run

1. Open the repository's **Actions** tab.
2. If GitHub asks, enable Actions for the repository.
3. Select **Western Washington outage monitor**.
4. Choose **Run workflow** to start the first manual run.
5. Open the run and inspect **Poll MassOutage and process one observation**.

The workflow is set to `DRY_RUN=true`. It will fetch MassOutage, update the alert state, and print any proposed alert instead of posting to Slack. It should show `Fresh counties: 19/19` when all county data is fresh. A provider-degraded warning can still appear; read it carefully. If a county is UNKNOWN or the data are stale, do not enable live delivery.

The scheduled job will then run on the five-minute cadence. Allow at least 20 minutes of runs to verify that the `bot-state` branch is created and that state is restored on successive runs. You can inspect that branch in the repository; it should contain only the current `state.dry-run.db` file.

## 5. Move from dry-run to live Slack delivery (only after validation)

Do not make this change during initial setup. First confirm:

- Multiple scheduled runs complete successfully.
- The logs show all 19 county observations fresh (or any missing/stale data is understood).
- The `bot-state` branch is restored and updated on successive runs.
- `DRY_RUN=true` produces only console output and does not send outage alerts.

Then edit `.github/workflows/outage-monitor.yml` on the default branch:

1. Change `DRY_RUN: 'true'` to `DRY_RUN: 'false'`.
2. Change `STATE_FILE: data/state.github-dry-run.db` to `STATE_FILE: data/state.github-live.db`.
3. Change `STATE_BRANCH_FILE: state.dry-run.db` to `STATE_BRANCH_FILE: state.live.db`.
4. Keep `ACK_COUNTY_PROXY: 'true'` because the 19-county proxy is being acknowledged.
5. Commit the workflow change.

The live database starts separately from dry-run state, preventing a dry-run notification or test timestamp from suppressing a real Slack notification. The next scheduled/manual run will begin live processing. The bot does **not** send a test message automatically; Slack delivery was already tested separately in your workspace.

To pause monitoring, disable the workflow in the Actions tab. To resume, re-enable it. Do not delete the `bot-state` branch unless you intentionally want to reset alert history and hold timers.

## 6. Inspecting and troubleshooting

- Every run's console output is available under **Actions → workflow run → job → Poll MassOutage and process one observation**.
- A `429` warning means MassOutage has asked the bot to slow down. Do not repeatedly press **Run workflow**; allow the provider's `Retry-After` interval to expire.
- A run that fails before saving state leaves the previous `bot-state` snapshot untouched, and the next run restores that last committed snapshot.
- `bot-state` branch push failure means the next scheduled run may not restore the latest hold/cooldown information. Treat that as a deployment failure; do not run a parallel worker.
- GitHub's workflow scheduler is best-effort and can be delayed or drop scheduled events. This setup is not a substitute for a paid always-on host for critical alerts.

## Alert policy retained from the tested bot

- Regional trigger: strictly more than 10,000 customers.
- Hold: strictly more than 15 minutes of fresh qualifying observations.
- Scope: combined count across 19 county proxies.
- Growth update: at least 10,000 additional customers since the last successfully sent notification; no percentage-growth condition.
- Cooldown: at least 30 minutes between growth updates.
- Notification cap: at most two Slack messages per rolling hour.
- Recovery notices off; re-arm only after the total remains strictly below 5,000 for 30 minutes of fresh observations.

## Sources and platform documentation

- GitHub scheduled workflows and five-minute minimum interval: https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax
- GitHub notes on scheduled-workflow delays/dropped runs: https://docs.github.com/en/actions/how-tos/troubleshoot-workflows
- GitHub Actions billing (standard runners free for public repositories; private plan allowances): https://docs.github.com/en/actions/concepts/billing-and-usage
- GitHub Actions secrets: https://docs.github.com/en/actions/concepts/security/secrets
- MassOutage API: https://massoutage.com/developers

This bot is for situational awareness only. It is not an emergency service. Confirm outages with the relevant utility before acting.
