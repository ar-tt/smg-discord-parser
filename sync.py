#!/usr/bin/env python3
"""Poll Discord channels over the REST API and append messages to a Google Sheet.

One row per message. Designed to run on a schedule (e.g. GitHub Actions cron).
A per-channel cursor in state.json means each run only picks up what's new.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

DISCORD_API = "https://discord.com/api/v10"
STATE_FILE = Path("state.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Discord snowflake IDs encode a creation timestamp measured from 2015-01-01.
DISCORD_EPOCH_MS = 1_420_070_400_000

HEADERS = [
    "Timestamp (UTC)",
    "Channel",
    "Author",
    "Message",
    "Attachments",
    "Link",
    "Message ID",
]

# Discord message types worth archiving: 0 = default, 19 = inline reply.
KEEP_TYPES = {0, 19}

# Sheets caps a single cell at 50,000 characters.
CELL_LIMIT = 49_000


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if isinstance(value, str):
        # Pasted secrets often carry a trailing newline, which is illegal in
        # an HTTP header and produces a confusing InvalidHeader crash.
        value = value.strip()
    if required and not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            print("state.json was unreadable; starting fresh", file=sys.stderr)
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------
# Snowflake / cutoff handling
# --------------------------------------------------------------------------

def snowflake_for(moment):
    """Lowest Discord message ID that could have been created at `moment`."""
    ms = int(moment.timestamp() * 1000) - DISCORD_EPOCH_MS
    if ms < 0:
        return 0
    return ms << 22


def parse_cutoff(raw):
    """Accept YYYY-MM-DD or a full ISO timestamp. Returns an aware datetime."""
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        sys.exit(
            f"ARCHIVE_SINCE is not a valid date: {raw!r}. "
            "Use YYYY-MM-DD, for example 2026-09-01."
        )
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

def discord_get(session, path, params=None):
    """GET against the Discord API with retry on rate limits and 5xx."""
    last_error = None
    for attempt in range(5):
        response = session.get(f"{DISCORD_API}{path}", params=params, timeout=30)

        if response.status_code == 429:
            wait = float(response.json().get("retry_after", 5))
            print(f"    rate limited, waiting {wait:.1f}s")
            time.sleep(wait + 0.5)
            continue

        if response.status_code >= 500:
            last_error = f"{response.status_code} from Discord"
            time.sleep(2**attempt)
            continue

        if response.status_code == 403:
            raise PermissionError(
                f"Discord returned 403 for {path}. The bot is probably missing "
                "'View Channel' or 'Read Message History' on this channel."
            )

        response.raise_for_status()
        return response.json()

    raise RuntimeError(f"Discord request failed after retries ({last_error}): {path}")


def channel_info(session, channel_id):
    """Return (channel name, guild id). Falls back gracefully."""
    try:
        data = discord_get(session, f"/channels/{channel_id}")
        return data.get("name", channel_id), data.get("guild_id", "")
    except Exception as exc:  # noqa: BLE001 - cosmetic, never fatal
        print(f"    could not read channel details: {exc}")
        return channel_id, ""


def fetch_messages_after(session, channel_id, after_id, cap):
    """Return up to `cap` messages newer than after_id, oldest first."""
    collected = []
    cursor = after_id

    while len(collected) < cap:
        batch = discord_get(
            session,
            f"/channels/{channel_id}/messages",
            params={"after": cursor, "limit": 100},
        )
        if not batch:
            break

        batch.sort(key=lambda m: int(m["id"]))
        collected.extend(batch)
        cursor = batch[-1]["id"]

        if len(batch) < 100:
            break
        time.sleep(0.3)

    return collected[:cap]


def latest_message_id(session, channel_id):
    batch = discord_get(session, f"/channels/{channel_id}/messages", params={"limit": 1})
    return batch[0]["id"] if batch else "0"


# --------------------------------------------------------------------------
# Row building
# --------------------------------------------------------------------------

def build_row(message, channel_name, channel_id, guild_id):
    stamp = datetime.fromisoformat(message["timestamp"]).astimezone(timezone.utc)
    author = message["author"].get("global_name") or message["author"]["username"]

    body = (message.get("content") or "").strip()
    if not body:
        for embed in message.get("embeds", []):
            label = embed.get("title") or embed.get("description") or embed.get("url")
            if label:
                body = f"[embed] {label}"
                break

    attachments = " | ".join(
        a.get("url", "") for a in message.get("attachments", []) if a.get("url")
    )

    link = (
        f"https://discord.com/channels/{guild_id}/{channel_id}/{message['id']}"
        if guild_id
        else ""
    )

    return [
        stamp.strftime("%Y-%m-%d %H:%M:%S"),
        channel_name,
        author,
        body[:CELL_LIMIT],
        attachments[:CELL_LIMIT],
        link,
        message["id"],
    ]


# --------------------------------------------------------------------------
# Google Sheets
# --------------------------------------------------------------------------

def sheets_client():
    raw = env("GOOGLE_CREDENTIALS", required=True)
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit("GOOGLE_CREDENTIALS is not valid JSON. Paste the whole key file.")
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def ensure_headers(sheets, sheet_id, tab):
    """Write the header row if the sheet is empty."""
    existing = (
        sheets.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"{tab}!A1:G1")
        .execute()
        .get("values", [])
    )
    if existing:
        return

    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{tab}!A1",
        valueInputOption="RAW",
        body={"values": [HEADERS]},
    ).execute()
    print("    wrote header row")


def append_rows(sheets, sheet_id, tab, rows):
    if not rows:
        return
    sheets.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{tab}!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


# --------------------------------------------------------------------------

def main():
    token = env("DISCORD_BOT_TOKEN", required=True)
    sheet_id = env("GOOGLE_SHEET_ID", required=True)
    raw_channels = env("DISCORD_CHANNEL_IDS", required=True)
    tab = env("SHEET_TAB_NAME", "Sheet1")
    mode = env("SYNC_MODE", "new").strip().lower()
    cap = int(env("MAX_MESSAGES_PER_RUN", "2000"))
    cutoff = parse_cutoff(env("ARCHIVE_SINCE", "2026-09-01"))

    channel_ids = [c.strip() for c in raw_channels.split(",") if c.strip()]
    if not channel_ids:
        sys.exit("DISCORD_CHANNEL_IDS is empty.")

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordArchive (github actions, 2.0)",
        }
    )

    sheets = sheets_client()
    ensure_headers(sheets, sheet_id, tab)

    state = load_state()
    grand_total = 0

    if cutoff:
        print(f"Ignoring anything before {cutoff:%Y-%m-%d %H:%M} UTC\n")

    for channel_id in channel_ids:
        name, guild_id = channel_info(session, channel_id)
        print(f"#{name} ({channel_id})")

        cursor = state.get(channel_id)

        if cursor is None:
            if mode == "backfill":
                cursor = str(snowflake_for(cutoff)) if cutoff else "0"
                if cutoff:
                    print(f"    backfilling from {cutoff:%Y-%m-%d}")
                else:
                    print("    backfilling from the start of the channel")
            else:
                state[channel_id] = latest_message_id(session, channel_id)
                save_state(state)
                print(f"    initialised at {state[channel_id]}; history skipped")
                continue

        messages = fetch_messages_after(session, channel_id, cursor, cap)
        messages = [m for m in messages if m.get("type", 0) in KEEP_TYPES]

        if cutoff:
            floor = snowflake_for(cutoff)
            messages = [m for m in messages if int(m["id"]) >= floor]

        if not messages:
            print("    nothing new")
            continue

        rows = [build_row(m, name, channel_id, guild_id) for m in messages]
        append_rows(sheets, sheet_id, tab, rows)

        state[channel_id] = messages[-1]["id"]
        save_state(state)

        grand_total += len(messages)
        print(f"    appended {len(messages)} row(s)")

    if grand_total >= cap:
        print(
            f"\nHit the {cap}-message cap. More history remains — "
            "re-run backfill until this stops appearing."
        )

    print(f"\nDone. {grand_total} row(s) written.")


if __name__ == "__main__":
    main()
