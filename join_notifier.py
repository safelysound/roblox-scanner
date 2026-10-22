#!/usr/bin/env python3
"""
Discord notifier: posts a message as soon as a tracked Developer / Admin / Video Star joins The Hunt.

Polls Roblox presence every --interval seconds for --duration seconds (the workflow runs it once per
external-cron tick, so polling is near-continuous). "Joined" = the person was not in The Hunt on the
previous polls and now is. State (who was in The Hunt, cached group members) is kept in --state so
consecutive runs don't re-announce people who are still there.

Presence only exposes a user's place / server (gameId = jobId) to accounts that can see their game, and
each pool account follows a different subset of users. Small lists are queried with every account. Big
groups are swept by ONE account (rotating each poll) and only users who are "in-game, location hidden"
are re-queried through the other accounts. People who appear offline to everyone but their followers
are therefore found more slowly in big groups, and people hidden from every account can't be found.

Config: config/trackers.yaml -> notifiers.<name>. The webhook URL is read from env WEBHOOK (or the env
var named by webhookSecret).

  python join_notifier.py --notifier developers --dry-run --duration 60   # print what would be sent
  python join_notifier.py --notifier developers --test                    # send one sample message
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import requests
import yaml

from tracker_scanner import fetch_group_members
from developer_scanner import (
    DEFAULT_PLACE_ID,
    DEFAULT_UNIVERSE_ID,
    PRESENCE_API,
    PRESENCE_BATCH_SIZE,
    _get_all_cookies,
    fetch_user_infos,
    load_user_ids,
    resolve_game_name,
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
PENDING_MAX_AGE = 600     # give up on an unsent alert after 10 minutes
STATE_MAX_STALE = 900     # older than this the state is ignored (notifier was down) -> reseed silently
WIDE_PAUSE = 0.62         # ~1.6 requests/s per job so concurrent trackers stay under Roblox's per-account window
HIDDEN_RETRY_TTL = 150    # if all pool accounts confirmed placeId is hidden, re-probe across all accounts every 150s
PAUSE_MAX_WAIT = 8        # if every account is paused by a short 429, wait up to this long instead of failing the poll
SEED_MAX_WAIT = 180       # keep recording silently until one complete poll, but at most this long
MEMBERS_TTL = 3600        # refresh a group's member list at most hourly
WIDE_THRESHOLD = 150      # more users than this -> one rotating account sweeps, others only re-check hidden ones


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- presence

class Account:
    """One pool account with its own session and CSRF token."""

    def __init__(self, key, cookie):
        self.key = key
        self.csrf = None
        self.blocked_until = 0.0
        self.fails = 0
        self.errors = 0     # total failed requests, lets poll() tell whether a sweep was complete
        self.requests = 0
        self.limited = []   # retry-after values of the 429s this account got
        self.dead = False
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": UA, "Content-Type": "application/json",
                                  "Referer": "https://www.roblox.com/", "Origin": "https://www.roblox.com"})
        self.sess.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")

    def presence(self, ids):
        """Presence dicts for ids, or None if the request failed."""
        for _ in range(2):  # second try after a CSRF refresh
            hdr = {"x-csrf-token": self.csrf} if self.csrf else {}
            self.requests += 1
            try:
                r = self.sess.post(PRESENCE_API, json={"userIds": ids}, headers=hdr, timeout=15)
            except requests.RequestException as e:
                self.fails += 1
                self.errors += 1
                log(f"{self.key}: presence error {type(e).__name__}")
                return None
            if r.status_code == 403 and r.headers.get("x-csrf-token"):
                self.csrf = r.headers["x-csrf-token"]
                continue
            if r.status_code == 200:
                self.fails = 0
                return r.json().get("userPresences", [])
            if r.status_code == 429:
                try:
                    wait = float(r.headers.get("retry-after", 30))
                except ValueError:
                    wait = 30.0
                self.blocked_until = time.time() + wait
                self.limited.append(wait)
                self.errors += 1
                log(f"{self.key}: rate limited, pausing {wait:.0f}s")
                return None
            self.fails += 1
            self.errors += 1
            if r.status_code == 401 or self.fails >= 5:
                self.dead = True
                log(f"{self.key}: disabled for this run (HTTP {r.status_code})")
            else:
                log(f"{self.key}: presence HTTP {r.status_code}")
            return None
        return None


def _rank(p):
    """How informative a presence record is: has server id > has place > merely in-game."""
    if not p:
        return 0
    r = 1 if p.get("userPresenceType") == 2 else 0
    if p.get("placeId") or p.get("rootPlaceId"):
        r += 2
    if p.get("gameId"):
        r += 4
    return r


def _sweep(acct, ids, best, pause=0.4):
    """Query one account for ids, merging into best. -> how many ids (from the start of the list) were covered."""
    covered = 0
    for i in range(0, len(ids), PRESENCE_BATCH_SIZE):
        batch = ids[i:i + PRESENCE_BATCH_SIZE]
        res = acct.presence(batch)
        if res is None:
            break
        covered += len(batch)
        for p in res:
            uid = p.get("userId")
            if uid is not None and _rank(p) > _rank(best.get(uid)):
                best[uid] = p
        time.sleep(pause)
    return covered


def _wait_for_capacity(accounts):
    """Every live account is paused by a 429 (retry-after is ~5s): sleep until the earliest resumes.
    -> True if something is usable now, False if there is nothing to wait for or it would take too long."""
    live = [a for a in accounts if not a.dead]
    if not live:
        return False
    now = time.time()
    soonest = min(a.blocked_until for a in live)
    if soonest <= now:
        return True
    if soonest - now > PAUSE_MAX_WAIT:
        return False
    time.sleep(soonest - now + 0.1)
    return True


def _hidden_in_game(p):
    return p.get("userPresenceType") == 2 and not (p.get("placeId") or p.get("rootPlaceId") or p.get("universeId"))


def poll(accounts, ids, poll_no=0, stats=None, cache=None, acct_offset=0):
    """One presence sweep. -> (best record per user, any_success, complete).

    complete = every account answered every request it was asked, so the picture is not missing anyone
    who is only visible through an account that failed (used to decide when seeding is trustworthy).
    """
    errors_before = sum(a.errors for a in accounts)
    usable = [a for a in accounts if not a.dead and time.time() >= a.blocked_until]
    if not usable and _wait_for_capacity(accounts):
        usable = [a for a in accounts if not a.dead and time.time() >= a.blocked_until]
    best, ok = {}, False
    if not usable:
        return best, ok, False
    live = [a for a in accounts if not a.dead]
    covered_all = True
    viewer_map = cache.setdefault("viewer", {}) if cache is not None else {}
    unseeable_until = cache.setdefault("unseeable_until", {}) if cache is not None else {}

    if len(ids) <= WIDE_THRESHOLD:
        start = (poll_no + acct_offset) % len(usable)
        for k in range(len(usable)):
            acct = usable[(start + k) % len(usable)]
            ok = _sweep(acct, ids, best) > 0 or ok
    else:
        # Wide mode: first query known follower/viewer accounts directly for users whose game visibility
        # was already pinned to a specific account, then deal remaining users round-robin across the ring.
        by_key = {a.key: a for a in live}
        pinned_buckets = {}
        remaining_ids = []
        for u in ids:
            vk = viewer_map.get(u)
            va = by_key.get(vk) if vk else None
            if va and not va.dead and time.time() >= va.blocked_until:
                pinned_buckets.setdefault(vk, []).append(u)
            else:
                remaining_ids.append(u)

        answered_by = {}
        for vk, uids in pinned_buckets.items():
            va = by_key[vk]
            for i in range(0, len(uids), PRESENCE_BATCH_SIZE):
                sub = uids[i:i + PRESENCE_BATCH_SIZE]
                if _sweep(va, sub, best, pause=WIDE_PAUSE):
                    ok = True
                    for u in sub:
                        answered_by.setdefault(u, set()).add(va)
                else:
                    remaining_ids.extend(sub)

        batches = [remaining_ids[i:i + PRESENCE_BATCH_SIZE] for i in range(0, len(remaining_ids), PRESENCE_BATCH_SIZE)]
        start = (poll_no + acct_offset) % len(live)
        waits = 0
        for j, batch in enumerate(batches):
            while True:
                done = False
                for k in range(len(live)):
                    acct = live[(start + j + k) % len(live)]
                    if acct.dead or time.time() < acct.blocked_until:
                        continue
                    if _sweep(acct, batch, best, pause=WIDE_PAUSE):
                        ok = done = True
                        for u in batch:
                            answered_by.setdefault(u, set()).add(acct)
                        break
                if done:
                    break
                if waits < 4 and _wait_for_capacity(live):
                    waits += 1
                    continue
                covered_all = False
                break

        now_ts = time.time()
        all_hidden = [u for u, p in best.items() if _hidden_in_game(p)]
        if stats is not None:
            stats["hidden"] += len(all_hidden)
        hidden = [u for u in all_hidden if now_ts >= unseeable_until.get(u, 0.0)]

        # Re-check hidden users across remaining accounts, stopping per user as soon as server gameId is found
        # and caching the account that revealed them (or cooling down if zero accounts in the pool can see them).
        for c, i in enumerate(range(0, len(hidden), PRESENCE_BATCH_SIZE)):
            chunk = hidden[i:i + PRESENCE_BATCH_SIZE]
            for k in range(len(live)):
                acct = live[(start + c + k) % len(live)]
                if acct.dead or time.time() < acct.blocked_until:
                    continue
                todo = [
                    u for u in chunk
                    if acct not in answered_by.get(u, ())
                    and not (best.get(u) or {}).get("gameId")
                ]
                if not todo:
                    continue
                before_ranks = {u: _rank(best.get(u)) for u in todo}
                if _sweep(acct, todo, best, pause=WIDE_PAUSE) == len(todo):
                    for u in todo:
                        answered_by.setdefault(u, set()).add(acct)
                        if _rank(best.get(u)) > before_ranks[u] and not _hidden_in_game(best[u]):
                            viewer_map[u] = acct.key
                            unseeable_until.pop(u, None)

        if cache is not None:
            for u in hidden:
                if _hidden_in_game(best.get(u) or {}) and len(answered_by.get(u, ())) >= len(live):
                    unseeable_until[u] = time.time() + HIDDEN_RETRY_TTL

    complete = ok and covered_all and len(usable) == len(accounts) and sum(a.errors for a in accounts) == errors_before
    return best, ok, complete


def is_hunt(p, place_id, universe_id, any_game=False):
    """Same rule as tracker_scanner: in-game, and (any_game: in ANY game; else: in the Hunt place/universe)."""
    if not p or p.get("userPresenceType") != 2:
        return False
    if any_game:
        return bool(p.get("placeId") or p.get("rootPlaceId") or p.get("universeId"))
    if universe_id and p.get("universeId") == universe_id:
        return True
    return place_id in (p.get("placeId"), p.get("rootPlaceId"))


# ---------------------------------------------------------------- state / join detection

LOAD_INFO = {"result": "not loaded", "age": None}   # what load_state found, for the run summary


def fresh_state():
    return {"hunt": {}, "pending": {}, "last_job": {}, "last_place": {}, "updated": 0, "members": [], "members_ts": 0}


def load_state(path):
    """-> (state, seeded). seeded=False means: record who is in The Hunt now without announcing anyone."""
    try:
        st = json.loads(Path(path).read_text(encoding="utf-8"))
        st["hunt"] = {int(k): float(v) for k, v in st.get("hunt", {}).items()}
        st["pending"] = {int(k): float(v) for k, v in st.get("pending", {}).items()}
        st["last_job"] = {int(k): v for k, v in st.get("last_job", {}).items()}
        st["last_place"] = {int(k): v for k, v in st.get("last_place", {}).items()}
        st.setdefault("members", [])
        st.setdefault("members_ts", 0)
    except (OSError, ValueError):
        LOAD_INFO.update(result="none (fresh start)", age=None)
        return fresh_state(), False
    age = time.time() - st.get("updated", 0)
    if age > STATE_MAX_STALE:
        log("state is stale, reseeding without announcements")
        LOAD_INFO.update(result="STALE (reseeded silently)", age=age)
        return fresh_state(), False
    LOAD_INFO.update(result="loaded", age=age)
    # Trusted state: everyone recorded as in The Hunt is assumed to still be there. Without this, a gap
    # between runs longer than the grace window would make all of them look like fresh joins.
    now = time.time()
    st["hunt"] = {uid: now for uid in st["hunt"]}
    return st, True


def save_state(path, st):
    st["updated"] = time.time()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{path}.tmp"
    Path(tmp).write_text(json.dumps(st), encoding="utf-8")
    os.replace(tmp, path)


def detect(st, in_hunt, now, seeded, grace, wait_for_job):
    """Update state from the users currently in The Hunt; return uids ready to announce."""
    ready = []
    st.setdefault("last_job", {})
    st.setdefault("last_place", {})
    for uid, p in in_hunt.items():
        last = st["hunt"].get(uid)
        job = p.get("gameId")
        place = p.get("universeId") or p.get("rootPlaceId") or p.get("placeId")
        prev_job = st["last_job"].get(uid)
        prev_place = st["last_place"].get(uid)
        st["hunt"][uid] = now
        if not seeded:
            if job:
                st["last_job"][uid] = job
            if place:
                st["last_place"][uid] = place
            continue
        same_place = (not place or not prev_place or place == prev_place)
        if job and prev_job and job == prev_job and same_place:
            if place:
                st["last_place"][uid] = place
            continue  # confirmed same server as last sighting -- never a new event, regardless of any gap
        if job and prev_job and job != prev_job:
            st["pending"][uid] = now  # real hop: we already have the new job id, don't wait on it
        elif place and prev_place and place != prev_place:
            # Direct switch into a different game/universe (even if new server's gameId is hidden/VIP)
            st["pending"].setdefault(uid, now)
        elif last is None or now - last > grace:
            st["pending"].setdefault(uid, now)
        elif place and prev_place is None:
            # Seamless cutover backfill for pre-existing state entries without triggering false alerts
            st["last_place"][uid] = place
        first = st["pending"].get(uid)
        if first is not None and (job or now - first >= wait_for_job):
            ready.append(uid)
            if job:
                st["last_job"][uid] = job
            elif prev_job and place and prev_place and place != prev_place:
                st["last_job"].pop(uid, None)
            if place:
                st["last_place"][uid] = place
    for uid in [u for u, t in st["pending"].items() if now - t > PENDING_MAX_AGE]:
        del st["pending"][uid]
    for uid in [u for u, t in st["hunt"].items() if now - t > 86400]:
        del st["hunt"][uid]
    for uid in [u for u in list(st["last_job"]) if u not in st["hunt"]]:
        del st["last_job"][uid]
    for uid in [u for u in list(st["last_place"]) if u not in st["hunt"]]:
        del st["last_place"][uid]
    return ready


# ---------------------------------------------------------------- Discord

def md_escape(s):
    """Make a name safe inside a Discord masked-link label.

    Discord shows a backslash literally inside link text, so only escape what would really break
    formatting. An underscore between two letters/digits (Citizen_404) never triggers italics/underline
    and stays as is; one at the start/end, next to a space or punctuation, or doubled gets escaped.
    """
    for ch in "\\[]*~`|":
        s = s.replace(ch, "\\" + ch)
    return re.sub(r"(?<![^\W_])_|_(?![^\W_])", r"\\_", s)


JOIN_REDIRECT_BASE = "https://hunt-join.pages.dev/"


def join_url(p):
    """https URL for the Join button.

    Discord buttons can only open http(s) links, not roblox://, so this points at a small redirector
    page (JOIN_REDIRECT_BASE) that immediately hands off to roblox://experiences/start?placeId=...
    &gameInstanceId=..., the client's own native join route. That's a different, more direct mechanism
    than linking straight to https://www.roblox.com/games/start?... (the old value here), which goes
    through a web/JS intermediary that in practice did not reliably honor gameInstanceId on desktop.
    Confirmed working against a real server on 2026-09-26; the redirector page also carries its own
    fallback to the old web link, so nothing here needs a second fallback.
    """
    job = (p or {}).get("gameId")
    place = (p or {}).get("placeId") or (p or {}).get("rootPlaceId")
    if not job or not place:
        return None
    return f"{JOIN_REDIRECT_BASE}?placeId={place}&gameInstanceId={job}"


def avatar_url(uid):
    try:
        r = requests.get("https://thumbnails.roblox.com/v1/users/avatar-headshot",
                         params={"userIds": uid, "size": "150x150", "format": "Png"}, timeout=10)
        d = r.json()["data"][0]
        return d["imageUrl"] if d.get("state") == "Completed" else None
    except Exception:
        return None


def build_payload(uid, name, p, cfg, joined_at, thumb=None, test=False, game_name=None):
    display = md_escape(name.get("displayName") or name.get("username") or f"User {uid}")
    username = md_escape(name.get("username") or str(uid))
    profile = f"https://www.roblox.com/users/{uid}/profile"
    job = (p or {}).get("gameId")
    server = f"`{job}`" if job else "`server not visible`"
    game_label = game_name or "The Hunt"
    desc = (f"- {cfg['emoji']} [{display} ({username})]({profile}) has joined **{game_label}**!\n"
            f"<t:{int(joined_at)}:R>\u30fb{server}")
    # cfg['title'] may contain a {game} placeholder (any_game-mode notifiers); .format() is a safe no-op
    # for titles that don't reference it (e.g. Developers, which is always Hunt-specific).
    title_text = cfg["title"].format(game=game_label)
    embed = {"title": ("[TEST] " if test else "") + title_text, "description": desc}
    if cfg.get("color") is not None:
        embed["color"] = cfg["color"]
    if thumb:
        embed["thumbnail"] = {"url": thumb}
    payload = {"embeds": [embed]}
    if cfg.get("role_id") and not test:
        payload["content"] = f"-# <@&{cfg['role_id']}>"
        payload["allowed_mentions"] = {"roles": [str(cfg["role_id"])]}
    else:
        payload["allowed_mentions"] = {"parse": []}
    url = join_url(p)
    if url:
        # Webhooks can only send LINK buttons (style 5); Discord opens the https URL, Roblox launches the client.
        payload["components"] = [{"type": 1, "components": [
            {"type": 2, "style": 5, "label": "Join \u2197", "emoji": {"name": "\U0001F465"}, "url": url}]}]
    return payload


def with_text_link(payload):
    """Fallback when the button can't be used: same join link as a clickable text link in the embed."""
    out = json.loads(json.dumps(payload))
    comps = out.pop("components", None)
    url = comps[0]["components"][0].get("url") if comps else None
    if url:
        out["embeds"][0]["description"] += f"\n[Join \u2197]({url})"
    return out


