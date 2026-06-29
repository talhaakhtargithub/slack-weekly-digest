# Slack Weekly Digest

Collects every **Monday–Friday** message from a source Slack channel and posts the
whole week combined as a **single downloadable `.txt` file** into a destination
channel. Runs automatically every **Saturday at 03:00 Asia/Karachi (PKT)** via
GitHub Actions.

## What it does

- Pulls all messages (including thread replies) for the Mon–Fri window of the
  week that just ended, in the `Asia/Karachi` timezone.
- Resolves user IDs to real names and groups messages by day.
- Uploads the combined digest as **one** file snippet (Slack splits long chat
  messages, so a file keeps it as a single item) using Slack's current
  `files.getUploadURLExternal` + `files.completeUploadExternal` flow.
- **Retention:** after posting, deletes any weekly log file older than
  `--max-age-days` (default **14 days**) so the channel keeps roughly the last
  two weeks. Set `MAX_AGE_DAYS` in the workflow to change it.

## Setup

### 1. Slack app / bot token

Create a Slack app with a **bot token** (`xoxb-...`) that has these scopes:

- `channels:history` (or `groups:history` for private channels) — read the source channel
- `chat:write` — post to the destination channel
- `users:read` — resolve real names
- `files:write` — upload the digest file

Invite the bot to **both** the source and destination channels.

### 2. GitHub Actions secret

In this repo: **Settings → Secrets and variables → Actions → New repository secret**

- Name: `SLACK_BOT_TOKEN`
- Value: your `xoxb-...` token

> The token is **never** stored in this repository — only as an encrypted GitHub
> Actions secret.

### 3. Channels

The source and destination channel IDs are set in
[`.github/workflows/weekly-digest.yml`](.github/workflows/weekly-digest.yml)
(`SOURCE_CHANNEL` / `DEST_CHANNEL`). Edit them there if they change.

## Schedule

`0 22 * * 5` (UTC) = **Friday 22:00 UTC** = **Saturday 03:00 Asia/Karachi**.

## Run manually

Go to the **Actions** tab → **Weekly Slack Digest** → **Run workflow**. You can
set `week_offset` to `1` to re-post the previous week.

## Run locally

```bash
SLACK_TOKEN=xoxb-... python3 slack_weekly_digest.py \
  --source C0ASUA49PDY --dest C0BDW158FKP --tz Asia/Karachi --week-offset 0

# preview without posting:
SLACK_TOKEN=xoxb-... python3 slack_weekly_digest.py \
  --source C0ASUA49PDY --dest C0BDW158FKP --tz Asia/Karachi --week-offset 0 --dry-run
```
