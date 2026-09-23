#!/usr/bin/env python3
"""
Discord webhook auto-editing updater for Roblox Group Scanner.
- Configurable title/color/emoji per tracker (Admin, Video Star, Developer)
- Generates embed:
  Title: <title>, Color: <color>
  Description:
    - <emoji> [DisplayName (@Username)](https://www.roblox.com/users/ID/profile)
      - Playing: [The Hunt: Roblox 20](https://www.roblox.com/games/74205509034203/The-Hunt-Roblox-20) (Must Follow to Join)  [if InGame Hunt via Follow]
      - Playing: [The Hunt Roblox 20](https://www.roblox.com/games/74205509034203/The-Hunt-Roblox-20) (Appearing Offline - Must Follow to Join)  [if Offline Hunt via Follow]
      - Playing: [The Hunt: Roblox 20](https://www.roblox.com/games/74205509034203/The-Hunt-Roblox-20)  [if InGame Hunt public, no Follow needed]
      - Playing: **Unknown** (Must Follow/Game Status Hidden)  [if InGame hidden NOT Following, only if playing but not visible]
    - Only Hunt (via Follow or public) + hidden InGame Unknown are shown; all else filtered per spec 2026-09-22:
      * Online(1)/InStudio(3)/missing/null -> Do not show
      * InGame hidden even when Following -> Do not show (e.g., 92501615)
      * InGame hidden NOT Following -> Unknown (Must Follow/Game Status Hidden)
      * InGame non-Hunt visible -> Do not show
      * Offline -> Do not show except Offline Hunt via Follow
    -# Last updated: <t:UNIX:R>

- Filters: ONLY Hunt visible via Following; everything else hidden
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

def empty_message(title: str) -> str:
    t = title.lower()
    if "developer" in t:
        return "No developers currently in-game."
    if "video" in t:
        return "No Video Stars currently in-game."
    if "admin" in t:
        return "No admins currently in-game."
    return "No one currently in-game."

def build_embeds(data: dict, title="Admin Tracker", color=DEFAULT_COLOR, emoji=ROBLOX_EMOJI):
    target = data.get("target_game_players", [])
    other = data.get("other_game_players", [])

    # Filter per final spec 2026-09-22 + 2026-09-22 clarification:
    # - Online(1), InStudio(3), missing/null -> Do not show (exclude)
    # - InGame hidden even when Following (isFollowing true, no placeId) -> Do not show
    # - InGame hidden NOT Following (isFollowing false, no placeId, presenceType 2) -> Keep as Unknown (Must Follow/Game Status Hidden)
    # - InGame Hunt visible (public, no Follow needed) -> Keep as plain Hunt link
    # - InGame Hunt visible when Following -> Keep as Must Follow to Join
    # - InGame non-Hunt visible -> Do not show
    # - Offline (0) -> Do not show, except Offline Hunt visible when Following -> Keep as Appearing Offline
    # - Offline non-Hunt visible -> Do not show
    filtered = []
    for u in target + other:
        pid = u.get("placeId") or u.get("rootPlaceId")
        ptype = u.get("presenceType")
        try:
            ptype_int = int(ptype) if ptype is not None else None
        except:
            ptype_int = None
        is_following = bool(u.get("isFollowing"))
        # Exclude Online, InStudio, missing/null per clarification
        if ptype_int in (1, 3) or ptype_int is None:
            continue
        # Hunt visible -> keep (both public and follow-required)
        if pid is not None and str(pid) == TARGET_PLACE_ID:
            filtered.append(u)
            continue
        # Hidden InGame cases
        if ptype_int == 2 and pid is None and u.get("universeId") is None:
            if is_following:
                # InGame hidden even when Following (e.g., 92501615) -> Do not show
                continue
            else:
                # InGame hidden NOT Following -> Keep as Unknown (Must Follow/Game Status Hidden) only if playing a game but not visible
                # This matches "ONLY IF their status shows them playing a game but the game isn't already visible"
                filtered.append(u)
                continue
        # All other Offline / hidden -> Do not show
        continue

    # Sort by playing hierarchy requested: Hunt (public) -> Hunt (Must Follow) -> Unknown
    def sort_key(u):
        pid = u.get("placeId") or u.get("rootPlaceId")
        is_following = bool(u.get("isFollowing"))
        ptype = u.get("presenceType")
        try:
            ptype_int = int(ptype) if ptype is not None else None
        except:
            ptype_int = None
        is_hunt = pid is not None and str(pid) == TARGET_PLACE_ID
        if is_hunt and not is_following and ptype_int == 2:
            prio = 0  # Playing: The Hunt: Roblox 20 (public)
        elif is_hunt and is_following:
            prio = 1  # Playing: The Hunt: Roblox 20 (Must Follow to Join) + Appearing Offline
        elif is_hunt and ptype_int == 0 and is_following:
            prio = 1
        elif is_hunt:
            prio = 0  # fallback Hunt public
        else:
            prio = 2  # Unknown
        # secondary sort alphabetically by displayName/username
        name = (u.get("displayName") or u.get("username") or "").lower()
        return (prio, name)

    filtered = sorted(filtered, key=sort_key)

    now = int(time.time())
    footer = f"-# Last updated: <t:{now}:R>"

    # Empty case — tracker-specific no-players line
    if not filtered:
        return [{"title": title, "color": color, "description": f"{empty_message(title)}\n{footer}"}]

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

        # Only Hunt is shown — per spec, determine label by presenceType
        # - InGame Hunt via Follow (isFollowing true) -> Must Follow to Join with colon
        # - Offline Hunt via Follow -> Appearing Offline - Must Follow to Join (no colon)
        # - InGame Hunt public (isFollowing false) -> plain Hunt link
        # - InGame hidden NOT Following -> Unknown (Must Follow/Game Status Hidden)
        pid_check = u.get("placeId") or u.get("rootPlaceId")
        is_following = bool(u.get("isFollowing"))
        ptype = u.get("presenceType")
        try:
            ptype_int = int(ptype) if ptype is not None else None
        except:
            ptype_int = None

        if pid_check is not None and str(pid_check) == TARGET_PLACE_ID:
            if ptype_int == 0:
                # Offline but Hunt shown via Following = Appearing Offline (no colon per spec)
                playing = f"[The Hunt Roblox 20]({TARGET_GAME_URL}) (Appearing Offline - Must Follow to Join)"
            elif is_following:
                # InGame Hunt visible when Following (requires Follow to see) -> Must Follow to Join with colon
                playing = f"[The Hunt: Roblox 20]({TARGET_GAME_URL}) (Must Follow to Join)"
            else:
                # InGame Hunt shown without being follow-checked (public) -> plain Hunt link per clarification
                playing = f"[The Hunt: Roblox 20]({TARGET_GAME_URL})"
        else:
            # Hidden InGame NOT Following -> Unknown (Must Follow/Game Status Hidden) only if playing but not visible
            playing = "**Unknown** (Must Follow/Game Status Hidden)"

        line = f"- {emoji} [{display} (@{username})]({profile})\n  - Playing: {playing}"
        lines.append(line)

    # Discord caps the sum of all embed text (titles included) at 6000; each embed repeats the title.
    # A 6000-char budget can span at most 2 description chunks, so reserve room for 2 titles.
    TOTAL_LIMIT = 6000 - 2 * len(title)
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
        desc = f"{empty_message(title)}\n{footer}"
        if truncated > 0:
            desc += f"\n-# +{truncated} more not shown (6000 limit)"
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
        notice = "\n... and more not shown (limit 10 embeds)"
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

def _send(method: str, url: str, payload: dict, idempotent: bool, attempts: int = 3):
    """requests.request with bounded retries.

    Always retries HTTP 429 (honouring retry_after). Network errors and 5xx are only retried when the
    call is idempotent (PATCH): retrying a POST after a timeout could create a duplicate message.
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.request(method, url, json=payload, timeout=15)
        except requests.RequestException as e:
            last_exc = e
            print(f"{method} attempt {attempt}/{attempts} failed: {type(e).__name__}", file=sys.stderr)
            if not idempotent or attempt == attempts:
                raise
            time.sleep(2 * attempt)
            continue
        if attempt < attempts:
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 5))
                except Exception:
                    wait = 5.0
                print(f"Rate limited, retrying in {min(wait, 30):.1f}s", file=sys.stderr)
                time.sleep(min(wait, 30) + 0.5)
                continue
            if idempotent and r.status_code >= 500:
                print(f"{method} got {r.status_code}, retrying ({attempt}/{attempts})", file=sys.stderr)
                time.sleep(2 * attempt)
                continue
        return r
    raise last_exc  # pragma: no cover - loop always returns or raises