def _post(url, payload):
    """POST with retries. -> (ok, response_json_or_None, status)"""
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}wait=true&with_components=true"
    status = None
    for attempt in (1, 2, 3):
        try:
            r = requests.post(full, json=payload, timeout=15)
        except requests.RequestException as e:
            log(f"webhook error {type(e).__name__} (attempt {attempt})")
            time.sleep(2)
            continue
        status = r.status_code
        if status in (200, 204):
            try:
                return True, r.json(), status
            except ValueError:
                return True, None, status
        if status == 429:
            try:
                wait = float(r.json().get("retry_after", 3))
            except Exception:
                wait = 3.0
            time.sleep(min(wait, 15) + 0.5)
            continue
        if status >= 500:
            time.sleep(2)
            continue
        log(f"webhook rejected: HTTP {status} {r.text[:200]}")
        return False, None, status
    return False, None, status


def post_webhook(url, payload, dry_run):
    """Send the message. -> (ok, button_accepted). Falls back to a text link if Discord won't take the button."""
    if dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return True, None
    wants_button = bool(payload.get("components"))
    ok, msg, status = _post(url, payload)
    if not ok and wants_button and status == 400:
        log("Discord rejected the button; retrying with a text link")
        ok, msg, status = _post(url, with_text_link(payload))
        return ok, False
    if ok and wants_button:
        accepted = bool(msg and msg.get("components"))
        if not accepted and msg and msg.get("id"):
            log("Discord dropped the button; adding a text link to the message")
            fixed = with_text_link(payload)
            try:
                requests.patch(f"{url.split('?')[0]}/messages/{msg['id']}", json={"embeds": fixed["embeds"]}, timeout=15)
            except requests.RequestException:
                pass
        return True, accepted
    return ok, None


