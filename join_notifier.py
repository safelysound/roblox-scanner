#!/usr/bin/env python3
"""
High-speed parallel streaming Discord join notifier for Roblox Tracker channels.

Architecture & latency optimizations:
  - Sharded horizontal scaling (--shard / --shards): deterministic `uid % shards == shard` partitioning
    across multiple GitHub Actions runners so each runner stays well below Roblox's ~60 req/min/IP window
    while sweeping thousands of members every few seconds with near-zero 429s.
  - Direct follower routing + sweeper specialization: maps followed users directly to the exact pool
    account that follows them (cached hourly in state) so Pass 1 returns both `placeId` and `gameId`
    immediately, while zero-follow accounts sweep unfollowed users concurrently across 6 worker threads.
  - Streaming mid-sweep webhook delivery + pre-warmed user/avatar caches: announces confirmed joins to
    Discord immediately as each batch arrives (~0.1s) rather than waiting for the rest of the sweep, and
    announces VIP/private joins immediately once follower accounts confirm `gameId` is hidden.
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml

from tracker_scanner import fetch_group_members
from developer_scanner import (
    DEFAULT_GAME_NAME,
    DEFAULT_PLACE_ID,
    DEFAULT_UNIVERSE_ID,
    PRESENCE_API,
    PRESENCE_BATCH_SIZE,
    _get_all_cookies,
    _get_followings,
    _get_my_id,
    fetch_user_infos,
    load_user_ids,
    resolve_game_name,
)
from sync_developer_ids import fetch_group_sources

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
PENDING_MAX_AGE = 600     # drop unsent pending entry after 10 minutes
STATE_MAX_STALE = 900     # older than 15 minutes -> silent reseed
HIDDEN_RETRY_TTL = 150    # cool down confirmed unseeable hidden users for 150s
PAUSE_MAX_WAIT = 6        # max wait when all accounts briefly pause on 429
SEED_MAX_WAIT = 60        # max seconds to wait before sealing baseline seed
MEMBERS_TTL = 3600        # refresh group roster hourly
GROUP_SOURCES_TTL = 900   # refresh lightweight developer groupSources every 15 min
FOLLOWERS_TTL = 3600      # refresh account->followings map hourly
MAX_WORKERS = 6           # parallel presence worker threads per runner
BATCH_STAGGER = 0.06      # gentle burst spacing (seconds) across concurrent worker submits
WIDE_THRESHOLD = 150


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- presence

# Shared per-runner IP rate-limit gate: Roblox presence.roblox.com enforces 60 req / 60s per runner IP
# across ALL cookies simultaneously, so immediately failing over across 11 other cookies on a 429 just
# fires 11 more instant 429s instead of letting the runner IP's 5s sliding sub-window refill once.
_IP_GATE_LOCK = threading.Lock()
_IP_BLOCKED_UNTIL = 0.0


class Account:
    """Pool account with thread-local keep-alive HTTP sessions and shared CSRF/rate state."""

    def __init__(self, key, cookie):
        self.key = key
        self.cookie = cookie
        self.csrf = None
        self.blocked_until = 0.0
        self.fails = 0
        self.errors = 0
        self.requests = 0
        self.limited = []
        self.dead = False
        self._local = threading.local()
        self._lock = threading.Lock()
        # Keep `.sess` alias for any direct unit-test inspection/mocking
        self.sess = self._make_session()

    def _make_session(self):
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Referer": "https://www.roblox.com/",
            "Origin": "https://www.roblox.com",
        })
        s.cookies.set(".ROBLOSECURITY", self.cookie, domain=".roblox.com")
        return s

    def _get_session(self):
        if threading.current_thread() is threading.main_thread():
            return self.sess
        s = getattr(self._local, "sess", None)
        if s is None:
            s = self._make_session()
            self._local.sess = s
        return s

    def presence(self, ids):
        """Presence dicts for ids, or None if the request failed."""
        global _IP_BLOCKED_UNTIL
        sess = self._get_session()
        for attempt in range(3):
            now_gate = time.time()
            if _IP_BLOCKED_UNTIL > now_gate and _IP_BLOCKED_UNTIL - now_gate <= PAUSE_MAX_WAIT:
                time.sleep(_IP_BLOCKED_UNTIL - now_gate)
            hdr = {"x-csrf-token": self.csrf} if self.csrf else {}
            with self._lock:
                self.requests += 1
            try:
                r = sess.post(PRESENCE_API, json={"userIds": ids}, headers=hdr, timeout=12)
            except requests.RequestException as e:
                with self._lock:
                    self.fails += 1
                    self.errors += 1
                log(f"{self.key}: presence error {type(e).__name__}")
                return None
            if r.status_code == 403 and r.headers.get("x-csrf-token"):
                self.csrf = r.headers["x-csrf-token"]
                continue
            if r.status_code == 200:
                with self._lock:
                    self.fails = 0
                return r.json().get("userPresences", [])
            if r.status_code == 429:
                try:
                    wait = float(r.headers.get("retry-after", 5))
                except ValueError:
                    wait = 5.0
                until = time.time() + wait + 0.15
                with _IP_GATE_LOCK:
                    if until > _IP_BLOCKED_UNTIL:
                        _IP_BLOCKED_UNTIL = until
                with self._lock:
                    self.limited.append(wait)
                if attempt == 0 and wait <= PAUSE_MAX_WAIT:
                    continue
                with self._lock:
                    self.blocked_until = max(self.blocked_until, until)
                    self.errors += 1
                log(f"{self.key}: rate limited, pausing {wait:.0f}s")
                return None
            with self._lock:
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


def _hidden_in_game(p):
    return p.get("userPresenceType") == 2 and not (p.get("placeId") or p.get("rootPlaceId") or p.get("universeId"))


def _wait_for_capacity(accounts):
    live = [a for a in accounts if not a.dead]
    if not live:
        return False
    now = time.time()
    soonest = min(a.blocked_until for a in live)
    if soonest <= now:
        return True
    if soonest - now > PAUSE_MAX_WAIT:
        return False
    time.sleep(soonest - now + 0.05)
    return True


def _batch_request_with_failover(primary_acct, live_accounts, start_idx, batch):
    """Run one 50-user presence batch on `primary_acct`, falling back cleanly if rate-limited."""
    candidates = [primary_acct] + [
        live_accounts[(start_idx + k) % len(live_accounts)]
        for k in range(len(live_accounts))
        if live_accounts[(start_idx + k) % len(live_accounts)] is not primary_acct
    ]
    for acct in candidates:
        if acct.dead or time.time() < acct.blocked_until:
            continue
        res = acct.presence(batch)
        if res is not None:
            return acct, res
    return None, None


def poll(
    accounts,
    ids,
    poll_no=0,
    stats=None,
    cache=None,
    acct_offset=0,
    executor=None,
    on_batch=None,
    follower_capable_keys=None,
):
    """High-speed concurrent presence sweep with direct follower routing & optional streaming callback."""
    errors_before = sum(a.errors for a in accounts)
    usable = [a for a in accounts if not a.dead and time.time() >= a.blocked_until]
    if not usable and _wait_for_capacity(accounts):
        usable = [a for a in accounts if not a.dead and time.time() >= a.blocked_until]
    best, ok = {}, False
    if not usable:
        return best, ok, False

    live = [a for a in accounts if not a.dead]
    by_key = {a.key: a for a in live}
    covered_all = True
    viewer_map = cache.setdefault("viewer", {}) if cache is not None else {}
    unseeable_until = cache.setdefault("unseeable_until", {}) if cache is not None else {}
    answered_by = {}

    own_exec = executor is None
    pool_exec = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(live)))) if own_exec else executor

    try:
        # Only probe Appearing-Offline followers on seed poll 0 and once per minute thereafter
        appearing_offline_sweep = len(ids) <= WIDE_THRESHOLD and (poll_no == 0 or poll_no % 15 == 0)

        pinned_buckets = {}
        unpinned_ids = []
        for u in ids:
            vk = viewer_map.get(u)
            va = by_key.get(vk) if vk else None
            if va and not va.dead:
                pinned_buckets.setdefault(vk, []).append(u)
            else:
                unpinned_ids.append(u)

        # Prefer zero-follow sweeper accounts first for unfollowed public batches so follower accounts stay cool
        if follower_capable_keys:
            sweepers = [a for a in live if a.key not in follower_capable_keys] or live
        else:
            sweepers = live

        tasks = []
        # 1) Direct follower-routed batches (returns both placeId and gameId on Pass 1!)
        for idx_k, (vk, uids) in enumerate(pinned_buckets.items()):
            va = by_key[vk]
            for i in range(0, len(uids), PRESENCE_BATCH_SIZE):
                tasks.append((va, (acct_offset + idx_k) % len(live), uids[i:i + PRESENCE_BATCH_SIZE]))

        # 2) Unfollowed public batches distributed across sweeper ring
        ring_start = (poll_no + acct_offset) % len(sweepers)
        for j, i in enumerate(range(0, len(unpinned_ids), PRESENCE_BATCH_SIZE)):
            batch = unpinned_ids[i:i + PRESENCE_BATCH_SIZE]
            primary = sweepers[(ring_start + j) % len(sweepers)]
            tasks.append((primary, (acct_offset + j) % len(live), batch))

        futures = {}
        for t_idx, (primary, fb_idx, batch) in enumerate(tasks):
            if t_idx > 0 and BATCH_STAGGER > 0:
                time.sleep(BATCH_STAGGER)
            fut = pool_exec.submit(_batch_request_with_failover, primary, live, fb_idx, batch)
            futures[fut] = batch

        for fut in as_completed(futures):
            batch = futures[fut]
            used_acct, res = fut.result()
            if res is None:
                covered_all = False
                continue
            ok = True
            batch_updates = {}
            for p in res:
                uid = p.get("userId")
                if uid is None:
                    continue
                answered_by.setdefault(uid, set()).add(used_acct)
                if _rank(p) > _rank(best.get(uid)):
                    best[uid] = p
                    batch_updates[uid] = p
            if on_batch and batch_updates:
                on_batch(batch_updates)

        # Optional Appearing-Offline sweep for small rosters (Developers) across follower-capable accounts
        if appearing_offline_sweep:
            probe_accts = [a for a in live if not follower_capable_keys or a.key in follower_capable_keys]
            extra_futs = {}
            for a_idx, acct in enumerate(probe_accts):
                todo = [u for u in ids if acct not in answered_by.get(u, ()) and not (best.get(u) or {}).get("gameId")]
                for i in range(0, len(todo), PRESENCE_BATCH_SIZE):
                    sub = todo[i:i + PRESENCE_BATCH_SIZE]
                    if BATCH_STAGGER > 0:
                        time.sleep(BATCH_STAGGER)
                    extra_futs[pool_exec.submit(acct.presence, sub)] = (acct, sub)
            for fut in as_completed(extra_futs):
                acct, sub = extra_futs[fut]
                res = fut.result()
                if res is None:
                    continue
                ok = True
                batch_updates = {}
                for p in res:
                    uid = p.get("userId")
                    if uid is None:
                        continue
                    prev_rank = _rank(best.get(uid))
                    answered_by.setdefault(uid, set()).add(acct)
                    if _rank(p) > prev_rank:
                        best[uid] = p
                        batch_updates[uid] = p
                        if not _hidden_in_game(p):
                            viewer_map[uid] = acct.key
                            unseeable_until.pop(uid, None)
                if on_batch and batch_updates:
                    on_batch(batch_updates)

        # Pass 2: probe hidden InGame users ONLY across follower-capable accounts (skipping 0-follow sweepers!)
        now_ts = time.time()
        all_hidden = [u for u, p in best.items() if _hidden_in_game(p)]
        if stats is not None:
            stats["hidden"] += len(all_hidden)
        hidden = [u for u in all_hidden if now_ts >= unseeable_until.get(u, 0.0)]

        probe_candidates = [a for a in live if not follower_capable_keys or a.key in follower_capable_keys]
        if hidden and probe_candidates:
            for acct in probe_candidates:
                if acct.dead or time.time() < acct.blocked_until:
                    continue
                todo = [
                    u for u in hidden
                    if acct not in answered_by.get(u, ())
                    and not (best.get(u) or {}).get("gameId")
                ]
                if not todo:
                    continue
                sub_futs = {}
                for i in range(0, len(todo), PRESENCE_BATCH_SIZE):
                    sub = todo[i:i + PRESENCE_BATCH_SIZE]
                    sub_futs[pool_exec.submit(acct.presence, sub)] = sub
                for fut in as_completed(sub_futs):
                    res = fut.result()
                    if res is None:
                        continue
                    batch_updates = {}
                    for p in res:
                        uid = p.get("userId")
                        if uid is None:
                            continue
                        prev_rank = _rank(best.get(uid))
                        answered_by.setdefault(uid, set()).add(acct)
                        if _rank(p) > prev_rank:
                            best[uid] = p
                            batch_updates[uid] = p
                            if not _hidden_in_game(p):
                                viewer_map[uid] = acct.key
                                unseeable_until.pop(uid, None)
                    if on_batch and batch_updates:
                        on_batch(batch_updates)

        if cache is not None:
            min_checked = max(1, len(probe_candidates) - 1) if probe_candidates else 1
            for u in hidden:
                if _hidden_in_game(best.get(u) or {}):
                    checked_capable = sum(1 for a in answered_by.get(u, ()) if a in probe_candidates)
                    if checked_capable >= min_checked:
                        unseeable_until[u] = time.time() + HIDDEN_RETRY_TTL
            # Also mark target-game players whose server gameId is hidden across all follower accounts
            # so detect() announces them immediately without waiting 12-30s on an unseeable VIP server ID.
            unseeable_job = cache.setdefault("unseeable_job", set())
            for u, p in best.items():
                if p.get("userPresenceType") == 2 and not p.get("gameId"):
                    vk = viewer_map.get(u)
                    if vk and by_key.get(vk) in answered_by.get(u, ()):
                        unseeable_job.add(u)
                elif p.get("gameId"):
                    unseeable_job.discard(u)
    finally:
        if own_exec:
            pool_exec.shutdown(wait=True)

    complete = ok and covered_all and sum(a.errors for a in accounts) == errors_before
    return best, ok, complete


def is_hunt(p, place_id, universe_id, any_game=False):
    """True when presence record `p` is in-game in the target game/universe (or any game if any_game)."""
    if not p or p.get("userPresenceType") != 2:
        return False
    if any_game:
        return bool(p.get("placeId") or p.get("rootPlaceId") or p.get("universeId"))
    if universe_id and p.get("universeId") == universe_id:
        return True
    return place_id in (p.get("placeId"), p.get("rootPlaceId"))


# ---------------------------------------------------------------- state / join detection

LOAD_INFO = {"result": "not loaded", "age": None}


def fresh_state():
    return {
        "hunt": {},
        "pending": {},
        "last_job": {},
        "last_place": {},
        "target": None,
        "updated": 0,
        "members": [],
        "members_ts": 0,
        "followers": {},
        "followers_accts": [],
        "followers_ts": 0,
    }


def load_state(path, target_sig=None, target_ids=None, shard=0, shards=1):
    """Load persisted state, pruning to `target_ids` on target change and to `uid % shards == shard`."""
    try:
        st = json.loads(Path(path).read_text(encoding="utf-8"))
        st["hunt"] = {int(k): float(v) for k, v in st.get("hunt", {}).items() if int(k) % shards == shard}
        st["pending"] = {int(k): float(v) for k, v in st.get("pending", {}).items() if int(k) % shards == shard}
        st["last_job"] = {int(k): v for k, v in st.get("last_job", {}).items() if int(k) % shards == shard}
        st["last_place"] = {int(k): v for k, v in st.get("last_place", {}).items() if int(k) % shards == shard}
        st["followers"] = {int(k): v for k, v in (st.get("followers") or {}).items()}
        st.setdefault("followers_accts", [])
        st.setdefault("followers_ts", 0)
        st.setdefault("members", [])
        st.setdefault("members_ts", 0)
    except (OSError, ValueError):
        LOAD_INFO.update(result="none (fresh start)", age=None)
        st = fresh_state()
        st["target"] = target_sig
        return st, False

    age = time.time() - st.get("updated", 0)
    if age > STATE_MAX_STALE:
        log("state is stale, reseeding without announcements")
        LOAD_INFO.update(result="STALE (reseeded silently)", age=age)
        fresh = fresh_state()
        fresh["members"] = st.get("members", [])
        fresh["members_ts"] = st.get("members_ts", 0)
        fresh["followers"] = st.get("followers", {})
        fresh["followers_accts"] = st.get("followers_accts", [])
        fresh["followers_ts"] = st.get("followers_ts", 0)
        fresh["target"] = target_sig
        return fresh, False

    LOAD_INFO.update(result="loaded", age=age)
    now = time.time()
    prev_target = st.get("target")
    if target_sig and prev_target != target_sig:
        keep = {uid for uid in st["hunt"] if not target_ids or st["last_place"].get(uid) in target_ids}
        st["hunt"] = {uid: now for uid in keep}
        st["pending"] = {uid: t for uid, t in st["pending"].items() if uid in keep}
        st["last_job"] = {uid: j for uid, j in st["last_job"].items() if uid in keep}
        st["last_place"] = {uid: p for uid, p in st["last_place"].items() if uid in keep}
        st["target"] = target_sig
        log(f"target changed ({prev_target} -> {target_sig}): retained {len(keep)} matching session(s)")
    else:
        st["hunt"] = {uid: now for uid in st["hunt"]}
    return st, True


def save_state(path, st):
    st["updated"] = time.time()
    out = dict(st)
    if isinstance(out.get("followers"), dict):
        out["followers"] = {str(k): v for k, v in out["followers"].items()}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = f"{path}.tmp"
    Path(tmp).write_text(json.dumps(out), encoding="utf-8")
    os.replace(tmp, path)


def detect(st, in_hunt, now, seeded, grace, wait_for_job, immediate_no_job=None, prune=True):
    """Update state from users currently in target game; return uids ready to announce."""
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
            continue
        if prev_job is None and not job and last is not None and now - last <= grace and same_place and uid not in st["pending"]:
            # Already announced earlier without visible job ID (VIP/Joins-Off session continuing)
            continue
        if job and prev_job and job != prev_job:
            st["pending"][uid] = now
        elif place and prev_place and place != prev_place:
            st["pending"].setdefault(uid, now)
        elif last is None or now - last > grace:
            st["pending"].setdefault(uid, now)
        elif place and prev_place is None:
            st["last_place"][uid] = place

        first = st["pending"].get(uid)
        job_confirmed_unseeable = bool(immediate_no_job and uid in immediate_no_job)
        if first is not None and (job or job_confirmed_unseeable or now - first >= wait_for_job):
            ready.append(uid)
            if job:
                st["last_job"][uid] = job
            elif prev_job and place and prev_place and place != prev_place:
                st["last_job"].pop(uid, None)
            if place:
                st["last_place"][uid] = place

    if prune:
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
    """Escape characters that would break a Discord masked-link label without ugly literal slashes."""
    for ch in "\\[]*~`|":
        s = s.replace(ch, "\\" + ch)
    return re.sub(r"(?<![^\W_])_|_(?![^\W_])", r"\\_", s)


JOIN_REDIRECT_BASE = "https://hunt-join.pages.dev/"


def join_url(p):
    job = (p or {}).get("gameId")
    place = (p or {}).get("placeId") or (p or {}).get("rootPlaceId")
    if not job or not place:
        return None
    return f"{JOIN_REDIRECT_BASE}?placeId={place}&gameInstanceId={job}"


def avatar_urls_batch(uids):
    """Fetch avatar headshot URLs in batches of up to 100 user IDs."""
    out = {}
    for i in range(0, len(uids), 100):
        chunk = uids[i:i + 100]
        try:
            r = requests.get(
                "https://thumbnails.roblox.com/v1/users/avatar-headshot",
                params={"userIds": ",".join(str(u) for u in chunk), "size": "150x150", "format": "Png"},
                timeout=8,
            )
            if r.status_code == 200:
                for d in r.json().get("data", []):
                    tid = d.get("targetId")
                    if tid and d.get("state") == "Completed" and d.get("imageUrl"):
                        out[int(tid)] = d["imageUrl"]
        except Exception:
            pass
    return out


def avatar_url(uid):
    return avatar_urls_batch([uid]).get(int(uid))


def build_payload(uid, name, p, cfg, joined_at, thumb=None, test=False, game_name=None):
    display = md_escape(name.get("displayName") or name.get("username") or f"User {uid}")
    username = md_escape(name.get("username") or str(uid))
    profile = f"https://www.roblox.com/users/{uid}/profile"
    job = (p or {}).get("gameId")
    server = f"`{job}`" if job else "`server not visible`"
    game_label = game_name or DEFAULT_GAME_NAME
    desc = (f"- {cfg['emoji']} [{display} ({username})]({profile}) has joined **{game_label}**!\n"
            f"<t:{int(joined_at)}:R>\u30fb{server}")
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
        payload["components"] = [{"type": 1, "components": [
            {"type": 2, "style": 5, "label": "Join \u2197", "emoji": {"name": "\U0001F465"}, "url": url}]}]
    return payload


def with_text_link(payload):
    out = json.loads(json.dumps(payload))
    comps = out.pop("components", None)
    url = comps[0]["components"][0].get("url") if comps else None
    if url:
        out["embeds"][0]["description"] += f"\n[Join \u2197]({url})"
    return out


_WEBHOOK_SESS = requests.Session()


def _post(url, payload):
    sep = "&" if "?" in url else "?"
    full = f"{url}{sep}wait=true&with_components=true"
    status = None
    for attempt in (1, 2, 3):
        try:
            r = _WEBHOOK_SESS.post(full, json=payload, timeout=12)
        except requests.RequestException as e:
            log(f"webhook error {type(e).__name__} (attempt {attempt})")
            time.sleep(1.5)
            continue
        status = r.status_code
        if status in (200, 204):
            try:
                return True, r.json(), status
            except ValueError:
                return True, None, status
        if status == 429:
            try:
                wait = float(r.json().get("retry_after", 2))
            except Exception:
                wait = 2.0
            time.sleep(min(wait, 15) + 0.3)
            continue
        if status >= 500:
            time.sleep(1.5)
            continue
        log(f"webhook rejected: HTTP {status} {r.text[:200]}")
        return False, None, status
    return False, None, status


def post_webhook(url, payload, dry_run):
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
                _WEBHOOK_SESS.patch(f"{url.split('?')[0]}/messages/{msg['id']}", json={"embeds": fixed["embeds"]}, timeout=12)
            except requests.RequestException:
                pass
        return True, accepted
    return ok, None


# ---------------------------------------------------------------- roster & follower pre-routing

def load_config(name):
    data = yaml.safe_load(Path("config/trackers.yaml").read_text(encoding="utf-8")) or {}
    return (data.get("notifiers") or {}).get(name), {t["id"]: t for t in data.get("trackers", [])}, data.get("settings", {})


def parse_color(c):
    if c is None:
        return None
    return c if isinstance(c, int) else int(str(c).lstrip("#"), 16)


def resolve_ids(tracker, st, shard=0, shards=1):
    """Return full deduplicated user list, filtered deterministically to `uid % shards == shard`."""
    ids = []
    if tracker.get("idsFile"):
        ids.extend(load_user_ids(tracker["idsFile"]))
    if tracker.get("groupSources"):
        prev = list(st.get("members") or [])
        if prev and time.time() - st.get("members_ts", 0) < GROUP_SOURCES_TTL:
            ids.extend(prev)
        else:
            try:
                synced = [m["userId"] for m in fetch_group_sources(tracker["groupSources"]) if m.get("userId")]
            except Exception as e:
                log(f"groupSources fetch warning: {type(e).__name__}")
                synced = []
            if synced:
                st["members"], st["members_ts"] = synced, time.time()
                ids.extend(synced)
                log(f"synced {len(synced)} groupSources members")
            elif prev:
                ids.extend(prev)
    elif tracker.get("groupId"):
        gid = int(tracker["groupId"])
        prev = list(st.get("members") or [])
        expected = None
        try:
            rg = requests.get(f"https://groups.roblox.com/v1/groups/{gid}", timeout=8, headers={"User-Agent": UA})
            if rg.status_code == 200:
                expected = rg.json().get("memberCount")
        except Exception:
            pass
        cache_complete = (not expected) or len(prev) >= expected * 0.9
        if prev and cache_complete and time.time() - st.get("members_ts", 0) < MEMBERS_TTL:
            ids.extend(prev)
        else:
            if prev and not cache_complete:
                log(f"cached member list looks truncated ({len(prev)} < {expected}), re-fetching full group {gid}")
            members = fetch_group_members(gid)
            fetched = [m["userId"] for m in members if m.get("userId")]
            is_complete = fetched and ((not expected and len(fetched) >= len(prev) * 0.9) or (expected and len(fetched) >= expected * 0.9))
            if is_complete:
                st["members"], st["members_ts"] = fetched, time.time()
                ids.extend(fetched)
                log(f"fetched {len(fetched)} group members")
            elif fetched:
                merged = list(dict.fromkeys(prev + fetched))
                st["members"] = merged
                ids.extend(merged)
                log(f"partial group fetch ({len(fetched)}/{expected or '?'}); keeping merged {len(merged)} members for retry next tick")
            else:
                ids.extend(prev)
    ids += [int(u) for u in tracker.get("extraIds", [])]
    deduped = list(dict.fromkeys(int(u) for u in ids))
    if shards > 1:
        return [u for u in deduped if u % shards == shard]
    return deduped


def refresh_follower_routing(pool, st, viewer_map, capable_keys_set, shard=0):
    """Populate `viewer_map` (`uid -> acct_key`) & `capable_keys_set` in background once per hour."""
    try:
        if shard > 0:
            time.sleep((shard % 4) * 1.2)
        new_map = {}
        capable = []
        for key, ck in pool:
            hdr = {"Cookie": f".ROBLOSECURITY={ck}"}
            mid = _get_my_id(hdr)
            if not mid:
                continue
            follows = _get_followings(mid, hdr)
            if follows:
                capable.append(key)
                for uid in follows:
                    new_map.setdefault(int(uid), key)
        if capable:
            viewer_map.update(new_map)
            capable_keys_set.clear()
            capable_keys_set.update(capable)
            st["followers"] = dict(viewer_map)
            st["followers_accts"] = list(capable)
            st["followers_ts"] = time.time()
            log(f"follower routing refreshed: {len(new_map)} followed users across {len(capable)}/{len(pool)} accounts")
    except Exception as e:
        log(f"follower routing background refresh warning: {type(e).__name__}")


def warm_metadata_cache(ids, names_dict, thumbs_dict, want_avatar=True):
    """Background batch pre-warmer so join alerts never block on user info / thumbnail HTTP calls."""
    try:
        for i in range(0, len(ids), 100):
            chunk = [u for u in ids[i:i + 100] if u not in names_dict]
            if chunk:
                names_dict.update(fetch_user_infos(chunk))
            if want_avatar:
                t_chunk = [u for u in ids[i:i + 100] if u not in thumbs_dict]
                if t_chunk:
                    thumbs_dict.update(avatar_urls_batch(t_chunk))
            time.sleep(0.15)
    except Exception:
        pass


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notifier", required=True, help="name under notifiers: in config/trackers.yaml")
    ap.add_argument("--shard", type=int, default=0, help="0-based notifier shard index")
    ap.add_argument("--shards", type=int, default=1, help="total notifier shards for this tracker")
    ap.add_argument("--state", default=None)
    ap.add_argument("--duration", type=int, default=286, help="seconds to keep polling (0 = forever)")
    ap.add_argument("--interval", type=int, default=None, help="seconds between polls, start to start")
    ap.add_argument("--grace", type=int, default=180, help="absent this long before a return counts as a new join")
    ap.add_argument("--wait-for-job", type=int, default=12, help="max seconds to wait for server id if still uncertain")
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
    interval = args.interval or int(ncfg.get("interval", 15))
    shards = max(1, int(args.shards))
    shard = max(0, min(shards - 1, int(args.shard)))
    default_state = f"state/notifier_{args.notifier}.json" if shards == 1 else f"state/notifier_{args.notifier}-{shard}.json"
    state_path = args.state or default_state
    webhook = (os.environ.get("WEBHOOK") or os.environ.get(ncfg.get("webhookSecret", ""), "")).strip()
    if not webhook and not args.dry_run:
        print(f"::warning title=notifier {args.notifier}::webhook secret {ncfg.get('webhookSecret')} is not set, notifier disabled")
        return 0

    place_id = int(settings.get("targetPlaceId", DEFAULT_PLACE_ID))
    universe_id = int(settings.get("targetUniverseId", DEFAULT_UNIVERSE_ID))
    any_game = bool(tracker.get("anyGame", False))
    target_game_name = DEFAULT_GAME_NAME
    if not any_game:
        try:
            target_game_name = resolve_game_name(universe_id) or DEFAULT_GAME_NAME
        except Exception:
            target_game_name = DEFAULT_GAME_NAME

    if args.test:
        if shard != 0:
            return 0
        uid = (load_user_ids(tracker["idsFile"]) if tracker.get("idsFile") else [1])[0]
        try:
            name = fetch_user_infos([uid]).get(uid, {})
        except Exception:
            name = {}
        sample = {"userPresenceType": 2, "placeId": place_id, "rootPlaceId": place_id,
                  "gameId": "00000000-0000-0000-0000-000000000000"}
        sample_game = "a Test Game" if any_game else target_game_name
        thumb = avatar_url(uid) if cfg["thumbnail"] == "avatar" else None
        ok, button = post_webhook(webhook, build_payload(uid, name, sample, cfg, time.time(), thumb, test=True, game_name=sample_game), args.dry_run)
        print(f"::notice title=notifier {args.notifier} test::sent={ok} button_accepted={button} thumbnail={'yes' if thumb else 'no'}")
        return 0 if ok else 1

    pool = _get_all_cookies()
    if not pool:
        log("no ROBLOSECURITY cookies configured; presence requires an account")
        return 1
    accounts = [Account(k, c) for k, c in pool]

    target_sig = "any" if any_game else f"{place_id}:{universe_id}"
    target_ids = None if any_game else {place_id, universe_id}
    st, seeded = load_state(state_path, target_sig=target_sig, target_ids=target_ids, shard=shard, shards=shards)
    ids = resolve_ids(tracker, st, shard=shard, shards=shards)
    if not ids:
        log("no users to watch")
        return 1

    names = {}
    thumbs = {}
    game_name_cache = {}
    viewer_map = dict(st.get("followers") or {})
    capable_keys = set(st.get("followers_accts") or [])
    poll_cache = {"viewer": viewer_map, "unseeable_until": {}, "unseeable_job": set()}

    # Non-blocking background pre-warm of follower map (if TTL expired) + user names/avatars
    if time.time() - st.get("followers_ts", 0) >= FOLLOWERS_TTL or not capable_keys:
        threading.Thread(
            target=refresh_follower_routing,
            args=(pool, st, viewer_map, capable_keys, shard),
            daemon=True,
        ).start()
    threading.Thread(
        target=warm_metadata_cache,
        args=(ids, names, thumbs, cfg["thumbnail"] == "avatar"),
        daemon=True,
    ).start()

    label = f"{args.notifier}[{shard}/{shards}]" if shards > 1 else args.notifier
    log(f"[{label}] watching {len(ids)} users, {len(accounts)} accounts ({len(capable_keys)} pre-mapped follower accts), "
        f"parallel streaming sweeps every {interval}s for {'ever' if not args.duration else str(args.duration) + 's'} "
        f"(target={'anyGame' if any_game else f'{target_game_name} [{place_id}]'})")

    def _game_name_for(p):
        if not any_game:
            return target_game_name
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

    sent = 0
    state_lock = threading.Lock()

    def _announce_uids(uids, lookup_map, ts_now):
        nonlocal sent
        missing_names = [u for u in uids if u not in names]
        if missing_names:
            try:
                names.update(fetch_user_infos(missing_names))
            except Exception:
                pass
        if cfg["thumbnail"] == "avatar":
            missing_thumbs = [u for u in uids if u not in thumbs]
            if missing_thumbs:
                thumbs.update(avatar_urls_batch(missing_thumbs))
        for uid in uids:
            p = lookup_map[uid]
            joined_at = st["pending"].get(uid, ts_now)
            thumb = thumbs.get(uid) if cfg["thumbnail"] == "avatar" else None
            g_name = _game_name_for(p)
            log(f"announcing {uid} ({g_name or target_game_name}, server id {'yes' if p.get('gameId') else 'no'})")
            sent_ok, _ = post_webhook(webhook, build_payload(uid, names.get(uid, {}), p, cfg, joined_at, thumb, game_name=g_name), args.dry_run)
            if sent_ok:
                st["pending"].pop(uid, None)
                sent += 1

    start = time.time()
    polls = good_polls = 0
    sweep_secs = []
    pstats = Counter()
    ring_slots = {"admin": 0, "video-stars": 2, "developers": 4}
    acct_offset = (ring_slots.get(args.notifier, 0) + shard * 2) % max(1, len(accounts))

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, max(1, len(accounts)))) as executor:
        while True:
            t0 = time.time()
            announced_in_sweep = set()

            def _on_batch_stream(batch_records):
                # Stream immediate Discord alerts mid-sweep the instant a join with server ID arrives
                if not seeded:
                    return
                with state_lock:
                    now_b = time.time()
                    ready_b = {
                        u: p for u, p in batch_records.items()
                        if u not in announced_in_sweep
                        and p.get("gameId")
                        and is_hunt(p, place_id, universe_id, any_game)
                    }
                    if not ready_b:
                        return
                    uids_now = detect(st, ready_b, now_b, seeded=True, grace=args.grace, wait_for_job=args.wait_for_job, prune=False)
                    if uids_now:
                        announced_in_sweep.update(uids_now)
                        _announce_uids(uids_now, ready_b, now_b)

            best, ok, complete = poll(
                accounts,
                ids,
                polls,
                pstats,
                cache=poll_cache,
                acct_offset=acct_offset,
                executor=executor,
                on_batch=_on_batch_stream,
                follower_capable_keys=capable_keys,
            )
            polls += 1
            sweep_dt = time.time() - t0
            sweep_secs.append(sweep_dt)
            if ok:
                good_polls += 1
                with state_lock:
                    in_hunt = {u: p for u, p in best.items() if is_hunt(p, place_id, universe_id, any_game)}
                    ready = detect(
                        st,
                        in_hunt,
                        time.time(),
                        seeded,
                        args.grace,
                        args.wait_for_job,
                        immediate_no_job=poll_cache.get("unseeable_job"),
                        prune=True,
                    )
                    ready = [u for u in ready if u not in announced_in_sweep]
                    if not seeded and (complete or time.time() - start >= SEED_MAX_WAIT):
                        what = "in-game (any game)" if any_game else f"already in {target_game_name}"
                        log(f"seeded: {len(in_hunt)} {what}, not announcing" + ("" if complete else " (incomplete after waiting)"))
                        seeded = True
                    if ready:
                        _announce_uids(ready, in_hunt, time.time())
                    if viewer_map:
                        st["followers"] = dict(viewer_map)
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
    print(f"::notice title=notifier {label}::{good_polls}/{polls} polls ok, {sent} announcement(s), "
          f"{len(st['hunt'])} {'in-game (any game)' if any_game else f'in {target_game_name}'}, {len(ids)} watched, "
          f"avg hidden {pstats['hidden'] / max(1, polls):.0f}, avg sweep {sum(sweep_secs)/len(sweep_secs):.1f}s, "
          f"state at start: {state_note} | "
          f"rate-limited accounts {limited}, disabled accounts {dead} of {len(accounts)} in pool | "
          f"presence requests {total_req} in {time.time()-start:.0f}s, "
          f"429s {n429}, retry-after {waits[0] if waits else '-'}..{waits[-1] if waits else '-'}s")
    return 0 if good_polls else 1


if __name__ == "__main__":
    sys.exit(main())
