#!/usr/bin/env python3
"""
Discord webhook auto-editing updater for Roblox Group Scanner.
- Generates embed exactly as requested:
  Title: Admin Tracker, Color: 3168504
  Description:
    - <:roblox:1551329066337050754> [DisplayName (@Username)](https://www.roblox.com/users/ID/profile)
      - Playing: [Game Name](https://www.roblox.com/games/PLACEID/Game-Name)
    - ... (target game first, then other games)
    - Playing: **Unknown** - (Must Follow/Game Status Hidden) if hidden
    -# Last updated: <t:UNIX:R>

- First run: POST ?wait=true -> gets message_id, saves to discord_message_id.txt
- Next runs: PATCH /messages/{id} to edit same embed (no spam)

Usage:
  python discord_updater.py roblox_scan_results.json --webhook https://discord.com/api/webhooks/... --message-id-file discord_message_id.txt
  # or via env: DISCORD_WEBHOOK, DISCORD_MESSAGE_ID
"""
import argparse
import json
import os
import sys
import time
import re
import requests
from pathlib import Path

ROBLOX_EMOJI = "<:roblox:1551329066337050754>"
DEFAULT_COLOR = 3168504  # 0x305778
TARGET_GAME_URL = "https://www.roblox.com/games/74205509034203/The-Hunt-Roblox-20"

def slugify(name: str) -> str:
    # Convert game name to URL slug: "Natural Disaster Survival" -> "Natural-Disaster-Survival"
    name = re.sub(r'[^a-zA-Z0-9]+', '-', name.strip())
    name = re.sub(r'-+', '-', name).strip('-')
    return name or "Game"

def get_game_link(place_id, universe_id, last_location: str):
    """Return (display_name, url) for game. If hidden, return (None, None)."""
    # If we have placeId, use it
    if place_id:
        # Use lastLocation as name if available, else try to keep generic
        name = last_location.strip() if last_location and last_location.strip() else "Game"
        # If lastLocation is empty but we have placeId, try to use name anyway
        # For target game, keep exact name
        safe_name = slugify(name) if name != "Game" else "Game"
        url = f"https://www.roblox.com/games/{place_id}/{safe_name}"
        # For known target, use exact URL user gave (better)
        if str(place_id) == "74205509034203":
            url = TARGET_GAME_URL
            name = "The Hunt: Roblox 20"
        return name, url
    if universe_id:
        # Fallback to universe link via place? Use universeId as place fallback
        name = last_location.strip() if last_location else "Game"
        # Can't directly link universe, use games page with universe? Use place fallback
        url = f"https://www.roblox.com/games/{universe_id}/Game"
        return name, url
    # Hidden
    return None, None

def build_description(data: dict) -> str:
    meta = data.get("meta", {})
    target = data.get("target_game_players", [])
    other = data.get("other_game_players", [])
    # Combine prioritized: target first, then other
    combined = target + other

    lines = []
    if not combined:
        # No one in any game
        lines.append("No admins currently in-game.")
    else:
        for u in combined:
            display = u.get("displayName") or u.get("username") or "Unknown"
            username = u.get("username") or "Unknown"
            user_id = u.get("userId")
            profile = f"https://www.roblox.com/users/{user_id}/profile" if user_id else "https://www.roblox.com/"
            # Playing part
            place_id = u.get("placeId")
            root_place = u.get("rootPlaceId")
            universe_id = u.get("universeId")
            last_loc = u.get("lastLocation") or ""
            # Prefer rootPlaceId if placeId null
            effective_place = place_id or root_place
            game_name, game_url = get_game_link(effective_place, universe_id, last_loc)

            if game_name and game_url:
                # Escape brackets in game name for markdown
                safe_game = game_name.replace("[", "\\[").replace("]", "\\]")
                playing = f"[{safe_game}]({game_url})"
            else:
                playing = "**Unknown** - (Must Follow/Game Status Hidden)"

            # Exact format user requested:
            # - <:roblox:ID> [DisplayName (@Username)](profile) \n - Playing: [Game](url)
            line = f"- {ROBLOX_EMOJI} [{display} (@{username})]({profile})\n - Playing: {playing}"
            lines.append(line)

    # Last updated Discord timestamp: <t:UNIX:R> relative
    now = int(time.time())
    lines.append(f"-# Last updated: <t:{now}:R>")

    desc = "\n".join(lines)
    # Discord limit 4096 for description, truncate if needed
    if len(desc) > 4000:
        # Keep header + last updated + truncate middle
        truncated = desc[:3900] + f"\n... and {len(combined) - 10} more\n-# Last updated: <t:{now}:R>"
        desc = truncated
    return desc

def load_message_id(path: str) -> str:
    p = Path(path)
    if p.exists():
        try:
            txt = p.read_text(encoding="utf-8").strip()
            # File may contain just id or json
            if txt.isdigit():
                return txt
            try:
                j = json.loads(txt)
                if isinstance(j, dict) and "id" in j:
                    return str(j["id"])
                if isinstance(j, str) and j.isdigit():
                    return j
            except:
                pass
            # Take first line that looks like id
            for line in txt.split():
                if line.strip().isdigit():
                    return line.strip()
            return txt.strip().split()[0] if txt else ""
        except:
            return ""
    return ""

