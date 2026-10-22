#!/usr/bin/env python3
"""
Developer IDs Presence Scanner — checks a list of user IDs for Hunt presence.
Reads IDs from config/developer_ids.txt (one per line, # comments).
Supports sharding 3x and same JSON output as group scanner for discord_updater.

Usage:
  python developer_scanner.py --user-ids-file config/developer_ids.txt --place-id 74205509034203 --json shard_0.json --shard 0 --shards 3
"""

import argparse
import json
import time
import sys
import os
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests

# Reuse same API endpoints and logic as group scanner but simplified
PRESENCE_API = "https://presence.roblox.com/v1/presence/users"
UNIVERSE_API = "https://apis.roblox.com/universes/v1/places/{place_id}/universe"
GAMES_API = "https://games.roblox.com/v1/games?universeIds={universe_id}"
USERS_API = "https://users.roblox.com/v1/users/{user_id}"
PRESENCE_BATCH_SIZE = 50
DEFAULT_PLACE_ID = 116463962153167
DEFAULT_UNIVERSE_ID = 10770728771
DEFAULT_GAME_NAME = "The Hollow"

# ================= Cookie support (fixes Offline-but-Follow-shows-Hunt) =================
# Reads alt cookie that Follows the 102 devs. Presence then returns InGame instead of Offline.
# Supports: ROBLOSECURITY, ROBLOX_COOKIE, ROBLOSECURITY_1 (all read, first found used)
def _get_roblox_cookie() -> str:
    for key in ["ROBLOX_COOKIE", "ROBLOSECURITY"] + [f"ROBLOSECURITY_{i}" for i in range(1, 21)]:
        val = os.environ.get(key, "")
        if val and val.strip():
            v = val.strip().strip('"').strip("'")
            if v.startswith(".ROBLOSECURITY="):
                v = v.split("=", 1)[1]
            if v:
                return v
    return ""

def _cookie_headers() -> Dict[str, str]:
    base = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
    ck = _get_roblox_cookie()
    if ck:
        base["Cookie"] = f".ROBLOSECURITY={ck}"
    return base

def _get_all_cookies():
    """Return list of (key, cookie) for all configured accounts, deduped by value. ROBLOX_COOKIE takes priority."""
    pool=[]
    seen=set()
    for key in ["ROBLOX_COOKIE", "ROBLOSECURITY"] + [f"ROBLOSECURITY_{i}" for i in range(1, 21)]:
        val = __import__("os").environ.get(key, "")
        if val and val.strip():
            v = val.strip().strip('"').strip("'")
            if v.startswith(".ROBLOSECURITY="):
                v = v.split("=",1)[1].strip().strip('"').strip("'")
            if v and v not in seen:
                seen.add(v)
                pool.append((key, v))
    return pool

def _log_cookie_status():
    pool=_get_all_cookies()
    if pool:
        print(f"Cookie pool: {len(pool)} accounts — {[k for k,_ in pool]}", file=sys.stderr)
        for k,ck in pool:
            print(f"  {k}: len {len(ck)} warn={'_|WARNING' in ck}", file=sys.stderr)
        # Quick validation via mobileapi for first cookie (status code only; never log the body)
        try:
            import requests as _rq
            _, ck0 = pool[0]
            r=_rq.get("https://www.roblox.com/mobileapi/userinfo", timeout=10, headers={"User-Agent":"Roblox/Android","Cookie": f".ROBLOSECURITY={ck0}", "Referer":"https://www.roblox.com/"})
            print(f"mobileapi check {r.status_code}", file=sys.stderr)
        except Exception as e:
            print(f"mobileapi check error {type(e).__name__}", file=sys.stderr)
        return True
    else:
        print("No ROBLOSECURITY/ROBLOX_COOKIE found — unauthenticated presence (Offline-hidden devs will stay Offline)", file=sys.stderr)
        return False

