#!/usr/bin/env python3
"""
Discord notifier: posts a message as soon as a tracked Developer joins The Hunt.

Polls Roblox presence every --interval seconds for --duration seconds (the workflow runs it once per
external-cron tick, so polling is near-continuous). "Joined" = the Developer was not in The Hunt on the
previous polls and now is. State (who was in The Hunt, when) is kept in --state so consecutive runs
don't re-announce people who are still there.

Presence only exposes a user's place / server (gameId = jobId) to accounts that can see their game, and
each pool account follows a different subset of users, so every account is queried and the most
informative answer per user wins. Users whose game is fully hidden to every account can't be detected.

Config: config/trackers.yaml -> notifiers.<name>. Webhook URL comes from the env var named there.

  python developer_notifier.py --dry-run --duration 60     # print what would be sent
  python developer_notifier.py --test                      # send one sample message (no role ping)
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
import yaml

from developer_scanner import (
    DEFAULT_PLACE_ID,
    DEFAULT_UNIVERSE_ID,
    PRESENCE_API,
    PRESENCE_BATCH_SIZE,
    _get_all_cookies,
    fetch_user_infos,
    load_user_ids,
)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
PENDING_MAX_AGE = 600     # give up on an unsent alert after 10 minutes
STATE_MAX_STALE = 900     # older than this the state is ignored (notifier was down) -> reseed silently


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
        self.dead = False
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": UA, "Content-Type": "application/json",
                                  "Referer": "https://www.roblox.com/", "Origin": "https://www.roblox.com"})
        self.sess.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")

    def presence(self, ids):
        """Presence dicts for ids, or None if the request failed."""
        for _ in range(2):  # second try after a CSRF refresh
            hdr = {"x-csrf-token": self.csrf} if self.csrf else {}
            try:
                r = self.sess.post(PRESENCE_API, json={"userIds": ids}, headers=hdr, timeout=15)
            except requests.RequestException as e:
                self.fails += 1
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
                log(f"{self.key}: rate limited, pausing {wait:.0f}s")
                return None
            self.fails += 1
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


def poll(accounts, ids):
    """Query every usable account; keep the most informative record per user. -> (best, any_success)"""
    best, ok = {}, False
    for acct in accounts:
        if acct.dead or time.time() < acct.blocked_until:
            continue
        for i in range(0, len(ids), PRESENCE_BATCH_SIZE):
            res = acct.presence(ids[i:i + PRESENCE_BATCH_SIZE])
            if res is None:
                break
            ok = True
            for p in res:
                uid = p.get("userId")
                if uid is not None and _rank(p) > _rank(best.get(uid)):
                    best[uid] = p
            time.sleep(0.25)
    return best, ok


def is_hunt(p, place_id, universe_id):
    """Same rule as tracker_scanner: in-game and in the Hunt place/universe."""
    if not p or p.get("userPresenceType") != 2:
        return False
    if universe_id and p.get("universeId") == universe_id:
        return True
    return place_id in (p.get("placeId"), p.get("rootPlaceId"))


# ---------------------------------------------------------------- state / join detection

def fresh_state():
    return {"hunt": {}, "pending": {}, "updated": 0}


def load_state(path):
    """-> (state, seeded). seeded=False means: record who is in The Hunt now without announcing anyone."""
    try:
        st = json.loads(Path(path).read_text(encoding="utf-8"))
        st["hunt"] = {int(k): float(v) for k, v in st.get("hunt", {}).items()}
        st["pending"] = {int(k): float(v) for k, v in st.get("pending", {}).items()}
    except (OSError, ValueError):
        return fresh_state(), False
    if time.time() - st.get("updated", 0) > STATE_MAX_STALE:
        log("state is stale, reseeding without announcements")
        return fresh_state(), False
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
    """Update state from the users currently in The Hunt; return uids ready to announce.

    in_hunt: {uid: presence}. A user counts as newly joined if not seen in The Hunt within `grace`
    seconds (absorbs presence flicker and short server hops). An announcement waits up to
    `wait_for_job` seconds for the server id to show up in presence.
    """
    ready = []
    for uid, p in in_hunt.items():
        last = st["hunt"].get(uid)
        st["hunt"][uid] = now
        if not seeded:
            continue
        if last is None or now - last > grace:
            st["pending"].setdefault(uid, now)
        first = st["pending"].get(uid)
        if first is not None and (p.get("gameId") or now - first >= wait_for_job):
            ready.append(uid)
    for uid in [u for u, t in st["pending"].items() if now - t > PENDING_MAX_AGE]:
        del st["pending"][uid]
    for uid in [u for u, t in st["hunt"].items() if now - t > 86400]:
        del st["hunt"][uid]
    return ready


# ---------------------------------------------------------------- Discord

def md_escape(s):
    for ch in "\\[]*_~`|>":
        s = s.replace(ch, "\\" + ch)
    return s


def server_text(p, fmt):
    job = (p or {}).get("gameId")
    place = (p or {}).get("placeId") or (p or {}).get("rootPlaceId")
    if not job or not place:
        return "Server not visible (the player's join privacy hides it)"
    link = f"https://www.roblox.com/games/start?placeId={place}&gameInstanceId={job}"
    return {"link": link, "id": job}.get(fmt, f"{link}\n{job}")


def build_payload(uid, name, p, cfg, test=False):
    display = md_escape(name.get("displayName") or name.get("username") or f"User {uid}")
    username = md_escape(name.get("username") or str(uid))
    profile = f"https://www.roblox.com/users/{uid}/profile"
    desc = (f"- {cfg['emoji']} [{display} ({username})]({profile}) has joined **The Hunt**!\n\n"
            f"```\n{server_text(p, cfg['server_format'])}\n```")
    payload = {"embeds": [{"title": ("[TEST] " if test else "") + "A Developer joined The Hunt!", "description": desc}]}
    if cfg.get("role_id") and not test:
        payload["content"] = f"-# <@&{cfg['role_id']}>"
        payload["allowed_mentions"] = {"roles": [str(cfg["role_id"])]}
    else:
        payload["allowed_mentions"] = {"parse": []}
    return payload


def post_webhook(url, payload, dry_run):
    if dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return True
    for attempt in (1, 2, 3):
        try:
            r = requests.post(url, json=payload, timeout=15)
        except requests.RequestException as e:
            log(f"webhook error {type(e).__name__} (attempt {attempt})")
            time.sleep(2)
            continue
        if r.status_code in (200, 204):
            return True
        if r.status_code == 429:
            try:
                wait = float(r.json().get("retry_after", 3))
            except Exception:
                wait = 3.0
            time.sleep(min(wait, 15) + 0.5)
            continue
        if r.status_code >= 500:
            time.sleep(2)
            continue
        log(f"webhook rejected: HTTP {r.status_code} {r.text[:200]}")
        return False
    return False


# ---------------------------------------------------------------- main

def load_config(name):
    data = yaml.safe_load(Path("config/trackers.yaml").read_text(encoding="utf-8")) or {}
    return (data.get("notifiers") or {}).get(name), {t["id"]: t for t in data.get("trackers", [])}, data.get("settings", {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notifier", default="developer-joins")
    ap.add_argument("--state", default="state/developer_notifier.json")
    ap.add_argument("--duration", type=int, default=260, help="seconds to keep polling (0 = forever)")
    ap.add_argument("--interval", type=int, default=20, help="seconds between polls (start to start)")
    ap.add_argument("--grace", type=int, default=180, help="absent this long before a return counts as a new join")
    ap.add_argument("--wait-for-job", type=int, default=30, help="max seconds to wait for the server id before announcing")
    ap.add_argument("--server-format", choices=["link", "id", "both"], default=None)
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    ap.add_argument("--test", action="store_true", help="send one sample message and exit")
    args = ap.parse_args()

    ncfg, trackers, settings = load_config(args.notifier)
    if not ncfg:
        log(f"notifier '{args.notifier}' not found in config/trackers.yaml")
        return 1
    tracker = trackers.get(ncfg.get("tracker"))
    if not tracker or not tracker.get("idsFile"):
        log("notifier needs a tracker with idsFile")
        return 1
    cfg = {"emoji": ncfg.get("emoji", ""), "role_id": ncfg.get("roleId"),
           "server_format": args.server_format or ncfg.get("serverFormat", "both")}
    webhook = os.environ.get(ncfg["webhookSecret"], "").strip()
    if not webhook and not args.dry_run:
        print(f"::warning title=developer notifier::secret {ncfg['webhookSecret']} is not set, notifier disabled")
        return 0

    ids = load_user_ids(tracker["idsFile"])
    place_id = int(settings.get("targetPlaceId", DEFAULT_PLACE_ID))
    universe_id = int(settings.get("targetUniverseId", DEFAULT_UNIVERSE_ID))
    names = {}
    try:
        names = fetch_user_infos(ids)
    except Exception as e:
        log(f"name lookup failed ({type(e).__name__}); will fall back to ids")

    if args.test:
        uid = ids[0]
        sample = {"userPresenceType": 2, "placeId": place_id, "rootPlaceId": place_id,
                  "gameId": "00000000-0000-0000-0000-000000000000"}
        ok = post_webhook(webhook, build_payload(uid, names.get(uid, {}), sample, cfg, test=True), args.dry_run)
        return 0 if ok else 1

    pool = _get_all_cookies()
    if not pool:
        log("no ROBLOSECURITY cookies configured; presence requires an account")
        return 1
    accounts = [Account(k, c) for k, c in pool]
    log(f"watching {len(ids)} developers with {len(accounts)} accounts, every {args.interval}s for "
        f"{'ever' if not args.duration else str(args.duration) + 's'}")

    st, seeded = load_state(args.state)
    start = time.time()
    polls = good_polls = sent = 0
    while True:
        t0 = time.time()
        best, ok = poll(accounts, ids)
        polls += 1
        if ok:
            good_polls += 1
            in_hunt = {u: p for u, p in best.items() if is_hunt(p, place_id, universe_id)}
            ready = detect(st, in_hunt, t0, seeded, args.grace, args.wait_for_job)
            if not seeded:
                log(f"seeded: {len(in_hunt)} developer(s) already in The Hunt, not announcing")
                seeded = True
            for uid in ready:
                if uid not in names:
                    try:
                        names.update(fetch_user_infos([uid]))
                    except Exception:
                        pass
                log(f"announcing {uid} (server id {'yes' if in_hunt[uid].get('gameId') else 'no'})")
                if post_webhook(webhook, build_payload(uid, names.get(uid, {}), in_hunt[uid], cfg), args.dry_run):
                    st["pending"].pop(uid, None)
                    sent += 1
            save_state(args.state, st)
        else:
            log("poll failed on every account")
        if args.duration and time.time() - start + args.interval > args.duration:
            break
        time.sleep(max(0.0, args.interval - (time.time() - t0)))

    print(f"::notice title=developer notifier::{good_polls}/{polls} polls ok, {sent} announcement(s), "
          f"{len(st['hunt'])} developer(s) tracked")
    return 0 if good_polls else 1


if __name__ == "__main__":
    sys.exit(main())
