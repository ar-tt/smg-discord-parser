#!/usr/bin/env python3
"""Poll Discord channels over the REST API and append new messages to a Google Doc.

Designed to run on a schedule (e.g. GitHub Actions cron). Keeps a per-channel
cursor in state.json so each run only picks up what it hasn't seen before.
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
SCOPES = ["https://www.googleapis.com/auth/documents"]

# Google Docs rejects very large single inserts; split long appends into chunks.
CHUNK_CHARS = 40_000

# Discord message types worth archiving: 0 = default, 19 = inline reply.
KEEP_TYPES = {0, 19}


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
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


def channel_label(session, channel_id):
    try:
        return discord_get(session, f"/channels/{channel_id}").get("name", channel_id)
    except Exception as exc:  # noqa: BLE001 - label is cosmetic, never fatal
        print(f"    could not read channel name: {exc}")
        return channel_id


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
# Formatting
# --------------------------------------------------------------------------

def format_message(message, channel_name):
    stamp = datetime.fromisoformat(message["timestamp"]).astimezone(timezone.utc)
    author = message["author"].get("global_name") or message["author"]["username"]

    lines = [f"[{stamp:%Y-%m-%d %H:%M} UTC] #{channel_name} — {author}"]

    content = (message.get("content") or "").strip()
    if content:
        lines.append(content)

    for attachment in message.get("attachments", []):
        lines.append(f"    [file] {attachment.get('filename')} — {attachment.get('url')}")

    for embed in message.get("embeds", []):
        label = embed.get("title") or embed.get("url")
        if label:
            lines.append(f"    [embed] {label}")

    if len(lines) == 1:
        lines.append(
            "    (empty — if this happens for every message, the Message Content "
            "intent is not enabled)"
        )

    return "\n".join(lines) + "\n\n"


# --------------------------------------------------------------------------
# Google Docs
# --------------------------------------------------------------------------

def docs_client():
    raw = env("GOOGLE_CREDENTIALS", required=True)
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit("GOOGLE_CREDENTIALS is not valid JSON. Paste the whole key file.")
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("docs", "v1", credentials=creds, cache_discovery=False)


def doc_end_index(docs, doc_id):
    doc = docs.documents().get(documentId=doc_id).execute()
    # The trailing newline of the body segment is not a valid insert target.
    return doc["body"]["content"][-1]["endIndex"] - 1


def append_to_doc(docs, doc_id, text):
    if not text:
        return

    for start in range(0, len(text), CHUNK_CHARS):
        chunk = text[start : start + CHUNK_CHARS]
        index = doc_end_index(docs, doc_id)
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={
                "requests": [
                    {"insertText": {"location": {"index": index}, "text": chunk}}
                ]
            },
        ).execute()
        time.sleep(0.5)


# --------------------------------------------------------------------------

def main():
    token = env("DISCORD_BOT_TOKEN", required=True)
    doc_id = env("GOOGLE_DOC_ID", required=True)
    raw_channels = env("DISCORD_CHANNEL_IDS", required=True)
    mode = env("SYNC_MODE", "new").strip().lower()
    cap = int(env("MAX_MESSAGES_PER_RUN", "500"))

    channel_ids = [c.strip() for c in raw_channels.split(",") if c.strip()]
    if not channel_ids:
        sys.exit("DISCORD_CHANNEL_IDS is empty.")

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordArchive (github actions, 1.0)",
        }
    )

    docs = docs_client()
    state = load_state()
    grand_total = 0

    for channel_id in channel_ids:
        name = channel_label(session, channel_id)
        print(f"#{name} ({channel_id})")

        cursor = state.get(channel_id)

        if cursor is None:
            if mode == "backfill":
                cursor = "0"
                print("    no cursor — backfilling from the start of the channel")
            else:
                state[channel_id] = latest_message_id(session, channel_id)
                save_state(state)
                print(f"    initialised at {state[channel_id]}; history skipped")
                continue

        messages = fetch_messages_after(session, channel_id, cursor, cap)
        messages = [m for m in messages if m.get("type", 0) in KEEP_TYPES]

        if not messages:
            print("    nothing new")
            continue

        text = "".join(format_message(m, name) for m in messages)
        append_to_doc(docs, doc_id, text)

        state[channel_id] = messages[-1]["id"]
        save_state(state)

        grand_total += len(messages)
        print(f"    appended {len(messages)} message(s)")

    if grand_total >= cap:
        print(
            f"\nHit the {cap}-message cap. More history remains — "
            "re-run until this stops appearing."
        )

    print(f"\nDone. {grand_total} message(s) written.")


if __name__ == "__main__":
    main()