def _get_my_id(headers):
    # Query lightweight /v1/users/authenticated JSON endpoint first (with 429 backoff);
    # keep /home HTML check only as a fallback.
    try:
        import requests as _req, sys, re
        ck = headers.get("Cookie", "")
        if not ck:
            return None
        for attempt in range(1, 5):
            r = _req.get(
                "https://users.roblox.com/v1/users/authenticated",
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0", "Cookie": ck, "Referer": "https://www.roblox.com/", "Origin": "https://www.roblox.com", "Accept": "application/json"},
            )
            if r.status_code == 200:
                uid = r.json().get("id")
                print(f"my_id {uid} ok", file=sys.stderr)
                return uid
            if r.status_code == 429 and attempt < 4:
                try:
                    wait = float(r.headers.get("Retry-After") or 0)
                except ValueError:
                    wait = 0.0
                time.sleep(max(wait, 2.0 * attempt))
                continue
            print(f"my_id status {r.status_code} body {r.text[:200]!r}", file=sys.stderr)
            break
        try:
            s = _req.Session()
            s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.roblox.com/", "Cookie": ck})
            rh = s.get("https://www.roblox.com/home", timeout=10)
            auth_m = re.search(r'"isAuthenticated"\s*:\s*true', rh.text)
            uid_m = re.search(r'"userId"\s*:\s*(\d+)', rh.text)
            if auth_m and uid_m:
                return int(uid_m.group(1))
        except Exception as e:
            print(f"home auth check error {e}", file=sys.stderr)
    except Exception as e:
        print(f"my_id error {e}", file=__import__("sys").stderr)
    return None

def _seeded_session(ck):
    import requests as _req
    s=_req.Session()
    s.headers.update({"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36","Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Accept-Language":"en-US,en;q=0.9","Referer":"https://www.roblox.com/","Cookie": ck})
    try:
        s.get("https://www.roblox.com/home", timeout=10)
    except: pass
    return s

def _get_csrf_from_session(sess):
    """Try HTML CSRF first (www not 9002-blocked), fallback to auth."""
    import re, sys
    try:
        rh=sess.get("https://www.roblox.com/home", timeout=10, headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)","Referer":"https://www.roblox.com/"})
        if rh.status_code==200:
            m=re.search(r'<meta\s+name="csrf-token"\s+[^>]*data-token="([^"]+)"', rh.text)
            if not m:
                m=re.search(r'<meta\s+name="csrf-token"\s+[^>]*content="([^"]+)"', rh.text)
            if not m:
                m=re.search(r"Roblox\.XsrfToken\.setToken\('([^']+)'\)", rh.text)
            if m:
                return m.group(1)
    except Exception as e:
        print(f"csrf html error {e}", file=sys.stderr)
    try:
        r=sess.post("https://auth.roblox.com/v2/logout", timeout=10, headers={"User-Agent":"Mozilla/5.0","Referer":"https://www.roblox.com/","Origin":"https://www.roblox.com"})
        tok=r.headers.get("x-csrf-token")
        if tok:
            return tok
    except: pass
    return None

def _profile_shows_hunt(user_id, headers, target_place_id=DEFAULT_PLACE_ID, target_universe_id=DEFAULT_UNIVERSE_ID, prefer_cookie=""):
    """Check only embedded JSON presence payloads on a profile page — never global promo tags like data-purple."""
    import re as _re, requests as _req, sys
    target_pid_s = str(target_place_id)
    ck = prefer_cookie
    if not ck:
        pool = _get_all_cookies()
        ck = f".ROBLOSECURITY={pool[0][1]}" if pool else headers.get("Cookie", "")
    try:
        h2 = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": f"https://www.roblox.com/users/{user_id}/profile",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if ck:
            h2["Cookie"] = ck
        r = _req.get(f"https://www.roblox.com/users/{user_id}/profile", timeout=6, headers=h2)
        if r.status_code == 200:
            # Strip global HTML/meta tags (e.g. sitewide data-purple event links on every Roblox page)
            cleaned = _re.sub(r"<meta\b[^>]*>", "", r.text, flags=_re.IGNORECASE)
            cleaned = _re.sub(r'data-[a-z0-9_-]+\s*=\s*"[^"]*"', "", cleaned, flags=_re.IGNORECASE)
            if (
                f'"placeId":{target_pid_s}' in cleaned
                or f'"placeId":"{target_pid_s}"' in cleaned
                or (target_universe_id and (
                    f'"universeId":{target_universe_id}' in cleaned
                    or f'"universeId":"{target_universe_id}"' in cleaned
                ))
            ):
                print(f"profile scrape {user_id}: found embedded JSON target presence", file=sys.stderr)
                return True
    except Exception as e:
        print(f"profile scrape {user_id} HTML error {e}", file=sys.stderr)
    return False
