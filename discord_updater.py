#!/usr/bin/env python3
"""
Discord webhook auto-editing updater for Roblox Group Scanner.
- Configurable title/color/emoji per tracker (Admin, Video Star, Developer)
- Generates embed:
  Title: <title>, Color: <color>
  Description:
    - <emoji> [DisplayName (@Username)](https://www.roblox.com/users/ID/profile)
      - Playing: [Game Name](https://www.roblox.com/games/PLACEID/Game-Name)
    - ... (Hunt first, then Unknown)
    - Playing: **Unknown** (Must Follow — game hidden even when Following) if presenceType 1/2/3 but game hidden, else **Unknown** (don't assume Hunt)
    -# Last updated: <t:UNIX:R>

- Filters: excludes anyone confirmed NOT playing Hunt — only Hunt + Unknown remain
- Pagination: if description >4000 chars, splits into 2+ embeds (up to 10), each title/color. Auto-reverts to 1 when fits. Total 6000.
- First run: POST ?wait=true -> gets message_id, saves to file
- Next runs: PATCH /messages/{id} to edit same message
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
DEFAULT_COLOR = 3168504  # Admin Tracker #305778
TARGET_GAME_URL = "https://www.roblox.com/games/74205509034203/The-Hunt-Roblox-20"
TARGET_PLACE_ID = "74205509034203"
MAX_DESC = 4000

def parse_color(val):
    val = str(val).strip()
    if val.startswith("#"):
        val = val[1:]
    # Try hex
    try:
        if len(val) <= 6 and all(c in "0123456789abcdefABCDEF" for c in val):
            return int(val, 16)
    except:
        pass
    try:
        return int(val)
    except:
        return DEFAULT_COLOR

def slugify(name: str) -> str:
    name = re.sub(r'[^a-zA-Z0-9]+', '-', name.strip())
    name = re.sub(r'-+', '-', name).strip('-')
    return name or "Game"

def get_game_link(place_id, universe_id, last_location: str):
    if place_id:
        name = last_location.strip() if last_location and last_location.strip() else "Game"
        safe_name = slugify(name) if name != "Game" else "Game"
        url = f"https://www.roblox.com/games/{place_id}/{safe_name}"
        if str(place_id) == TARGET_PLACE_ID:
            url = TARGET_GAME_URL
            name = "The Hunt: Roblox 20"
        return name, url
    if universe_id:
        name = last_location.strip() if last_location else "Game"
        url = f"https://www.roblox.com/games/{universe_id}/Game"
        return name, url
    return None, None

def build_embeds(data: dict, title="Admin Tracker", color=DEFAULT_COLOR, emoji=ROBLOX_EMOJI):
    meta = data.get("meta", {})
    target = data.get("target_game_players", [])
    other = data.get("other_game_players", [])

    # Filter: exclude confirmed NOT playing Hunt — Keep Hunt + Unknown
    filtered = []
    for u in target + other:
        pid = u.get("placeId") or u.get("rootPlaceId")
        uid = u.get("universeId")
        if pid is not None and str(pid) == TARGET_PLACE_ID:
            filtered.append(u)
        elif pid is None and uid is None:
            filtered.append(u)
        else:
            continue

    now = int(time.time())
    footer = f"-# Last updated: <t:{now}:R>"

    # Empty case — use tracker-specific no-players line but keep structure
    if not filtered:
        # Generic no-players (keep for all trackers)
        desc = f"No admins currently in-game.\n{footer}"
        # For Video Stars / Developers we could customize, but keep as is per same style request
        # If title is not Admin, still show same line (user said keep same embed style)
        return [{"title": title, "color": color, "description": desc}]

    lines = []
    for u in filtered:
        display = u.get("displayName") or u.get("username") or "Unknown"
        username = u.get("username") or "Unknown"
        user_id = u.get("userId")
        profile = f"https://www.roblox.com/users/{user_id}/profile" if user_id else "https://www.roblox.com/"
        place_id = u.get("placeId")
        root_place = u.get("rootPlaceId")
        universe_id = u.get("universeId")
        last_loc = u.get("lastLocation") or ""
        effective_place = place_id or root_place
        game_name, game_url = get_game_link(effective_place, universe_id, last_loc)

        if game_name and game_url:
            safe_game = game_name.replace("[", "\\[").replace("]", "\\]")
            playing = f"[{safe_game}]({game_url})"
        else:
            # Game hidden — don't assume Hunt. User 92501615 proved InGame hidden even when Following stays hidden.
            ptype = u.get("presenceType")
            try:
                ptype_int = int(ptype) if ptype is not None else None
            except:
                ptype_int = None
            if ptype_int in (1, 2, 3):
                playing = "**Unknown** (Must Follow — game hidden even when Following)"
            else:
                playing = "**Unknown**"

        line = f"- {emoji} [{display} (@{username})]({profile})\n  - Playing: {playing}"
        lines.append(line)

    TOTAL_LIMIT = 6000
    filtered_lines = []
    running_total = 0
    truncated = 0
    for line in lines:
        ll = len(line) + 1
        if running_total + ll + len(footer) + 1 > TOTAL_LIMIT:
            truncated = len(lines) - len(filtered_lines)
            break
        filtered_lines.append(line)
        running_total += ll
    lines = filtered_lines

    if not lines:
        if truncated > 0:
            desc = f"No admins currently in-game.\n{footer}\n-# +{truncated} more not shown (6000 limit)"
        else:
            desc = f"No admins currently in-game.\n{footer}"
        return [{"title": title, "color": color, "description": desc}]

    chunks = []
    cur = []
    cur_len = 0
    for line in lines:
        ll = len(line) + 1
        if cur and cur_len + ll > MAX_DESC:
            chunks.append(cur)
            cur = []
            cur_len = 0
        cur.append(line)
        cur_len += ll
    if cur:
        chunks.append(cur)

    embeds = []
    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        desc = "\n".join(chunk)
        if is_last:
            if len(desc) + 1 + len(footer) > MAX_DESC:
                while chunk and len("\n".join(chunk)) + 1 + len(footer) > MAX_DESC:
                    last_line = chunk.pop()
                    if chunk:
                        embeds.append({"title": title, "color": color, "description": "\n".join(chunk)})
                        chunk = [last_line]
                    else:
                        chunk = [last_line[:MAX_DESC - len(footer) - 10] + "..."]
                        break
                desc = "\n".join(chunk) + f"\n{footer}"
                embeds.append({"title": title, "color": color, "description": desc})
                continue
            else:
                desc = f"{desc}\n{footer}" if desc else footer
        embeds.append({"title": title, "color": color, "description": desc})

    if truncated > 0:
        notice = f"\n-# +{truncated} more not shown (6000 char total limit)"
        last = embeds[-1]
        if len(last["description"]) + len(notice) > MAX_DESC:
            last["description"] = last["description"][:MAX_DESC - len(notice) - 1] + notice
        elif sum(len(e["description"]) for e in embeds) + len(notice) > TOTAL_LIMIT:
            last["description"] = last["description"][:TOTAL_LIMIT - sum(len(e["description"]) for e in embeds[:-1]) - len(notice) - 1] + notice
        else:
            last["description"] += notice

    if len(embeds) > 10:
        embeds = embeds[:10]
        last = embeds[-1]
        notice = f"\n... and more not shown (limit 10 embeds)"
        if len(last["description"]) + len(notice) > MAX_DESC:
            last["description"] = last["description"][:MAX_DESC - len(notice) - 1] + notice
        else:
            last["description"] += notice

    return embeds

def load_message_id(path: str) -> str:
    p = Path(path)
    if p.exists():
        try:
            txt = p.read_text(encoding="utf-8").strip()
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

def send_or_edit(webhook_url: str, embeds: list, message_id_file: str):
    webhook_url = webhook_url.strip()
    if not webhook_url:
        webhook_url = os.environ.get("DISCORD_WEBHOOK", "") or os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        print("No webhook URL provided (arg --webhook or env DISCORD_WEBHOOK)", file=sys.stderr)
        sys.exit(1)
    payload = {"embeds": embeds}
    existing_id = load_message_id(message_id_file) if message_id_file else ""
    if existing_id:
        print(f"Found existing message ID {existing_id}, trying to edit...")
        base = webhook_url.split("?")[0]
        edit_url = f"{base}/messages/{existing_id}"
        try:
            r = requests.patch(edit_url, json=payload, timeout=15)
            print(f"PATCH {edit_url} -> {r.status_code}")
            if r.status_code in (200, 204):
                print("Edited existing message successfully.")
                return existing_id
            else:
                print(f"Edit failed ({r.status_code}): {r.text[:500]}")
                if r.status_code == 404:
                    print("Message not found (404), will create new one...")
                    existing_id = ""
                else:
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
                    if r.status_code != 404:
                        print("Edit failed, exiting without creating duplicate.")
                        sys.exit(1)
        except Exception as e:
            print(f"PATCH exception: {e}", file=sys.stderr)
            sys.exit(1)
    if not existing_id:
        print("Creating new webhook message...")
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
                        print("POST succeeded but no ID in response.")
                        print(r.text[:1000])
                        return ""
                except:
                    print(f"POST response not JSON: {r.text[:500]}")
                    return ""
            else:
                print(f"POST failed {r.status_code}: {r.text[:1000]}")
                sys.exit(1)
        except Exception as e:
            print(f"POST exception: {e}", file=sys.stderr)
            sys.exit(1)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Discord webhook updater - edit same message every 5 min")
    ap.add_argument("json_file", nargs="?")
    ap.add_argument("--webhook", type=str, default="", help="Discord webhook URL (or env DISCORD_WEBHOOK)")
    ap.add_argument("--message-id-file", type=str, default="discord_message_id.txt", help="File to store message ID")
    ap.add_argument("--title", type=str, default="Admin Tracker", help="Embed title")
    ap.add_argument("--color", type=str, default=str(DEFAULT_COLOR), help="Embed color (decimal or hex #RRGGBB)")
    ap.add_argument("--emoji", type=str, default=ROBLOX_EMOJI, help="Emoji for each line")
    ap.add_argument("--dry-run", action="store_true", help="Just print embeds, don't send")
    args = ap.parse_args()

    data_path = None
    if args.json_file:
        data_path = Path(args.json_file)
    else:
        for cand in ["roblox_scan_results.json", "shards/roblox_scan_results.json", "discord_test.json"]:
            if Path(cand).exists():
                data_path = Path(cand)
                break
        if not data_path:
            data_path = Path("roblox_scan_results.json")

    if not data_path.exists():
        print(f"JSON not found: {data_path}", file=sys.stderr)
        sys.exit(1)
    data = json.loads(data_path.read_text(encoding="utf-8"))

    color_int = parse_color(args.color)
    embeds = build_embeds(data, title=args.title, color=color_int, emoji=args.emoji)

    if args.dry_run:
        print(json.dumps({"embeds": embeds}, indent=2))
        print(f"\n# Embeds: {len(embeds)}, total chars: {sum(len(e['description']) for e in embeds)}", file=sys.stderr)
        sys.exit(0)

    webhook = args.webhook or os.environ.get("DISCORD_WEBHOOK", "")
    msg_id_file = args.message_id_file
    send_or_edit(webhook, embeds, msg_id_file)