# ---------------------------------------------------------------- main

def load_config(name):
    data = yaml.safe_load(Path("config/trackers.yaml").read_text(encoding="utf-8")) or {}
    return (data.get("notifiers") or {}).get(name), {t["id"]: t for t in data.get("trackers", [])}, data.get("settings", {})


def parse_color(c):
    if c is None:
        return None
    return c if isinstance(c, int) else int(str(c).lstrip("#"), 16)


def resolve_ids(tracker, st):
    """User ids to watch: an ids file, or a group's members (cached an hour in state) plus extraIds."""
    if tracker.get("idsFile"):
        ids = list(load_user_ids(tracker["idsFile"]))
    elif tracker.get("groupId"):
        if st.get("members") and time.time() - st.get("members_ts", 0) < MEMBERS_TTL:
            ids = list(st["members"])
        else:
            members = fetch_group_members(int(tracker["groupId"]))
            ids = [m["userId"] for m in members if m.get("userId")]
            if ids:
                st["members"], st["members_ts"] = ids, time.time()
                log(f"fetched {len(ids)} group members")
            else:
                ids = list(st.get("members", []))  # fetch failed: keep using the old list
    else:
        ids = []
    ids += [int(u) for u in tracker.get("extraIds", [])]
    return list(dict.fromkeys(ids))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notifier", required=True, help="name under notifiers: in config/trackers.yaml")
    ap.add_argument("--state", default=None)
    ap.add_argument("--duration", type=int, default=278, help="seconds to keep polling (0 = forever)")
    ap.add_argument("--interval", type=int, default=None, help="seconds between polls, start to start")
    ap.add_argument("--grace", type=int, default=180, help="absent this long before a return counts as a new join")
    ap.add_argument("--wait-for-job", type=int, default=30, help="max seconds to wait for the server id before announcing")
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    ap.add_argument("--test", action="store_true", help="send one sample message and exit")
    args = ap.parse_args()

    ncfg, trackers, settings = load_config(args.notifier)
    if not ncfg:
        log(f"notifier '{args.notifier}' not found in config/trackers.yaml")
        return 1
    tracker = trackers.get(ncfg.get("tracker") or args.notifier)
    if not tracker:
        log(f"notifier '{args.notifier}' points at an unknown tracker")
        return 1
    cfg = {"emoji": ncfg.get("emoji", ""), "role_id": ncfg.get("roleId"), "title": ncfg["title"],
           "color": parse_color(ncfg.get("color")), "thumbnail": ncfg.get("thumbnail", "avatar")}
    interval = args.interval or int(ncfg.get("interval", 20))
    state_path = args.state or f"state/notifier_{args.notifier}.json"
    webhook = (os.environ.get("WEBHOOK") or os.environ.get(ncfg.get("webhookSecret", ""), "")).strip()
    if not webhook and not args.dry_run:
        print(f"::warning title=notifier {args.notifier}::webhook secret {ncfg.get('webhookSecret')} is not set, notifier disabled")
        return 0

    place_id = int(settings.get("targetPlaceId", DEFAULT_PLACE_ID))
    universe_id = int(settings.get("targetUniverseId", DEFAULT_UNIVERSE_ID))
    any_game = bool(tracker.get("anyGame", False))

    if args.test:
        uid = (load_user_ids(tracker["idsFile"]) if tracker.get("idsFile") else [1])[0]
        try:
            name = fetch_user_infos([uid]).get(uid, {})
        except Exception:
            name = {}
        sample = {"userPresenceType": 2, "placeId": place_id, "rootPlaceId": place_id,
                  "gameId": "00000000-0000-0000-0000-000000000000"}
        sample_game = "a Test Game" if any_game else None
        thumb = avatar_url(uid) if cfg["thumbnail"] == "avatar" else None
        ok, button = post_webhook(webhook, build_payload(uid, name, sample, cfg, time.time(), thumb, test=True, game_name=sample_game), args.dry_run)
        print(f"::notice title=notifier {args.notifier} test::sent={ok} button_accepted={button} thumbnail={'yes' if thumb else 'no'}")
        return 0 if ok else 1

    pool = _get_all_cookies()
    if not pool:
        log("no ROBLOSECURITY cookies configured; presence requires an account")
        return 1
    accounts = [Account(k, c) for k, c in pool]

    st, seeded = load_state(state_path)
    ids = resolve_ids(tracker, st)
    if not ids:
        log("no users to watch")
        return 1
    mode = "wide" if len(ids) > WIDE_THRESHOLD else "all-accounts"
    log(f"[{args.notifier}] watching {len(ids)} users, {len(accounts)} accounts, {mode} sweeps, every {interval}s for "
        f"{'ever' if not args.duration else str(args.duration) + 's'}")

    names = {}
    game_name_cache = {}  # universeId -> resolved name, any_game mode only

    def _game_name_for(p):
        if not any_game:
            return None
        u = p.get("universeId")
        if u:
            if u not in game_name_cache:
                try:
                    game_name_cache[u] = resolve_game_name(u)
                except Exception:
                    game_name_cache[u] = None
            if game_name_cache[u]:
                return game_name_cache[u]
        return p.get("lastLocation") or "a game"

    start = time.time()
    polls = good_polls = sent = 0
    sweep_secs = []
    pstats = Counter()
    poll_cache = {}
    ring_slots = {"admin": 0, "video-stars": max(1, len(accounts) // 2), "developers": max(1, len(accounts) // 4)}
    acct_offset = ring_slots.get(args.notifier, sum(ord(ch) for ch in args.notifier) % max(1, len(accounts)))
    while True:
        t0 = time.time()
        best, ok, complete = poll(accounts, ids, polls, pstats, cache=poll_cache, acct_offset=acct_offset)
        polls += 1
        sweep_secs.append(time.time() - t0)
        if ok:
            good_polls += 1
            in_hunt = {u: p for u, p in best.items() if is_hunt(p, place_id, universe_id, any_game)}
            ready = detect(st, in_hunt, t0, seeded, args.grace, args.wait_for_job)
            if not seeded and (complete or t0 - start >= SEED_MAX_WAIT):
                # Only trust the baseline once every account answered; otherwise people visible through a
                # failed account would look like fresh joins when it recovers.
                what = "in-game (any game)" if any_game else "already in The Hunt"
                log(f"seeded: {len(in_hunt)} {what}, not announcing" + ("" if complete else " (incomplete after waiting)"))
                seeded = True
            for uid in ready:
                if uid not in names:
                    try:
                        names.update(fetch_user_infos([uid]))
                    except Exception:
                        pass
                joined_at = st["pending"].get(uid, t0)
                thumb = avatar_url(uid) if cfg["thumbnail"] == "avatar" else None
                game_name = _game_name_for(in_hunt[uid])
                log(f"announcing {uid} ({game_name or 'The Hunt'}, server id {'yes' if in_hunt[uid].get('gameId') else 'no'})")
                sent_ok, _ = post_webhook(webhook, build_payload(uid, names.get(uid, {}), in_hunt[uid], cfg, joined_at, thumb, game_name=game_name), args.dry_run)
                if sent_ok:
                    st["pending"].pop(uid, None)
                    sent += 1
            save_state(state_path, st)
        else:
            log("poll failed on every account")
        if args.duration and time.time() - start + interval > args.duration:
            break
        time.sleep(max(0.0, interval - (time.time() - t0)))

    state_note = LOAD_INFO["result"]
    if LOAD_INFO["age"] is not None:
        state_note += f" (age {LOAD_INFO['age']:.0f}s)"
    limited = sum(1 for a in accounts if a.limited)
    dead = sum(1 for a in accounts if a.dead)
    total_req = sum(a.requests for a in accounts)
    n429 = sum(len(a.limited) for a in accounts)
    waits = sorted(w for a in accounts for w in a.limited)
    print(f"::notice title=notifier {args.notifier}::{good_polls}/{polls} polls ok, {sent} announcement(s), "
          f"{len(st['hunt'])} {'in-game (any game)' if any_game else 'in The Hunt'}, {len(ids)} watched, avg hidden {pstats['hidden'] / max(1, polls):.0f}, avg sweep {sum(sweep_secs)/len(sweep_secs):.0f}s, "
          f"state at start: {state_note} | "
          f"rate-limited accounts {limited}, disabled accounts {dead} of {len(accounts)} in pool | presence requests {total_req} in {time.time()-start:.0f}s, "
          f"429s {n429}, retry-after {waits[0] if waits else '-'}..{waits[-1] if waits else '-'}s")
    return 0 if good_polls else 1


if __name__ == "__main__":
    sys.exit(main())