def _get_followings(my_id, headers):
    following=set()
    if not my_id:
        return following
    try:
        import requests as _req
        ck=headers.get("Cookie","")
        sess=_req.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Referer": "https://www.roblox.com/",
            "Origin": "https://www.roblox.com",
            "Accept": "application/json",
            "Cookie": ck,
        })
        cursor=""
        while True:
            url=f"https://friends.roblox.com/v1/users/{my_id}/followings?limit=100&sortOrder=Asc"
            if cursor:
                url+=f"&cursor={cursor}"
            r=None
            for attempt in range(1, 7):
                r=sess.get(url, timeout=12)
                if r.status_code==200:
                    break
                if r.status_code==429:
                    # friends.roblox.com enforces ~10 req / ~37s per runner IP while returning Retry-After: 5;
                    # 13s steps cleanly bridge the sliding window without losing later pages/accounts.
                    try:
                        ra=float(r.headers.get("Retry-After") or 0)
                    except ValueError:
                        ra=0.0
                    wait=max(ra, 13.0)
                    print(f"followings 429 attempt {attempt}/6 for {my_id}, waiting {wait:.1f}s", file=__import__("sys").stderr)
                    time.sleep(wait)
                    continue
                print(f"followings fetch status {r.status_code} body {r.text[:200]!r}", file=__import__("sys").stderr)
                if r.status_code in (401,403) and attempt == 1:
                    r2=_req.get(url, timeout=12, headers={"User-Agent":"Mozilla/5.0","Cookie": ck, "Referer":"https://www.roblox.com/"})
                    print(f"followings retry status {r2.status_code}", file=__import__("sys").stderr)
                    r=r2
                break
            if not r or r.status_code!=200:
                break
            j=r.json()
            for e in j.get("data",[]):
                following.add(e["id"])
            cursor=j.get("nextPageCursor")
            if not cursor:
                break
            time.sleep(0.25)
    except Exception as e:
        print(f"followings error {e}", file=__import__("sys").stderr)
    return following

