# Setup guide: turning on episode processing

Three one-time steps. Do them in any order; each takes a few minutes. **Never paste a key into a chat, an issue or a file in the repo.** Keys go only into GitHub's secret form (steps 1 and 2).

Repo: https://github.com/tamirgen/signal-trader

---

## 1. Anthropic API key (`ANTHROPIC_API_KEY`) — reads the transcripts

Claude reads each transcript and extracts the signals. This is a separate account from claude.ai chat.

1. Go to https://console.anthropic.com and sign in (or create an account with your email).
2. Add credit: **Settings → Billing → Buy credits**. A $10 starting balance is plenty. Each full episode costs roughly $0.20–0.50.
3. Set a spending limit: **Settings → Limits**, set a monthly limit (for example $20) so it can never run away.
4. Create the key: **Settings → API keys → Create key**. Name it `signal-trader`.
5. **Copy the key now** (starts with `sk-ant-`). It is shown only once.
6. Add it to GitHub:
   1. Open https://github.com/tamirgen/signal-trader/settings/secrets/actions.
   2. Click **New repository secret**.
   3. **Name:** `ANTHROPIC_API_KEY` (exactly, all capitals).
   4. **Secret:** paste the key. Click **Add secret**.

## 2. Deepgram key (`DEEPGRAM_API_KEY`) — converts audio to text

Most podcast feeds publish only audio, so Deepgram's speech-to-text service creates the transcript. If a feed already includes a transcript, Deepgram isn't used and costs nothing.

1. Go to https://deepgram.com and click **Sign Up** (free; new accounts get starter credit, so you can test before paying).
2. After signing in, open your project and go to **API Keys → Create a New API Key**.
3. Name it `signal-trader`, set permissions to **Member** (the default is fine), and click **Create Key**.
4. **Copy the key now.** It is shown only once.
5. Add it to GitHub the same way as step 1: **New repository secret**, **Name:** `DEEPGRAM_API_KEY`, paste the key, **Add secret**.

Cost: roughly $0.50 per two-hour episode (check Deepgram's pricing page for the current rate).

## 3. Turn on the dashboard (GitHub Pages)

1. Open https://github.com/tamirgen/signal-trader/settings/pages.
2. Under **Build and deployment**, find **Source** and choose **GitHub Actions** from the dropdown. It saves automatically.
3. Next time the daily job runs, the dashboard appears at **https://tamirgen.github.io/signal-trader/**.

The repo is public, so the dashboard is public: it shows paper positions, P&L and signals, never keys. To keep it private, make the repo private (Settings → General → Danger Zone → Change visibility); Pages on private repos requires a paid GitHub plan.

---

## 4. Test it

1. Open https://github.com/tamirgen/signal-trader/actions/workflows/daily.yml.
2. Click **Run workflow** (grey button, right side), then the green **Run workflow** in the dropdown.
3. Wait 2–10 minutes (episodes take longer to transcribe), then click the run to open it.
4. Check the results:
   - **Summary page** (scroll down on the run page): today's "what and why" report with the sources checked, new episodes, signals found, trades and any problems.
   - **Dashboard:** the `pages` job shows a link once Pages is enabled.
   - **Repo:** a commit named "Daily update <date>" adds the new signals and `activity/<date>.md`.

Episodes older than 4 days are ignored, so the first test run may find nothing new for some shows. That is normal.

## What each message in the report means

| Message | What to do |
|---|---|
| `ANTHROPIC_API_KEY is not set` | Step 1 is missing or the secret name is misspelled. |
| `feed has no transcript and DEEPGRAM_API_KEY is not set` | Do step 2. The episode is retried on the next run. |
| `401` / `authentication_error` | The key is wrong or was regenerated. Create a new one and replace the secret. |
| `credit balance is too low` | Add credit in the Anthropic console (step 1.2). |
| `skipped: too short` | A clip, not a full episode. Ignored on purpose. |
| `feed not found` | Set `feed_url` for that show in `sources.yaml`. |

## Changing or removing a key later

On the secrets page, click the pencil icon next to a secret to replace its value (you can't view the old one), or the trash icon to delete it. Replacing a key takes effect on the next run.

## Optional: adding anything that isn't a podcast

Earnings calls, filings and letters have no feed. Save the transcript as a `.txt` or `.pdf` and upload it to the `inbox/` folder in the repo (**Add file → Upload files** on GitHub). The next daily run processes it and moves it to `inbox/processed/`. See `inbox/README.md` for the optional header lines (name, date, source).