def send_or_edit(webhook_url: str, embeds: list, message_id_file: str):
    webhook_url = (webhook_url or "").strip() or os.environ.get("DISCORD_WEBHOOK", "") or os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        print("No webhook URL provided (arg --webhook or env DISCORD_WEBHOOK)", file=sys.stderr)
        sys.exit(1)
    payload = {"embeds": embeds}
    base = webhook_url.split("?")[0]
    existing_id = load_message_id(message_id_file) if message_id_file else ""

    if existing_id:
        print(f"Found existing message ID {existing_id}, trying to edit...")
        try:
            r = _send("PATCH", f"{base}/messages/{existing_id}", payload, idempotent=True)
        except requests.RequestException as e:
            print(f"PATCH failed: {type(e).__name__}", file=sys.stderr)
            sys.exit(1)
        print(f"PATCH .../messages/{existing_id} -> {r.status_code}")
        if r.status_code in (200, 204):
            print("Edited existing message successfully.")
            return existing_id
        if r.status_code != 404:
            print(f"Edit failed ({r.status_code}): {r.text[:500]}")
            print("Edit failed, exiting without creating a duplicate.")
            sys.exit(1)
        print("Message not found (404), creating a new one...")

    print("Creating new webhook message...")
    separator = "&" if "?" in webhook_url else "?"
    try:
        r = _send("POST", f"{webhook_url}{separator}wait=true", payload, idempotent=False)
    except requests.RequestException as e:
        print(f"POST failed: {type(e).__name__}", file=sys.stderr)
        sys.exit(1)
    print(f"POST .../?wait=true -> {r.status_code}")
    if r.status_code not in (200, 204):
        print(f"POST failed {r.status_code}: {r.text[:1000]}")
        sys.exit(1)
    try:
        msg_id = str(r.json().get("id", ""))
    except ValueError:
        print(f"POST response not JSON: {r.text[:500]}")
        return ""
    if not msg_id:
        print("POST succeeded but no ID in response.")
        return ""
    print(f"Created message ID {msg_id}")
    if message_id_file:
        save_message_id(message_id_file, msg_id)
    return msg_id

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