def resolve_universe_id(place_id: int) -> Optional[int]:
    for attempt in range(1, 4):
        try:
            r = requests.get(UNIVERSE_API.format(place_id=place_id), timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                uid = r.json().get("universeId")
                if uid is not None:
                    return uid
            if r.status_code == 429 and attempt < 3:
                time.sleep(2 * attempt)
                continue
        except Exception:
            if attempt < 3:
                time.sleep(1 + attempt)
                continue
        break
    return DEFAULT_UNIVERSE_ID if int(place_id) == DEFAULT_PLACE_ID else None

def resolve_game_name(universe_id: int) -> Optional[str]:
    try:
        r = requests.get(GAMES_API.format(universe_id=universe_id), timeout=10)
        if r.status_code == 200:
            j = r.json()
            if j.get("data"):
                return j["data"][0].get("name")
    except:
        pass
    return None

def load_user_ids(path: str) -> List[int]:
    p = Path(path)
    if not p.exists():
        print(f"IDs file not found: {path}", file=sys.stderr)
        sys.exit(1)
    ids = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line=line.strip()
        if not line or line.startswith("#"):
            continue
        # Allow comma/space separated
        for part in line.replace(","," ").split():
            part=part.strip()
            if part.isdigit():
                ids.append(int(part))
    # Deduplicate preserve order
    seen=set()
    uniq=[]
    for i in ids:
        if i not in seen:
            seen.add(i)
            uniq.append(i)
    if not uniq:
        print(f"No IDs found in {path}", file=sys.stderr)
        sys.exit(1)
    return uniq

def fetch_user_infos(user_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """Batched POST to users.roblox.com/v1/users — 1 request per 100 IDs, with 429 retry.
    Replaces sequential GET loop that 429s after ~7 requests (see 2026-09-22 debug: 28/35 failed with 0.05s delay).
    """
    result: Dict[int, Dict[str, Any]] = {}
    if not user_ids:
        return result
    # dedup preserve order
    seen=set()
    uniq=[]
    for uid in user_ids:
        if uid not in seen:
            seen.add(uid)
            uniq.append(uid)
    import random as _rand
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json", "Accept": "application/json"}
    for start in range(0, len(uniq), 100):
        batch = uniq[start:start+100]
        for attempt in range(1, 6):
            try:
                r = requests.post("https://users.roblox.com/v1/users", json={"userIds": batch}, timeout=15, headers=headers)
                if r.status_code == 429:
                    wait = r.headers.get("Retry-After")
                    try:
                        wait_f = float(wait) if wait else (2 ** attempt)
                    except:
                        wait_f = 2 ** attempt
                    # datacenter 429 bursts: wait proper time
                    print(f"Users batch {start//100} 429 attempt {attempt} wait {wait_f:.1f}s", file=sys.stderr)
                    time.sleep(wait_f + _rand.uniform(0, 1))
                    continue
                if r.status_code == 200:
                    for entry in r.json().get("data", []):
                        uid = entry.get("id")
                        if uid is not None:
                            result[uid] = {"userId": uid, "username": entry.get("name"), "displayName": entry.get("displayName"), "hasVerifiedBadge": entry.get("hasVerifiedBadge", False)}
                    break
                else:
                    print(f"Users batch {start//100} attempt {attempt} status {r.status_code}: {r.text[:200]}", file=sys.stderr)
                    time.sleep(1 + attempt + _rand.uniform(0, 1))
            except Exception as e:
                print(f"Users batch error {e} attempt {attempt}", file=sys.stderr)
                time.sleep(1 + attempt + _rand.uniform(0, 1))
        # fill any missing in this batch (fallback single GET, then user<id>)
        for uid in batch:
            if uid not in result:
                try:
                    rg = requests.get(USERS_API.format(user_id=uid), timeout=8, headers={"User-Agent": "Mozilla/5.0"})
                    if rg.status_code == 200:
                        j = rg.json()
                        result[uid] = {"userId": j.get("id"), "username": j.get("name"), "displayName": j.get("displayName"), "hasVerifiedBadge": j.get("hasVerifiedBadge", False)}
                        print(f"Users batch fallback GET ok {uid} -> {result[uid]['username']}", file=sys.stderr)
                        time.sleep(0.2)
                        continue
                    else:
                        print(f"Users fallback GET {uid} status {rg.status_code}", file=sys.stderr)
                except Exception as e:
                    print(f"Users fallback GET {uid} error {e}", file=sys.stderr)
                result[uid] = {"userId": uid, "username": f"user{uid}", "displayName": f"user{uid}", "hasVerifiedBadge": False}
                print(f"Users fallback user{uid} for failed batch (will show as user(UserID))", file=sys.stderr)
        if start + 100 < len(uniq):
            time.sleep(0.4 + _rand.uniform(0, 0.3))
    return result

def fetch_user_info(user_id: int) -> Dict[str, Any]:
    # wrapper using batched endpoint with retry (fixes sequential GET 429)
    try:
        infos = fetch_user_infos([user_id])
        if user_id in infos and infos[user_id].get("username") and not infos[user_id]["username"].startswith("user"):
            return infos[user_id]
        # if fallback triggered, still return it (better than extra GET)
        if user_id in infos:
            return infos[user_id]
    except Exception as e:
        print(f"fetch_user_info batch wrapper error {e}", file=sys.stderr)
    # last resort single GET (old behaviour)
    try:
        r = requests.get(USERS_API.format(user_id=user_id), timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            j=r.json()
            return {"userId": j.get("id"), "username": j.get("name"), "displayName": j.get("displayName"), "hasVerifiedBadge": j.get("hasVerifiedBadge", False)}
    except:
        pass
    return {"userId": user_id, "username": f"user{user_id}", "displayName": f"user{user_id}", "hasVerifiedBadge": False}

_POOL_FOLLOWINGS_HINT = None  # (tuple(pool), {key: set(uids)}) set per scan run to avoid duplicate fetches


def _set_pool_followings_hint(pool, account_followings):
    global _POOL_FOLLOWINGS_HINT
    _POOL_FOLLOWINGS_HINT = (tuple(pool), {k: set(v) for k, v in (account_followings or {}).items()})


def _presence_rank(p):
    """Rank how informative a presence dict is: has server gameId > has place/universe > InGame > Online/Offline."""
    if not p:
        return (-1, 0)
    r = 1 if p.get("userPresenceType") == 2 else 0
    if p.get("placeId") or p.get("rootPlaceId") or p.get("universeId"):
        r += 2
    if p.get("gameId"):
        r += 4
    return (r, p.get("userPresenceType") or 0)


class _PresencePoolAccount:
    """Session wrapper for a pool cookie during scan presence sweeps."""

    def __init__(self, key: str, cookie: str):
        self.key = key
        self.csrf = None
        self.blocked_until = 0.0
        self.dead = False
        self.fails = 0
        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": "https://www.roblox.com/",
            "Origin": "https://www.roblox.com",
        })
        self.sess.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")

    def query(self, ids: List[int]):
        for _ in range(2):
            hdr = {"x-csrf-token": self.csrf} if self.csrf else {}
            try:
                r = self.sess.post(PRESENCE_API, json={"userIds": ids}, headers=hdr, timeout=15)
            except requests.RequestException as e:
                self.fails += 1
                print(f"Presence error ({self.key}): {type(e).__name__}", file=sys.stderr)
                return None
            if r.status_code == 403 and r.headers.get("x-csrf-token"):
                self.csrf = r.headers["x-csrf-token"]
                continue
            if r.status_code == 200:
                self.fails = 0
                return r.json().get("userPresences", [])
            if r.status_code == 429:
                try:
                    wait = float(r.headers.get("Retry-After") or r.headers.get("retry-after") or 6.0)
                except ValueError:
                    wait = 6.0
                self.blocked_until = time.time() + wait
                return None
            self.fails += 1
            if r.status_code == 401 or self.fails >= 4:
                self.dead = True
            return None
        return None


def fetch_presences(user_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    """Fetch best-available presence per user across all configured pool cookies."""
    if not user_ids:
        return {}
    pool = _get_all_cookies()
    if not hasattr(fetch_presences, "_logged"):
        _log_cookie_status()
        fetch_presences._logged = True

    # Multi-cookie pool path: round-robin initial sweep + targeted follower & hidden-InGame sweep across all accounts.
    if len(pool) >= 2:
        accounts = [_PresencePoolAccount(k, ck) for k, ck in pool]
        results: Dict[int, Dict[str, Any]] = {}
        answered_by: Dict[int, set] = {}

        def _merge_batch(acct_key: str, pres_list: List[Dict[str, Any]]):
            for p in pres_list:
                uid = p.get("userId")
                if uid is not None:
                    answered_by.setdefault(uid, set()).add(acct_key)
                    if _presence_rank(p) > _presence_rank(results.get(uid)):
                        results[uid] = p

        batches = [user_ids[i:i + PRESENCE_BATCH_SIZE] for i in range(0, len(user_ids), PRESENCE_BATCH_SIZE)]
        for idx, batch in enumerate(batches):
            delivered = False
            for retry_round in range(2):
                for off in range(len(accounts)):
                    acct = accounts[(idx + off) % len(accounts)]
                    if acct.dead or time.time() < acct.blocked_until:
                        continue
                    res = acct.query(batch)
                    if res is not None:
                        _merge_batch(acct.key, res)
                        delivered = True
                        time.sleep(0.35)
                        break
                if delivered:
                    break
                live_accts = [a for a in accounts if not a.dead]
                if not live_accts:
                    break
                soonest = min(a.blocked_until for a in live_accts)
                wait_s = max(0.2, min(soonest - time.time() + 0.2, 8.0))
                if retry_round == 0:
                    time.sleep(wait_s)
            if not delivered:
                # Fallback unauthenticated batch so a transient full-pool block never drops presence completely
                try:
                    ru = requests.post(PRESENCE_API, json={"userIds": batch}, timeout=15, headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})
                    if ru.status_code == 200:
                        _merge_batch("unauth", ru.json().get("userPresences", []))
                except Exception:
                    pass

        # Resolve per-account followings map (reusing pre-fetched hint when called from tracker_scanner/main)
        acct_follow_map: Dict[str, set] = {}
        if _POOL_FOLLOWINGS_HINT and _POOL_FOLLOWINGS_HINT[0] == tuple(pool):
            acct_follow_map = _POOL_FOLLOWINGS_HINT[1]
        else:
            for k, ck in pool:
                hdr = {"Cookie": f".ROBLOSECURITY={ck}"}
                mid = _get_my_id(hdr)
                if mid:
                    acct_follow_map[k] = _get_followings(mid, hdr)

        # Pass 2: query each account for its own followed users (catches both Appearing-Offline AND hidden-game
        # followers) plus any user still InGame with hidden placeId across remaining pool accounts.
        uid_set = set(user_ids)
        for acct in accounts:
            if acct.dead:
                continue
            my_follows = acct_follow_map.get(acct.key, set()) & uid_set
            todo = []
            for uid in user_ids:
                cur = results.get(uid)
                if cur and cur.get("gameId"):
                    continue
                if acct.key in answered_by.get(uid, ()):
                    continue
                is_hidden_ingame = bool(cur and cur.get("userPresenceType") == 2 and not (cur.get("placeId") or cur.get("rootPlaceId") or cur.get("universeId")))
                if uid in my_follows or is_hidden_ingame:
                    todo.append(uid)
            for i in range(0, len(todo), PRESENCE_BATCH_SIZE):
                chunk = todo[i:i + PRESENCE_BATCH_SIZE]
                for attempt in range(2):
                    if acct.dead:
                        break
                    wait_left = acct.blocked_until - time.time()
                    if wait_left > 0:
                        time.sleep(min(wait_left + 0.2, 8.0))
                    res = acct.query(chunk)
                    if res is not None:
                        _merge_batch(acct.key, res)
                        time.sleep(0.35)
                        break

        return results

    # Single-cookie or unauthenticated path
    headers = _cookie_headers()
    csrf_token = None
    sess_fp = None
    if "Cookie" in headers and headers.get("Cookie"):
        try:
            ck2 = headers.get("Cookie", "")
            sess_fp = requests.Session()
            sess_fp.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Referer": "https://www.roblox.com/", "Origin": "https://www.roblox.com"})
            r_csrf = sess_fp.post("https://auth.roblox.com/v2/logout", timeout=10, headers={"Cookie": ck2})
            csrf_token = r_csrf.headers.get("x-csrf-token")
            if not csrf_token:
                sess_fp = _seeded_session(ck2)
                csrf_token = _get_csrf_from_session(sess_fp)
            if csrf_token:
                headers["x-csrf-token"] = csrf_token
                headers["Referer"] = "https://www.roblox.com/"
                headers["Origin"] = "https://www.roblox.com"
        except Exception as e:
            print(f"presence CSRF fetch failed {e}", file=sys.stderr)
    results = {}
    for i in range(0, len(user_ids), PRESENCE_BATCH_SIZE):
        batch = user_ids[i:i + PRESENCE_BATCH_SIZE]
        for attempt in range(1, 4):
            try:
                if sess_fp and "x-csrf-token" in headers:
                    r = sess_fp.post(PRESENCE_API, json={"userIds": batch}, timeout=15, headers=headers)
                else:
                    r = requests.post(PRESENCE_API, json={"userIds": batch}, timeout=15, headers=headers)
                if r.status_code == 403 and r.headers.get("x-csrf-token") and "Cookie" in headers:
                    headers["x-csrf-token"] = r.headers["x-csrf-token"]
                    continue
                if r.status_code == 403 and "Cookie" in headers:
                    print("Presence 403 with cookie — retrying unauthenticated (cookie may be invalid/expired)", file=sys.stderr)
                    headers_no_cookie = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
                    r = requests.post(PRESENCE_API, json={"userIds": batch}, timeout=15, headers=headers_no_cookie)
                if r.status_code == 429 and attempt < 3:
                    try:
                        wait = float(r.headers.get("Retry-After") or 5.0)
                    except ValueError:
                        wait = 5.0
                    time.sleep(min(wait, 15.0) + 0.5)
                    continue
                if r.status_code == 200:
                    for p in r.json().get("userPresences", []):
                        results[p["userId"]] = p
                else:
                    print(f"Presence batch failed {r.status_code}: {r.text[:200]}", file=sys.stderr)
                break
            except Exception as e:
                print(f"Presence error {e}", file=sys.stderr)
                if attempt < 3:
                    time.sleep(1 + attempt)
        time.sleep(0.4)
    return results

def main():
    ap=argparse.ArgumentParser(description="Developer IDs scanner")
    ap.add_argument("--user-ids-file", type=str, default="config/developer_ids.txt", help="Path to IDs file (one per line)")
    ap.add_argument("--user-ids", type=str, default="", help="Comma-separated IDs (overrides file)")
    ap.add_argument("--place-id", type=int, default=DEFAULT_PLACE_ID)
    ap.add_argument("--universe-id", type=int, default=None)
    ap.add_argument("--json", type=str, default="developer_scan_results.json")
    ap.add_argument("--csv", type=str, default="")
    ap.add_argument("--shard", type=int, default=None)
    ap.add_argument("--shards", type=int, default=None)
    ap.add_argument("--verbose", action="store_true")
    args=ap.parse_args()

    # Load IDs
    if args.user_ids:
        ids=[int(x.strip()) for x in args.user_ids.replace(","," ").split() if x.strip().isdigit()]
    else:
        ids=load_user_ids(args.user_ids_file)

    print(f"Loaded {len(ids)} developer IDs from {args.user_ids_file if not args.user_ids else 'CLI'}")

    # Shard
    is_sharded=args.shard is not None and args.shards is not None
    if is_sharded:
        total=len(ids)
        chunk=(total+args.shards-1)//args.shards
        start=args.shard*chunk
        end=min(start+chunk, total)
        ids_slice=ids[start:end]
        print(f"Shard {args.shard}/{args.shards}: handling IDs {start}-{end-1} ({len(ids_slice)}/{total})")
        ids_for_presence=ids_slice
        # For meta, keep full list size for offline counts? Use slice
    else:
        ids_for_presence=ids

    universe=args.universe_id or resolve_universe_id(args.place_id) or DEFAULT_UNIVERSE_ID
    game_name=resolve_game_name(universe) if universe else None
    if game_name:
        print(f'Target game: "{game_name}" Universe {universe}')

    # Fetch user infos for display names (for those in slice) — batched POST fixes 429 flood (0.05s GET -> 28/35 fails)
    infos = fetch_user_infos(ids_for_presence)
    members=[]
    for uid in ids_for_presence:
        info=infos.get(uid) or fetch_user_info(uid)
        info.update({"role": "Developer", "rank": 255, "roleId": 0})
        members.append(info)

    # Fetch followings across the pool FIRST so fetch_presences can query each account's followed users directly
    headers_for_follow=_cookie_headers()
    pool=_get_all_cookies()
    followings=set()
    account_followings={}
    my_ids=[]
    for key, ck in pool:
        hdr={"Cookie": f".ROBLOSECURITY={ck}"}
        mid=_get_my_id(hdr)
        if mid:
            my_ids.append(mid)
            f=_get_followings(mid, hdr)
            print(f"pool {key} -> my_id {mid} following {len(f)}", file=sys.stderr)
            account_followings[key]=f
            followings.update(f)
    # fallback single if pool empty
    if not pool:
        my_id=_get_my_id(headers_for_follow)
        if my_id:
            my_ids.append(my_id)
            followings=_get_followings(my_id, headers_for_follow)
    if my_ids:
        print(f"Pool {len(pool)} accounts my_ids {my_ids} total unique followings {len(followings)} for isFollowing", file=sys.stderr)
        my_id=my_ids[0]  # keep for per-user fallback
    else:
        my_id=None
        print("Pool no my_id - unauthenticated", file=sys.stderr)

    _set_pool_followings_hint(pool, account_followings)
    try:
        presences=fetch_presences(ids_for_presence)
    finally:
        _set_pool_followings_hint([], {})
    print(f"Got {len(presences)}/{len(ids_for_presence)} presence responses")

    # Categorize same as group scanner
    target=[]
    other=[]
    offline=[]
    for m in members:
        uid=m["userId"]
        pres=presences.get(uid)
        if not pres:
            offline.append({**m, "presence": None})
            continue
        ptype=pres.get("userPresenceType",0)
        place_id=pres.get("placeId")
        root_place=pres.get("rootPlaceId")
        univ=pres.get("universeId")
        last_loc=pres.get("lastLocation","")
        is_in_game=(ptype==2)
        is_target=False
        if is_in_game:
            if universe and univ==universe:
                is_target=True
            elif place_id==args.place_id or root_place==args.place_id:
                is_target=True
        # isFollowing flag for discord_updater to know if Hunt was revealed via Follow
        is_following = uid in followings if followings else False
        # Fallback per-user check if bulk followings empty (e.g., 9002 auth) — check single followings page for this uid
        if not is_following and my_id and len(followings)==0 and ptype==2 and place_id is None:
            try:
                import requests as _req
                ck2=headers_for_follow.get("Cookie","")
                h2={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)","Referer":"https://www.roblox.com/","Accept":"application/json","Cookie": ck2}
                sess2=_seeded_session(ck2) if ck2 else _req.Session()
                r=sess2.get(f"https://friends.roblox.com/v1/users/{my_id}/followings?limit=100&sortOrder=Asc", timeout=10, headers=h2)
                if r.status_code==200 and any(e.get("id")==uid for e in r.json().get("data",[])):
                    is_following=True
                    print(f"per-user isFollowing true for {uid}", file=sys.stderr)
            except: pass
        # Profile scraping for ANY InGame hidden (isFollowing true/false) — website profile shows Hunt when Following, even when presence API hides (e.g., 91512961)
        # We check profile for Hunt for all InGame hidden to avoid relying on broken isFollowing bulk fetch (9002). If profile shows Hunt, treat as Hunt.
        if ptype==2 and place_id is None and root_place is None and univ is None:
            if _profile_shows_hunt(uid, headers_for_follow, target_place_id=args.place_id, target_universe_id=universe):
                print(f"profile scrape: {uid} shows target game via profile", file=sys.stderr)
                place_id=args.place_id
                root_place=args.place_id
                univ=universe or DEFAULT_UNIVERSE_ID
                last_loc=game_name or DEFAULT_GAME_NAME
                is_target=True
                is_following=True
        enriched={**m, "presenceType": ptype, "presenceTypeName": {0:"Offline",1:"Online",2:"InGame",3:"InStudio"}.get(ptype,str(ptype)), "lastLocation": last_loc, "placeId": place_id, "rootPlaceId": root_place, "universeId": univ, "gameId": pres.get("gameId"), "lastOnline": pres.get("lastOnline"), "isFollowing": is_following, "presence_raw": pres}
        if is_target:
            target.append(enriched)
        elif is_in_game:
            other.append(enriched)
        else:
            offline.append(enriched)

    target.sort(key=lambda x: (x["username"] or "").lower())
    other.sort(key=lambda x: ((x["lastLocation"] or ""), (x["username"] or "").lower()))

    total_members=len(ids_for_presence)
    print(f"Target Hunt: {len(target)} | Other: {len(other)} | Offline: {len(offline)} | Total slice {total_members}")

    meta={
        "groupId": "developer_list",
        "groupUrl": "",
        "targetPlaceId": args.place_id,
        "targetUniverseId": universe,
        "targetGameName": game_name,
        "targetGameUrl": f"https://www.roblox.com/games/{args.place_id}/",
        "total_members": total_members,
        "full_group_size": len(ids),
        "elapsed_seconds": 0,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "settings": {"shard": args.shard, "shards": args.shards, "source": args.user_ids_file}
    }
    out={
        "meta": meta,
        "summary": {"total_members": total_members, "target_game_count": len(target), "other_game_count": len(other), "offline_count": len(offline)},
        "target_game_players": target,
        "other_game_players": other,
        "offline_or_website": offline
    }
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"JSON exported to {args.json}")
    if args.csv:
        import csv
        fieldnames=["priority","username","displayName","userId","role","rank","presenceType","presenceTypeName","lastLocation","placeId","rootPlaceId","universeId","profileUrl"]
        rows=[]
        for u in target:
            rows.append({"priority":"TARGET_GAME","username":u["username"],"displayName":u["displayName"],"userId":u["userId"],"role":u["role"],"rank":u["rank"],"presenceType":u["presenceType"],"presenceTypeName":u["presenceTypeName"],"lastLocation":u["lastLocation"],"placeId":u["placeId"],"rootPlaceId":u["rootPlaceId"],"universeId":u["universeId"],"profileUrl":f"https://www.roblox.com/users/{u['userId']}/profile"})
        for u in other:
            rows.append({"priority":"OTHER_GAME","username":u["username"],"displayName":u["displayName"],"userId":u["userId"],"role":u["role"],"rank":u["rank"],"presenceType":u["presenceType"],"presenceTypeName":u["presenceTypeName"],"lastLocation":u["lastLocation"],"placeId":u["placeId"],"rootPlaceId":u["rootPlaceId"],"universeId":u["universeId"],"profileUrl":f"https://www.roblox.com/users/{u['userId']}/profile"})
        with open(args.csv,"w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        print(f"CSV to {args.csv}")

if __name__=="__main__":
    main()
