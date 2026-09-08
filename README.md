# Discord to Google Doc

Archives messages from one or more Discord channels into a Google Doc. Runs on
GitHub Actions every 15 minutes at no cost.

It polls Discord's REST API rather than holding an open gateway connection, so
nothing has to stay running on your own hardware. A `state.json` file committed
back to the repo tracks the last message seen in each channel.

## What you need to collect

| Item | Where it comes from |
|---|---|
| Discord bot token | Discord Developer Portal |
| Discord channel ID(s) | Right-click the channel in Discord |
| Google service account JSON | Google Cloud Console |
| Google Doc ID | The doc's URL |
| A GitHub repository | Make it private |

---

## 1. Discord

1. Go to <https://discord.com/developers/applications> and click **New
   Application**. Name it anything.
2. Open the **Bot** tab. Under **Privileged Gateway Intents**, turn on
   **Message Content Intent** and save. Without this, every message comes back
   with empty content.
3. Still on the Bot tab, click **Reset Token**, then copy the token. It is shown
   once. This token can read every channel the bot can see — treat it like a
   password.
4. Go to **OAuth2 → URL Generator**. Tick the `bot` scope, then tick
   **View Channels** and **Read Message History** under Bot Permissions.
5. Open the generated URL and add the bot to your server. You need **Manage
   Server** permission to do this.
6. Confirm the bot actually has access to the channels you care about. If a
   channel has permission overrides, add the bot's role explicitly.

### Getting channel IDs

In Discord: **User Settings → Advanced → Developer Mode** on. Then right-click
any channel and choose **Copy Channel ID**. It's an 18-19 digit number.

---

## 2. Google

1. At <https://console.cloud.google.com>, create a project.
2. **APIs & Services → Library** → search for **Google Docs API** → **Enable**.
   No billing account is required.
3. **APIs & Services → Credentials → Create Credentials → Service Account**.
   Give it a name; skip the optional role and access steps.
4. Open the new service account → **Keys** tab → **Add Key → Create new key →
   JSON**. A file downloads. Keep it; you'll paste its contents into GitHub.
5. Create the Google Doc that will hold the archive. Copy the service account's
   email address (it looks like `something@project-id.iam.gserviceaccount.com`)
   and **share the doc with it as an Editor**.

   This step is the one people miss. The service account is a separate identity
   from your own Google account and cannot see your documents by default.

6. The doc ID is the long string in its URL:
   `https://docs.google.com/document/d/`**`THIS_PART`**`/edit`

---

## 3. GitHub

Create a **private** repository and push these files to it.

Then under **Settings → Secrets and variables → Actions**:

**Secrets** tab → New repository secret:

- `DISCORD_BOT_TOKEN` — the bot token from step 1.3
- `GOOGLE_CREDENTIALS` — the entire contents of the downloaded JSON key file,
  pasted as-is including the braces

**Variables** tab → New repository variable:

- `GOOGLE_DOC_ID` — from step 2.6
- `DISCORD_CHANNEL_IDS` — one ID, or several separated by commas with no spaces

---

## 4. First run

Go to **Actions → Discord to Google Doc → Run workflow**.

- Choose **new** to start archiving from this moment forward. The first run
  records where each channel currently is and writes nothing.
- Choose **backfill** to pull existing history. It grabs up to 500 messages per
  run, so for a busy channel just run it repeatedly until the log says nothing
  is left.

After the first manual run, the 15-minute schedule takes over.

---

## Things to know

**Scheduled workflows get disabled after 60 days of repo inactivity.** GitHub
emails you when this happens; visit the repo and re-enable it. The cursor
commits usually count as activity, but don't rely on it.

**Google Docs cap out near 1 million characters.** For a high-traffic channel
you'll eventually hit this. If that's a concern, switch the destination to
Google Sheets — one row per message, and it stays searchable.

**Polling means up to 15 minutes of delay,** and edits or deletions after the
fact won't be reflected. The archive captures messages as they were when first
seen.

**Deleted messages are gone.** If a message is deleted inside the polling
window, it never reaches the doc.

**Rate limits** are handled automatically — the script backs off and retries
on 429s from Discord.

## Running locally

```bash
pip install -r requirements.txt

export DISCORD_BOT_TOKEN="..."
export GOOGLE_CREDENTIALS="$(cat service-account.json)"
export GOOGLE_DOC_ID="..."
export DISCORD_CHANNEL_IDS="123456789012345678"
export SYNC_MODE="backfill"

python sync.py
```

Never commit `service-account.json` or the bot token to the repo.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Every message body is empty | Message Content Intent is off in the Developer Portal |
| `403` from Discord | Bot lacks View Channel or Read Message History on that channel |
| `404` from Google | The doc wasn't shared with the service account email |
| `Missing required environment variable` | A secret or variable name is misspelled |
| Nothing happens on schedule | Workflow disabled after 60 days, or the first manual run was never done |