def save_message_id(path: str, msg_id: str):
    Path(path).write_text(str(msg_id).strip(), encoding="utf-8")
    print(f"Saved message ID {msg_id} to {path}")

def send_or_edit(webhook_url: str, embed: dict, message_id_file: str):
    webhook_url = webhook_url.strip()
    if not webhook_url:
        webhook_url = os.environ.get("DISCORD_WEBHOOK", "") or os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        print("No webhook URL provided (arg --webhook or env DISCORD_WEBHOOK)", file=sys.stderr)
        sys.exit(1)

    # Ensure webhook has ?wait=true for POST to get id
    # Use webhook's existing name/avatar (don't override with username/avatar_url)
    payload = {"embeds": [embed]}

    existing_id = load_message_id(message_id_file) if message_id_file else ""
    if existing_id:
        print(f"Found existing message ID {existing_id}, trying to edit...")
        # Try PATCH
        # Webhook edit URL: {webhook}/messages/{message_id}
        # Need to handle webhook URL that may already have ?wait=true or other query
        base = webhook_url.split("?")[0]
        edit_url = f"{base}/messages/{existing_id}"
        # Preserve query? For edit, don't need ?wait
        try:
            r = requests.patch(edit_url, json=payload, timeout=15)
            print(f"PATCH {edit_url} -> {r.status_code}")
            if r.status_code in (200, 204):
                print("Edited existing message successfully.")
                # Update timestamp file if needed (id unchanged)
                return existing_id
            else:
                print(f"Edit failed ({r.status_code}): {r.text[:500]}")
                if r.status_code == 404:
                    print("Message not found (404), will create new one...")
                    existing_id = ""  # fallback to POST
                else:
                    # For other errors, still try to show but not fallback?
                    # e.g., 429 ratelimit -> wait and retry once
                    if r.status_code == 429:
                        try:
                            j = r.json()
                            retry_after = j.get("retry_after", 5)
                            print(f"Rate limited, retry after {retry_after}s")
                            time.sleep(float(retry_after) + 1)
                            r2 = requests.patch(edit_url, json=payload, timeout=15)
                            if r2.status_code in (200, 204):
                                print("Edit succeeded after retry.")
                                return existing_id
                        except:
                            pass
                    # Don't exit, try POST as fallback only on 404
                    if r.status_code != 404:
                        print("Edit failed, exiting without creating duplicate.")
                        sys.exit(1)
        except Exception as e:
            print(f"PATCH exception: {e}", file=sys.stderr)
            sys.exit(1)

    if not existing_id:
        print("Creating new webhook message...")
        # POST with ?wait=true to get message id back
        separator = "&" if "?" in webhook_url else "?"
        post_url = f"{webhook_url}{separator}wait=true"
        try:
            r = requests.post(post_url, json=payload, timeout=15)
            print(f"POST {post_url.split('?')[0]}/...?wait=true -> {r.status_code}")
            if r.status_code in (200, 204):
                try:
                    j = r.json()
                    msg_id = str(j.get("id", ""))
                    if msg_id:
                        print(f"Created message ID {msg_id}")
                        if message_id_file:
                            save_message_id(message_id_file, msg_id)
                        return msg_id
                    else:
                        print("POST succeeded but no ID in response (maybe webhook without wait?).")
                        print(r.text[:1000])
                        return ""
                except:
                    print(f"POST response not JSON: {r.text[:500]}")
                    return ""
            else:
                print(f"POST failed {r.status_code}: {r.text[:1000]}")
                if r.status_code == 429:
                    print("Rate limited on POST")
                sys.exit(1)
        except Exception as e:
            print(f"POST exception: {e}", file=sys.stderr)
            sys.exit(1)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Discord webhook updater - edit same embed every 5 min")
    ap.add_argument("json_file", nargs="?", default="roblox_scan_results.json", help="Merged scan JSON")
    ap.add_argument("--webhook", type=str, default="", help="Discord webhook URL (or env DISCORD_WEBHOOK)")
    ap.add_argument("--message-id-file", type=str, default="discord_message_id.txt", help="File to store message ID")
    ap.add_argument("--dry-run", action="store_true", help="Just print embed, don't send")
    args = ap.parse_args()

    data_path = Path(args.json_file)
    if not data_path.exists():
        print(f"JSON not found: {args.json_file}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(data_path.read_text(encoding="utf-8"))

    # Also handle case where json is sharded and we need to merge? If user passes shard_0.json, we still handle
    # Build embed
    desc = build_description(data)
    embed = {
        "title": "Admin Tracker",
        "color": DEFAULT_COLOR,
        "description": desc
    }

    if args.dry_run:
        print(json.dumps({"embeds": [embed]}, indent=2))
        sys.exit(0)

    webhook = args.webhook or os.environ.get("DISCORD_WEBHOOK", "")
    msg_id_file = args.message_id_file

    # If webhook is empty, try to load from file? No, need webhook
    send_or_edit(webhook, embed, msg_id_file)
