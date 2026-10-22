#!/usr/bin/env python3
"""
Discord webhook multi-message auto-editing updater for Roblox Tracker channels.
- Configurable title/color/emoji per tracker (Admin, Video Star, Developer)
- Generates standing embeds showing all visible target-game players + unfollowed Unknowns:
  - <emoji> [DisplayName (@Username)](https://www.roblox.com/users/ID/profile)
    - Playing: [The Hollow](https://www.roblox.com/games/116463962153167/The-Hollow) (Must Follow to Join)
    - Playing: [The Hollow](https://www.roblox.com/games/116463962153167/The-Hollow) (Appearing Offline - Must Follow to Join)
    - Playing: [The Hollow](https://www.roblox.com/games/116463962153167/The-Hollow)
    - Playing: **Unknown** (Must Follow/Game Status Hidden)
  -# Last updated: <t:UNIX:R>

- Multi-message scaling & cleanup (bypasses Discord's 6,000-char per-message limit cleanly):
  * Within a message: splits across embeds at <=3,950 UTF-16 code units per description (max 4,096).
  * Across messages: whenever total text exceeds ~5,900 chars, overflows cleanly onto additional
    webhook messages ("<Title> (1/N)", "<Title> (2/N)", ...), storing one message ID per line in
    the tracker's message-id file.
  * Automatic cleanup: as player count drops and fewer messages are needed, edits the remaining
    standing message(s) in place and immediately DELETEs trailing overflow messages from Discord.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

from join_notifier import JOIN_REDIRECT_BASE, md_escape

ROBLOX_EMOJI = "<:roblox:1551329066337050754>"
DEFAULT_COLOR = 3168504  # Admin Tracker #305778
TARGET_GAME_URL = "https://www.roblox.com/games/116463962153167/The-Hollow"
TARGET_PLACE_ID = "116463962153167"
TARGET_UNIVERSE_ID = 10770728771
TARGET_GAME_NAME = "The Hollow"
MAX_DESC = 3950        # safely under Discord's 4,096 per-embed description limit
MSG_CHAR_LIMIT = 5900  # safely under Discord's 6,000 total embed char limit per message
MAX_EMBEDS_PER_MSG = 10


def _discord_len(s: str) -> int:
    """Conservative length matching Discord's limits across astral emojis / math-bold Unicode."""
    if not s:
        return 0
    return max(len(s), len(s.encode("utf-16-le")) // 2)


def parse_color(val):
    val = str(val).strip()
    if val.startswith("#"):
        val = val[1:]
    try:
        if len(val) <= 6 and all(c in "0123456789abcdefABCDEF" for c in val):
            return int(val, 16)
    except Exception:
        pass
    try:
        return int(val)
    except Exception:
        return DEFAULT_COLOR


def slugify(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip())
    name = re.sub(r"-+", "-", name).strip("-")
    return name or "Game"


def get_game_link(place_id, universe_id, last_location: str, target_place_id: str = TARGET_PLACE_ID, target_game_name: str = None):
    if place_id:
        raw_name = (last_location or "").strip() or (target_game_name or "").strip() or "Game"
        safe_name = slugify(raw_name) if raw_name != "Game" else "Game"
        url = f"https://www.roblox.com/games/{place_id}/{safe_name}"
        if str(place_id) == TARGET_PLACE_ID:
            url = TARGET_GAME_URL
            raw_name = (target_game_name or "").strip() or TARGET_GAME_NAME
        return raw_name, url
    if target_place_id:
        raw_name = (last_location or "").strip() or (target_game_name or "").strip() or (TARGET_GAME_NAME if str(target_place_id) == TARGET_PLACE_ID else "Game")
        url = TARGET_GAME_URL if str(target_place_id) == TARGET_PLACE_ID else f"https://www.roblox.com/games/{target_place_id}/Game"
        return raw_name, url
    return None, None


def empty_message(title: str) -> str:
    t = title.lower()
    if "video" in t or "star" in t:
        return "No video stars are currently playing."
    if "dev" in t:
        return "No developers are currently playing."
    return "No admins are currently playing."


def _format_player_lines(data: dict, emoji: str = ROBLOX_EMOJI, any_game=None) -> list:
    target = data.get("target_game_players", [])
    other = data.get("other_game_players", [])
    meta = data.get("meta") or {}
    if any_game is None:
        any_game = bool(meta.get("anyGame", False))

    target_place = str(meta.get("targetPlaceId") or TARGET_PLACE_ID)
    raw_univ = meta.get("targetUniverseId")
    try:
        target_universe = int(raw_univ) if raw_univ is not None else (TARGET_UNIVERSE_ID if target_place == TARGET_PLACE_ID else None)
    except (TypeError, ValueError):
        target_universe = TARGET_UNIVERSE_ID if target_place == TARGET_PLACE_ID else None
    target_game_name = meta.get("targetGameName")

    def _is_target_visible(u):
        pid = u.get("placeId") or u.get("rootPlaceId")
        if pid is None:
            return False
        if any_game:
            return True
        if str(u.get("placeId")) == target_place or str(u.get("rootPlaceId")) == target_place:
            return True
        if target_universe is not None and u.get("universeId") is not None:
            try:
                if int(u.get("universeId")) == target_universe:
                    return True
            except (TypeError, ValueError):
                pass
        return False

    filtered = []
    for u in target + other:
        pid = u.get("placeId") or u.get("rootPlaceId")
        ptype = u.get("presenceType")
        try:
            ptype_int = int(ptype) if ptype is not None else None
        except Exception:
            ptype_int = None
        is_following = bool(u.get("isFollowing"))
        if ptype_int in (1, 3) or ptype_int is None:
            continue
        if _is_target_visible(u):
            filtered.append(u)
            continue
        if ptype_int == 2 and pid is None and u.get("universeId") is None:
            if is_following:
                continue
            filtered.append(u)

    def sort_key(u):
        is_following = bool(u.get("isFollowing"))
        ptype = u.get("presenceType")
        try:
            ptype_int = int(ptype) if ptype is not None else None
        except Exception:
            ptype_int = None
        is_visible = _is_target_visible(u)
        if is_visible and not is_following and ptype_int == 2:
            prio = 0
        elif is_visible and is_following:
            prio = 1
        elif is_visible and ptype_int == 0 and is_following:
            prio = 1
        elif is_visible:
            prio = 0
        else:
            prio = 2
        name = (u.get("displayName") or u.get("username") or "").lower()
        if any_game:
            game = (u.get("gameName") or u.get("lastLocation") or "").lower()
            return (prio, game, name)
        return (prio, name)

    filtered = sorted(filtered, key=sort_key)

    lines = []
    for u in filtered:
        display = md_escape(u.get("displayName") or u.get("username") or "Unknown")
        username = md_escape(u.get("username") or "Unknown")
        user_id = u.get("userId")
        profile = f"https://www.roblox.com/users/{user_id}/profile" if user_id else "https://www.roblox.com/"
        place_id = u.get("placeId")
        root_place = u.get("rootPlaceId")
        universe_id = u.get("universeId")
        last_loc = u.get("gameName") or u.get("lastLocation") or ""
        effective_place = place_id or root_place
        game_name, game_url = get_game_link(effective_place, universe_id, last_loc, target_place_id=target_place, target_game_name=target_game_name)

        pid_check = u.get("placeId") or u.get("rootPlaceId")
        is_following = bool(u.get("isFollowing"))
        job_id = u.get("gameId")
        ptype = u.get("presenceType")
        try:
            ptype_int = int(ptype) if ptype is not None else None
        except Exception:
            ptype_int = None

        if _is_target_visible(u):
            label = md_escape(game_name or "Game")
            if ptype_int == 0:
                playing = f"[{label}]({game_url}) (Appearing Offline - Must Follow to Join)"
            elif is_following:
                playing = f"[{label}]({game_url}) (Must Follow to Join)"
            else:
                playing = f"[{label}]({game_url})"
            if any_game and job_id and pid_check:
                join_link = f"{JOIN_REDIRECT_BASE}?placeId={pid_check}&gameInstanceId={job_id}"
                playing += f" \u00b7 [Join \u2197]({join_link})"
        else:
            playing = "**Unknown** (Must Follow/Game Status Hidden)"

        lines.append(f"- {emoji} [{display} (@{username})]({profile})\n  - Playing: {playing}")

    return lines


def _pack_lines_into_messages(
    lines: list,
    title: str,
    color: int,
    footer: str,
    msg_char_limit: int = MSG_CHAR_LIMIT,
    max_desc: int = MAX_DESC,
) -> list:
    """Split player lines into 1..N Discord messages, each strictly within 4,096/embed & 6,000/message."""
    if not lines:
        return [[{"title": title, "color": color, "description": f"{empty_message(title)}\n{footer}"}]]

    title_budget = _discord_len(title) + 16   # reserves room for " (99/99)" on multi-message pages
    footer_budget = _discord_len(footer) + 1
    max_single_line = max(200, max_desc - footer_budget - 4)

    raw_messages = []   # list of messages, each message = list of embed line-lists
    cur_msg = [[]]
    cur_embed_len = 0
    cur_msg_desc_len = 0

    for raw_line in lines:
        line = raw_line if _discord_len(raw_line) <= max_single_line else (raw_line[:max_single_line - 3] + "...")
        line_len = _discord_len(line)
        join_extra = line_len + (1 if cur_msg[-1] else 0)

        msg_has_content = bool(cur_msg[0])
        would_exceed_msg = title_budget + cur_msg_desc_len + join_extra + footer_budget > msg_char_limit
        at_embed_cap = len(cur_msg) >= MAX_EMBEDS_PER_MSG - 1 and (cur_embed_len + join_extra > max_desc)

        if msg_has_content and (would_exceed_msg or at_embed_cap):
            raw_messages.append(cur_msg)
            cur_msg = [[line]]
            cur_embed_len = line_len
            cur_msg_desc_len = line_len
        elif cur_msg[-1] and cur_embed_len + join_extra > max_desc:
            cur_msg.append([line])
            cur_embed_len = line_len
            cur_msg_desc_len += line_len
        else:
            cur_msg[-1].append(line)
            cur_embed_len += join_extra
            cur_msg_desc_len += join_extra

    if cur_msg[0]:
        raw_messages.append(cur_msg)

    total_msgs = len(raw_messages)
    messages = []
    for m_idx, embed_chunks in enumerate(raw_messages):
        msg_title = title if total_msgs == 1 else f"{title} ({m_idx + 1}/{total_msgs})"
        embeds = []
        for c_idx, chunk in enumerate(embed_chunks):
            desc = "\n".join(chunk)
            is_last = c_idx == len(embed_chunks) - 1
            if is_last:
                if _discord_len(desc) + footer_budget <= max_desc:
                    desc = f"{desc}\n{footer}"
                    emb = {"color": color, "description": desc}
                    if c_idx == 0:
                        emb["title"] = msg_title
                    embeds.append(emb)
                else:
                    # Last embed description is full; place footer in a clean trailing embed within the same message budget
                    emb = {"color": color, "description": desc}
                    if c_idx == 0:
                        emb["title"] = msg_title
                    embeds.append(emb)
                    embeds.append({"color": color, "description": footer})
            else:
                emb = {"color": color, "description": desc}
                if c_idx == 0:
                    emb["title"] = msg_title
                embeds.append(emb)
        messages.append(embeds)

    return messages


def build_message_batches(
    data: dict,
    title: str = "Admin Tracker",
    color: int = DEFAULT_COLOR,
    emoji: str = ROBLOX_EMOJI,
    any_game=None,
    msg_char_limit: int = MSG_CHAR_LIMIT,
    max_desc: int = MAX_DESC,
) -> list:
    """Return List[List[embed_dict]] — one inner list of embeds per Discord message needed."""
    lines = _format_player_lines(data, emoji=emoji, any_game=any_game)
    footer = f"-# Last updated: <t:{int(time.time())}:R>"
    return _pack_lines_into_messages(lines, title=title, color=color, footer=footer, msg_char_limit=msg_char_limit, max_desc=max_desc)


def build_embeds(data: dict, title="Admin Tracker", color=DEFAULT_COLOR, emoji=ROBLOX_EMOJI, any_game=None) -> list:
    """Backward-compatible helper returning the first message's embeds."""
    return build_message_batches(data, title=title, color=color, emoji=emoji, any_game=any_game)[0]


def load_message_ids(path: str) -> list:
    """Load ordered list of Discord message IDs (one per line, JSON array, or single ID)."""
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    try:
        txt = p.read_text(encoding="utf-8").strip()
        if not txt:
            return []
        try:
            j = json.loads(txt)
            if isinstance(j, list):
                return [str(x).strip() for x in j if str(x).strip().isdigit()]
            if isinstance(j, dict) and "id" in j and str(j["id"]).strip().isdigit():
                return [str(j["id"]).strip()]
            if isinstance(j, (int, str)) and str(j).strip().isdigit():
                return [str(j).strip()]
        except Exception:
            pass
        ids = []
        seen = set()
        for tok in txt.split():
            t = tok.strip()
            if t.isdigit() and t not in seen:
                seen.add(t)
                ids.append(t)
        return ids
    except Exception:
        return []


def save_message_ids(path: str, msg_ids: list):
    clean = [str(m).strip() for m in msg_ids if str(m).strip()]
    Path(path).write_text(("\n".join(clean) + "\n") if clean else "", encoding="utf-8")
    print(f"Saved {len(clean)} message ID(s) {clean} to {path}")


def load_message_id(path: str) -> str:
    ids = load_message_ids(path)
    return ids[0] if ids else ""


def save_message_id(path: str, msg_id: str):
    save_message_ids(path, [msg_id] if msg_id else [])


def _send(method: str, url: str, payload: dict = None, idempotent: bool = True, attempts: int = 3):
    """HTTP helper with bounded 429 + 5xx/network retries for idempotent operations."""
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            kwargs = {"timeout": 15}
            if payload is not None:
                kwargs["json"] = payload
            r = requests.request(method, url, **kwargs)
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
                print(f"Rate limited on {method}, retrying in {min(wait, 30):.1f}s", file=sys.stderr)
                time.sleep(min(wait, 30) + 0.5)
                continue
            if idempotent and r.status_code >= 500:
                print(f"{method} got {r.status_code}, retrying ({attempt}/{attempts})", file=sys.stderr)
                time.sleep(2 * attempt)
                continue
        return r
    raise last_exc  # pragma: no cover


def _delete_webhook_message(base_url: str, msg_id: str) -> bool:
    """Delete an unneeded overflow webhook message. Returns True when confirmed gone (200/204/404)."""
    try:
        r = _send("DELETE", f"{base_url}/messages/{msg_id}", payload=None, idempotent=True)
    except requests.RequestException as e:
        print(f"DELETE .../messages/{msg_id} failed: {type(e).__name__}", file=sys.stderr)
        return False
    print(f"DELETE .../messages/{msg_id} -> {r.status_code}")
    return r.status_code in (200, 204, 404)


def _post_webhook_message(webhook_url: str, payload: dict) -> str:
    separator = "&" if "?" in webhook_url else "?"
    r = _send("POST", f"{webhook_url}{separator}wait=true", payload=payload, idempotent=False)
    print(f"POST .../?wait=true -> {r.status_code}")
    if r.status_code not in (200, 204):
        raise RuntimeError(f"POST failed {r.status_code}: {r.text[:500]}")
    msg_id = str(r.json().get("id", "")).strip()
    if not msg_id:
        raise RuntimeError("POST succeeded without returning a message ID")
    print(f"Created message ID {msg_id}")
    return msg_id


def sync_messages(webhook_url: str, message_batches: list, message_id_file: str = "") -> list:
    """Synchronize standing Discord webhook messages with `message_batches` (1..N messages).

    - Edits existing messages in place via PATCH.
    - Posts additional overflow messages below when >6,000 chars are needed.
    - Deletes trailing overflow messages immediately when no longer needed.
    - If an earlier message was manually deleted (404), removes stale downstream messages first so
      top-to-bottom message order (1/N -> 2/N -> ...) never inverts.
    """
    webhook_url = (webhook_url or "").strip() or os.environ.get("DISCORD_WEBHOOK", "") or os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        print("No webhook URL provided (arg --webhook or env DISCORD_WEBHOOK)", file=sys.stderr)
        sys.exit(1)

    base = webhook_url.split("?")[0]
    existing_ids = load_message_ids(message_id_file) if message_id_file else []
    active_ids = []
    undeleted_tail = []
    invalidated_from = None

    try:
        for i, embeds in enumerate(message_batches):
            payload = {"embeds": embeds}
            if invalidated_from is None and i < len(existing_ids):
                mid = existing_ids[i]
                print(f"Message {i + 1}/{len(message_batches)}: editing existing ID {mid}...")
                r = _send("PATCH", f"{base}/messages/{mid}", payload=payload, idempotent=True)
                print(f"PATCH .../messages/{mid} -> {r.status_code}")
                if r.status_code in (200, 204):
                    active_ids.append(mid)
                    continue
                if r.status_code != 404:
                    print(f"Edit failed ({r.status_code}): {r.text[:500]}", file=sys.stderr)
                    final_ids = active_ids + existing_ids[i:]
                    if message_id_file and final_ids != existing_ids:
                        save_message_ids(message_id_file, final_ids)
                    sys.exit(1)
                print(f"Message {mid} was deleted (404); cleaning downstream messages to preserve order...")
                invalidated_from = i
                for stale_id in existing_ids[i + 1:]:
                    if not _delete_webhook_message(base, stale_id):
                        undeleted_tail.append(stale_id)

            new_id = _post_webhook_message(webhook_url, payload)
            active_ids.append(new_id)
    except Exception as e:
        print(f"Webhook sync error: {e}", file=sys.stderr)
        # Persist any newly created message IDs immediately so nothing gets orphaned
        already = set(active_ids)
        fallback_ids = active_ids + [x for x in existing_ids[len(active_ids):] if x not in already]
        if message_id_file and fallback_ids and fallback_ids != existing_ids:
            save_message_ids(message_id_file, fallback_ids)
        sys.exit(1)

    if invalidated_from is None and len(existing_ids) > len(message_batches):
        overflow_ids = existing_ids[len(message_batches):]
        print(f"Cleaning up {len(overflow_ids)} unneeded overflow message(s): {overflow_ids}")
        for extra_id in overflow_ids:
            if not _delete_webhook_message(base, extra_id):
                undeleted_tail.append(extra_id)

    final_ids = active_ids + [x for x in undeleted_tail if x not in active_ids]
    if message_id_file and final_ids != existing_ids:
        save_message_ids(message_id_file, final_ids)
    return final_ids


def send_or_edit(webhook_url: str, embeds: list, message_id_file: str):
    ids = sync_messages(webhook_url, [embeds], message_id_file)
    return ids[0] if ids else ""


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Discord multi-message webhook updater")
    ap.add_argument("json_file", nargs="?")
    ap.add_argument("--webhook", type=str, default="", help="Discord webhook URL (or env DISCORD_WEBHOOK)")
    ap.add_argument("--message-id-file", type=str, default="discord_message_id.txt", help="File to store message ID(s)")
    ap.add_argument("--title", type=str, default="Admin Tracker", help="Embed title")
    ap.add_argument("--color", type=str, default=str(DEFAULT_COLOR), help="Embed color (decimal or hex #RRGGBB)")
    ap.add_argument("--emoji", type=str, default=ROBLOX_EMOJI, help="Emoji for each line")
    ap.add_argument("--dry-run", action="store_true", help="Print message batches without sending")
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
    batches = build_message_batches(data, title=args.title, color=color_int, emoji=args.emoji)

    if args.dry_run:
        out = [{"embeds": b} for b in batches]
        print(json.dumps(out[0] if len(out) == 1 else out, indent=2))
        total_embeds = sum(len(b) for b in batches)
        char_counts = [sum(_discord_len(e.get("title", "")) + _discord_len(e.get("description", "")) for e in b) for b in batches]
        print(f"\n# Messages: {len(batches)}, total embeds: {total_embeds}, per-message chars: {char_counts}", file=sys.stderr)
        sys.exit(0)

    webhook = args.webhook or os.environ.get("DISCORD_WEBHOOK", "")
    sync_messages(webhook, batches, args.message_id_file)
