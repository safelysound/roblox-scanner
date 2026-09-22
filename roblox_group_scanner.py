#!/usr/bin/env python3
"""
ROBLOX Group Presence Scanner
==============================
Scans all members of a ROBLOX group, checks who is currently in-game,
and prioritizes those playing a specific target game.

Group: https://www.roblox.com/communities/1200769/Official-Group-of-Roblox
Target Game: https://www.roblox.com/games/74205509034203/The-Hunt-Roblox-20
  -> PlaceId: 74205509034203  -> UniverseId: 10766456501 (auto-resolved)

Efficient + Rate-Limit Safe Design:
-----------------------------------
1. Groups API is paginated (100 users/page) and cursor-sequential - can't be parallelized.
   -> Fetched sequentially with adaptive throttling + exponential backoff on 429.
   
2. Presence API supports BATCHING (up to 50 userIds per POST). This is the key efficiency:
   -> Instead of 2771 individual requests, we make ~56 batched requests (50 ids each).
   -> ~50x reduction in calls.

3. Presence batches are then executed CONCURRENTLY with a controlled ThreadPool
   (default 4 workers) + inter-request delay + token-bucket style throttling.
   -> 4-5x speedup vs sequential while staying under Roblox's undocumented limits.

4. Rate Limit Avoidance:
   - Single reused Session with realistic User-Agent + Accept headers
   - Default delays: 350ms between group pages, 250ms between presence batches
   - Automatic 429 handling: respects Retry-After header, exponential backoff (1s, 2s, 4s...) + jitter
   - Max retries 5 per request
   - Optional --throttle flag to go slower for huge groups

Output:
-------
- Prioritized terminal output: [1] People playing TARGET game, [2] People playing ANY other game
- JSON export (full details)
- CSV export (optional)

Usage:
------
# Basic (no deps beyond requests) — unauthenticated, ~80-90s for 2771 members:
pip install requests
python roblox_group_scanner.py

# With nice progress bars:
pip install requests tqdm
python roblox_group_scanner.py --progress

# Custom group/game:
python roblox_group_scanner.py --group-id 1200769 --place-id 74205509034203

# Throttle more for huge groups or if you hit 429:
python roblox_group_scanner.py --throttle 0.6 --workers 2

# Only scan first 200 members for testing:
python roblox_group_scanner.py --max-members 200

# Export:
python roblox_group_scanner.py --json output.json --csv output.csv

# 6x SPEED BOOST — add up to 6 ROBLOSECURITY cookies (each has own 60 req/min):
# Option 1: Edit ROBLOSECURITY_COOKIES list at top of this file
# Option 2: Env vars: export ROBLOSECURITY_1="..." ROBLOSECURITY_2="..." (up to 6)
# Option 3: File: python roblox_group_scanner.py --cookie-file cookies.txt  (one per line)
# Option 4: CLI: python roblox_group_scanner.py --cookies "cookie1,cookie2"
# With 6 cookies: ~15-20s for 2771 members (vs 80-90s single) — auto-tuned workers/delays
# Example: python roblox_group_scanner.py --cookie-file cookies.txt --max-members 500

Requires: Python 3.8+, requests (tqdm optional)
"""

import argparse
import json
import csv
import time
import random
import sys
import os
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging

try:
    import requests
except ImportError:
    print("Missing 'requests'. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    from tqdm import tqdm  # type: ignore
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# ================= CONFIG (edit these or use CLI flags) =================
DEFAULT_GROUP_ID = 1200769  # Official Group of Roblox
DEFAULT_PLACE_ID = 74205509034203  # The Hunt Roblox 20
DEFAULT_UNIVERSE_ID = 10766456501  # Auto-resolved from PlaceId; will be re-resolved live if None

# Efficiency / Rate Limit Tuning — tuned after live testing to avoid 429
# Roblox headers show: x-ratelimit-limit: 60, 60;w=60  -> 60 requests per 60s sliding window
# For 2771 members: 28 group pages + 56 presence batches = 84 requests total
# At 0.4s delay -> 56 presence in 22s = 152 req/min > 60 -> triggers 429 storm
# Safe delay: 60/56 ≈ 1.07s minimum for presence to stay under 60/min
# We use 1.1s global throttle + header-aware backoff for bulletproof safety.
GROUP_PAGE_LIMIT = 100  # Max allowed by Roblox is 100
PRESENCE_BATCH_SIZE = 50  # Max allowed is 50 (100+ gives 400 error)
DEFAULT_GROUP_DELAY = 0.6  # seconds between group page fetches (conservative, avoids groups 429)
DEFAULT_PRESENCE_DELAY = 1.1  # seconds MINIMUM between *any* two presence batches (global throttle, safe for 2771)
DEFAULT_WORKERS = 2  # Concurrent presence workers (2 with global throttle; 1 is safest sequential)

# ================= ROBLOSECURITY COOKIE POOL — 6x SPEED BOOST =================
# Fill up to 6 cookies to make scanning 3-6x faster (each cookie has its own rate limit bucket).
# When cookies are provided:
#   - Group + Presence requests are round-robin distributed across cookies
#   - Effective limit becomes 60 * N requests per 60s (e.g., 6 cookies = 360 req/min)
#   - Presence delay auto-drops to 0.35s and workers auto-scale to N (unless you override)
#
# SECURITY WARNING: .ROBLOSECURITY grants FULL account access. NEVER commit, share, or paste it!
#   - Prefer env vars or --cookie-file over hardcoding below.
#   - If you must hardcode, keep this file private and git-ignored.
#
# How to get a cookie:
#   1. Log into Roblox in browser → F12 → Application → Cookies → https://www.roblox.com → .ROBLOSECURITY
#   2. Copy the value (starts with _|WARNING:...)
#   3. Paste below OR set env var ROBLOSECURITY_1 .. ROBLOSECURITY_6 OR use --cookie-file
#
# Leave empty to run UNAUTHENTICATED (works for public groups, slower: 1 req/s).
ROBLOSECURITY_COOKIES: List[str] = [
    "",  # Cookie 1 — e.g., "_|WARNING:-DO-NOT-SHARE-THIS...|ABC123"
    "",  # Cookie 2
    "",  # Cookie 3
    "",  # Cookie 4
    "",  # Cookie 5
    "",  # Cookie 6
]
# You can also set via environment: ROBLOSECURITY, ROBLOSECURITY_1..6
# Or via file: one cookie per line → python roblox_group_scanner.py --cookie-file cookies.txt
MAX_RETRIES = 5
BACKOFF_BASE = 1.0  # seconds
TIMEOUT = 15

# API Endpoints
GROUPS_API = "https://groups.roblox.com/v1/groups/{group_id}/users"
UNIVERSE_API = "https://apis.roblox.com/universes/v1/places/{place_id}/universe"
PRESENCE_API = "https://presence.roblox.com/v1/presence/users"
GAMES_API = "https://games.roblox.com/v1/games?universeIds={universe_id}"
DISCORD_WEBHOOK_ENV = "DISCORD_WEBHOOK"

# Presence Types: 0=Offline, 1=Online (website), 2=InGame, 3=InStudio
PRESENCE_TYPE_MAP = {0: "Offline", 1: "Online", 2: "InGame", 3: "InStudio"}

# ========================================================================

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
)
log = logging.getLogger("roblox_scanner")


def _normalize_cookie(raw: str) -> str:
    """Strip whitespace, handle both 'value' and '.ROBLOSECURITY=value' forms."""
    raw = raw.strip().strip('"').strip("'")
    if not raw:
        return ""
    if raw.startswith(".ROBLOSECURITY="):
        raw = raw.split("=", 1)[1]
    if raw.startswith("_|WARNING"):
        return raw
    # If someone pasted full cookie string with other cookies, extract token
    if "_|WARNING" in raw:
        # try to find the token inside
        import re
        m = re.search(r"_\|WARNING[^;]*", raw)
        if m:
            return m.group(0).strip()
    return raw

def make_session(cookie: Optional[str] = None) -> requests.Session:
    s = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 RobloxGroupScanner/1.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.roblox.com/",
        "Origin": "https://www.roblox.com",
    }
    if cookie:
        # Roblox expects Cookie header, not Authorization
        s.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")
        # Add cookie to headers as fallback for some domains
        headers["Cookie"] = f".ROBLOSECURITY={cookie}"
    s.headers.update(headers)
    return s

def get_csrf_token(session: requests.Session) -> Optional[str]:
    """Try to fetch X-CSRF-TOKEN for authenticated POSTs (needed when using cookie)."""
    try:
        # This endpoint always returns a token in header even on 403
        r = session.post("https://auth.roblox.com/v2/logout", timeout=5)
        token = r.headers.get("x-csrf-token")
        if token:
            return token
        # Alternative: try users auth
        r = session.get("https://www.roblox.com/home", timeout=5)
        # Look for token in response headers/meta
        token = r.headers.get("x-csrf-token")
        return token
    except:
        return None


# ================= Cookie Pool Manager =================
_cookie_pool: List[requests.Session] = []
_cookie_index = 0
_cookie_lock = threading.Lock()
_cookie_csrf: Dict[int, str] = {}  # session id -> csrf token

def load_cookies(args) -> List[str]:
    """Load up to 6 cookies from: hardcoded list + env vars + CLI file/args. Deduplicated."""
    cookies: List[str] = []

    # 1. Hardcoded list
    for c in ROBLOSECURITY_COOKIES:
        n = _normalize_cookie(c)
        if n:
            cookies.append(n)

    # 2. Env vars: ROBLOSECURITY, ROBLOX_COOKIE, ROBLOSECURITY_1..6
    for env_key in ["ROBLOSECURITY", "ROBLOX_COOKIE", "ROBLOSECURITY_1", "ROBLOSECURITY_2", "ROBLOSECURITY_3", "ROBLOSECURITY_4", "ROBLOSECURITY_5", "ROBLOSECURITY_6", "ROBLOSECURITY1", "ROBLOSECURITY2"]:
        val = os.environ.get(env_key)
        if val:
            n = _normalize_cookie(val)
            if n and n not in cookies:
                cookies.append(n)

    # 3. CLI --cookies (comma-separated)
    if getattr(args, "cookies", None):
        for part in args.cookies.split(","):
            n = _normalize_cookie(part)
            if n and n not in cookies:
                cookies.append(n)

    # 4. CLI --cookie-file (one per line)
    if getattr(args, "cookie_file", None):
        try:
            p = Path(args.cookie_file)
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    n = _normalize_cookie(line)
                    if n and n not in cookies:
                        cookies.append(n)
            else:
                log.warning(f"Cookie file not found: {args.cookie_file}")
        except Exception as e:
            log.warning(f"Failed to read cookie file: {e}")

    # Deduplicate and cap at 6
    seen = set()
    uniq: List[str] = []
    for c in cookies:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    if len(uniq) > 6:
        log.warning(f"More than 6 cookies provided ({len(uniq)}), using first 6.")
        uniq = uniq[:6]
    return uniq

def init_cookie_pool(cookies: List[str]) -> List[requests.Session]:
    """Create a Session per cookie, pre-fetch CSRF tokens."""
    global _cookie_pool, _cookie_index
    pool: List[requests.Session] = []
    if not cookies:
        # Single unauthenticated session
        pool.append(make_session(None))
        log.info("No ROBLOSECURITY provided — running unauthenticated (slower, 60 req/min).")
        _cookie_pool = pool
        return pool

    log.info(f"Initializing cookie pool with {len(cookies)} account(s) — parallelizing for ~{60*len(cookies)} req/min")
    for i, ck in enumerate(cookies, 1):
        s = make_session(ck)
        # Try to get CSRF token so POSTs don't 403
        token = get_csrf_token(s)
        if token:
            s.headers.update({"x-csrf-token": token})
            _cookie_csrf[id(s)] = token
            log.info(f"  Cookie {i}: CSRF token acquired ({token[:8]}...)")
        else:
            log.warning(f"  Cookie {i}: no CSRF token (POSTs may still work for presence/groups GET)")
        # Validate cookie quickly with a lightweight auth check
        try:
            r = s.get("https://users.roblox.com/v1/users/authenticated", timeout=8)
            if r.status_code == 200 and r.json().get("id"):
                log.info(f"  Cookie {i}: authenticated as user {r.json().get('id')} ({r.json().get('name')})")
            else:
                log.warning(f"  Cookie {i}: seems invalid/expired (authenticated check: {r.status_code})")
        except Exception as e:
            log.warning(f"  Cookie {i}: validation failed: {e}")
        pool.append(s)
    
    _cookie_pool = pool
    _cookie_index = 0
    return pool

def get_next_session() -> requests.Session:
    """Round-robin pick next session from pool (thread-safe)."""
    global _cookie_index
    if not _cookie_pool:
        return make_session(None)
    if len(_cookie_pool) == 1:
        return _cookie_pool[0]
    with _cookie_lock:
        s = _cookie_pool[_cookie_index % len(_cookie_pool)]
        _cookie_index += 1
        return s

def get_pool_size() -> int:
    return len(_cookie_pool) if _cookie_pool else 1


def sleep_with_jitter(base: float):
    """Sleep base +/- 25% jitter to avoid thundering herd."""
    jitter = base * 0.25 * (random.random() * 2 - 1)
    time.sleep(max(0, base + jitter))


def request_with_backoff(session: requests.Session, method: str, url: str, **kwargs) -> Optional[requests.Response]:
    """GET/POST with 429-aware exponential backoff + Retry-After + Roblox x-ratelimit header support."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.request(method, url, timeout=TIMEOUT, **kwargs)

            # Log Roblox rate limit headers if present (for debugging / adaptive throttling)
            remaining = resp.headers.get("x-ratelimit-remaining")
            reset = resp.headers.get("x-ratelimit-reset")
            if remaining is not None:
                try:
                    rem = int(remaining)
                    if rem < 10:
                        log.debug(f"Rate limit remaining low: {rem}, reset in {reset}s")
                        # If very low, proactively pause before next request will be throttled
                        if rem < 5 and reset and reset.isdigit():
                            wait_extra = min(10, int(reset) + 1)
                            log.warning(f"Low rate limit remaining ({rem}) -> precautionary sleep {wait_extra}s")
                            time.sleep(wait_extra)
                except:
                    pass

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                # Roblox often uses x-ratelimit-reset instead of Retry-After
                if not retry_after or not retry_after.isdigit():
                    retry_after = resp.headers.get("x-ratelimit-reset")
                if retry_after and retry_after.isdigit():
                    wait = float(retry_after) + 1.0  # +1 buffer
                else:
                    wait = BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                # Cap wait to avoid huge sleeps but ensure we back off
                wait = min(wait, 60)
                log.warning(f"429 Rate limited on {url} (attempt {attempt}/{MAX_RETRIES}) -> waiting {wait:.1f}s (remaining={remaining}, reset={reset})")
                time.sleep(wait)
                continue

            if resp.status_code >= 500:
                wait = BACKOFF_BASE * (2 ** (attempt - 1))
                log.warning(f"{resp.status_code} Server error on {url} -> retry {attempt}/{MAX_RETRIES} in {wait:.1f}s")
                time.sleep(wait)
                continue

            # 400 etc is not retryable (bad request)
            if resp.status_code >= 400:
                log.error(f"HTTP {resp.status_code} for {url}: {resp.text[:500]}")
                if resp.status_code == 400:
                    return None  # don't retry bad request
                # else still return for caller to handle
                return resp

            return resp

        except requests.exceptions.RequestException as e:
            wait = BACKOFF_BASE * (2 ** (attempt - 1))
            log.warning(f"Request exception {e} -> retry {attempt}/{MAX_RETRIES} in {wait:.1f}s")
            time.sleep(wait)

    log.error(f"Failed after {MAX_RETRIES} retries: {url}")
    return None


def resolve_universe_id(session: requests.Session, place_id: int) -> Optional[int]:
    """Convert PlaceId -> UniverseId via apis.roblox.com"""
    url = UNIVERSE_API.format(place_id=place_id)
    log.info(f"Resolving UniverseId for PlaceId {place_id}...")
    resp = request_with_backoff(session, "GET", url)
    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            uid = data.get("universeId")
            if uid:
                log.info(f"  -> UniverseId = {uid}")
                return int(uid)
        except Exception as e:
            log.warning(f"Failed to parse universe response: {e}")
    log.warning(f"Could not resolve UniverseId, will match on PlaceId only.")
    return None


def resolve_game_name(session: requests.Session, universe_id: int) -> Optional[str]:
    url = GAMES_API.format(universe_id=universe_id)
    resp = request_with_backoff(session, "GET", url)
    if resp and resp.status_code == 200:
        try:
            j = resp.json()
            if j.get("data") and len(j["data"]) > 0:
                return j["data"][0].get("name")
        except:
            pass
    return None


def fetch_all_group_members(
    session: requests.Session,
    group_id: int,
    delay: float = DEFAULT_GROUP_DELAY,
    max_members: Optional[int] = None,
    show_progress: bool = False,
) -> List[Dict[str, Any]]:
    """
    Paginate through /v1/groups/{groupId}/users (limit 100, cursor sequential).
    This is the bottleneck that MUST be sequential due to cursor dependency.
    Efficient because we fetch max 100 per page (fewest requests).
    When cookie pool is active, requests are round-robin across cookies for speed.
    """
    # Use cookie pool if available for this phase
    use_pool = get_pool_size() > 1
    if use_pool:
        log.info(f"Fetching members for group {group_id} (limit={GROUP_PAGE_LIMIT}/page, delay={delay}s) using {get_pool_size()} cookies round-robin...")
    else:
        log.info(f"Fetching members for group {group_id} (limit={GROUP_PAGE_LIMIT}/page, delay={delay}s) ...")
    members: List[Dict[str, Any]] = []
    cursor: Optional[str] = ""
    page = 0
    total_reported = None

    # Try to get memberCount first for progress estimate
    try:
        sess0 = get_next_session() if use_pool else session
        r = request_with_backoff(sess0, "GET", f"https://groups.roblox.com/v1/groups/{group_id}")
        if r and r.status_code == 200:
            total_reported = r.json().get("memberCount")
            if total_reported:
                log.info(f"Group reports {total_reported} members.")
    except:
        pass

    pbar = None
    if show_progress and HAS_TQDM and total_reported:
        pbar = tqdm(total=total_reported, desc="Fetching group members", unit=" user")

    while True:
        page += 1
        params = {"limit": GROUP_PAGE_LIMIT, "sortOrder": "Asc"}
        if cursor:
            params["cursor"] = cursor

        url = GROUPS_API.format(group_id=group_id)
        sess = get_next_session() if use_pool else session
        resp = request_with_backoff(sess, "GET", url, params=params)

        if not resp or resp.status_code != 200:
            log.error(f"Failed to fetch page {page}, stopping.")
            break

        data = resp.json()
        batch = data.get("data", [])

        if not batch:
            log.info("No more data, done.")
            break

        for entry in batch:
            user = entry.get("user", {})
            role = entry.get("role", {})
            members.append({
                "userId": user.get("userId"),
                "username": user.get("username"),
                "displayName": user.get("displayName"),
                "hasVerifiedBadge": user.get("hasVerifiedBadge", False),
                "role": role.get("name"),
                "rank": role.get("rank"),
                "roleId": role.get("id"),
            })
            if pbar:
                pbar.update(1)

            if max_members and len(members) >= max_members:
                log.info(f"Reached max-members limit {max_members}, stopping pagination early.")
                if pbar:
                    pbar.close()
                return members[:max_members]

        log.info(f"  Page {page}: +{len(batch)} members (total {len(members)}) nextCursor={'yes' if data.get('nextPageCursor') else 'none'}")

        cursor = data.get("nextPageCursor")
        if not cursor:
            break

        sleep_with_jitter(delay)

    if pbar:
        pbar.close()
    log.info(f"Finished fetching group: {len(members)} members collected.")
    return members


_presence_lock = threading.Lock()
_last_presence_time: Dict[int, float] = {}  # per-session throttle (key = id(session))
_global_last_time = 0.0  # fallback global when no pool

def _throttle_before_request(session: requests.Session, delay: float):
    """Throttling that respects per-cookie limits when pool is active."""
    global _global_last_time
    # When cookie pool is active, throttle per-cookie, not globally (allows N× throughput)
    # Otherwise throttle globally
    use_pool = get_pool_size() > 1
    if delay <= 0:
        return
    with _presence_lock:
        now = time.time()
        if use_pool:
            sid = id(session)
            last = _last_presence_time.get(sid, 0.0)
            # Effective delay per-cookie; global gap is smaller but still avoid instant burst
            wait = (last + delay) - now
            # Also ensure tiny global gap to avoid thundering herd across cookies
            global_wait = (_global_last_time + 0.15) - now  # 150ms min between any two requests
            wait = max(wait, global_wait)
            if wait > 0:
                jitter = random.uniform(-0.1, 0.1) * delay
                time.sleep(max(0, wait + jitter))
            _last_presence_time[sid] = time.time()
            _global_last_time = _last_presence_time[sid]
        else:
            wait = (_global_last_time + delay) - now
            if wait > 0:
                jitter = random.uniform(-0.15, 0.15) * delay
                time.sleep(max(0, wait + jitter))
            _global_last_time = time.time()

def fetch_presence_batch(
    session: requests.Session,
    user_ids: List[int],
    delay: float = DEFAULT_PRESENCE_DELAY,
) -> List[Dict[str, Any]]:
    """Single batched POST to presence API (up to 50 ids). Throttling via per-cookie lock."""
    _throttle_before_request(session, delay)

    payload = {"userIds": user_ids}
    resp = request_with_backoff(session, "POST", PRESENCE_API, json=payload)
    if not resp or resp.status_code != 200:
        # return offline stubs on failure so caller can continue
        log.warning(f"Presence batch failed for {len(user_ids)} ids, marking as offline.")
        return []

    try:
        return resp.json().get("userPresences", [])
    except Exception as e:
        log.warning(f"Failed to parse presence JSON: {e}")
        return []


def fetch_all_presences(
    session: requests.Session,
    user_ids: List[int],
    batch_size: int = PRESENCE_BATCH_SIZE,
    workers: int = DEFAULT_WORKERS,
    delay: float = DEFAULT_PRESENCE_DELAY,
    show_progress: bool = False,
) -> Dict[int, Dict[str, Any]]:
    """
    Efficient batched presence scanning with concurrency.
    - Splits user_ids into chunks of 50 (max allowed)
    - Dispatches chunks concurrently via ThreadPoolExecutor
    - Each worker respects inter-request delay + 429 backoff
    """
    batches = [user_ids[i:i + batch_size] for i in range(0, len(user_ids), batch_size)]
    log.info(f"Checking presence for {len(user_ids)} users in {len(batches)} batches of {batch_size} (workers={workers}, delay={delay}s)...")

    results: Dict[int, Dict[str, Any]] = {}

    # Reuse same session? requests.Session is not fully thread-safe but okay with CPython GIL for simple use.
    # To be safe, we create a session per thread via thread-local, but simpler: share and rely on connection pooling.
    # We'll create per-worker sessions inside the executor.
    
    def worker(batch: List[int]) -> List[Dict[str, Any]]:
        # Use round-robin cookie session if pool exists, else fresh unauthenticated session
        s = get_next_session() if get_pool_size() > 1 else make_session()
        # If pool has single unauthenticated session, reuse it; else get_next already round-robins
        return fetch_presence_batch(s, batch, delay=delay)

    # Use ThreadPoolExecutor for concurrency
    # If workers == 1, just do sequential to avoid overhead
    if workers <= 1:
        iterator = batches
        if show_progress and HAS_TQDM:
            iterator = tqdm(batches, desc="Checking presence", unit=" batch")
        for batch in iterator:
            presences = worker(batch)
            for p in presences:
                results[p["userId"]] = p
            # need manual tqdm update if not using executor path
            if show_progress and not HAS_TQDM:
                log.info(f"  Processed batch {len(results)}/{len(user_ids)} users")
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            # submit all
            future_to_batch = {executor.submit(worker, b): b for b in batches}
            
            # Iterate as completed with progress
            if show_progress and HAS_TQDM:
                pbar = tqdm(total=len(batches), desc="Checking presence", unit=" batch")
            else:
                pbar = None
                completed = 0

            for future in as_completed(future_to_batch):
                batch = future_to_batch[future]
                try:
                    presences = future.result()
                    for p in presences:
                        results[p["userId"]] = p
                except Exception as e:
                    log.warning(f"Batch {batch[:3]}... failed: {e}")
                
                if pbar:
                    pbar.update(1)
                else:
                    completed += 1
                    if completed % 5 == 0 or completed == len(batches):
                        log.info(f"  Presence progress: {completed}/{len(batches)} batches ({len(results)}/{len(user_ids)} users)")
            if pbar:
                pbar.close()

    log.info(f"Presence check complete: got {len(results)}/{len(user_ids)} responses.")
    return results


def categorize_users(
    members: List[Dict[str, Any]],
    presences: Dict[int, Dict[str, Any]],
    target_place_id: int,
    target_universe_id: Optional[int],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Returns (target_game_players, other_game_players, offline/website)
    Prioritizes target game.
    """
    target_players = []
    other_players = []
    offline = []

    for m in members:
        uid = m["userId"]
        pres = presences.get(uid)
        if not pres:
            # No presence data = treat as offline/unknown
            offline.append({**m, "presence": None})
            continue

        ptype = pres.get("userPresenceType", 0)
        place_id = pres.get("placeId")
        root_place_id = pres.get("rootPlaceId")
        univ_id = pres.get("universeId")
        last_location = pres.get("lastLocation", "")
        game_id = pres.get("gameId")

        # Determine if in game at all
        is_in_game = (ptype == 2)

        # Determine if in target game
        # Match any of: universeId, placeId, rootPlaceId, gameId
        is_target = False
        if is_in_game:
            if target_universe_id and univ_id == target_universe_id:
                is_target = True
            elif place_id == target_place_id or root_place_id == target_place_id:
                is_target = True
            elif game_id and str(game_id) == str(target_place_id):
                is_target = True

        enriched = {
            **m,
            "presenceType": ptype,
            "presenceTypeName": PRESENCE_TYPE_MAP.get(ptype, str(ptype)),
            "lastLocation": last_location,
            "placeId": place_id,
            "rootPlaceId": root_place_id,
            "universeId": univ_id,
            "gameId": game_id,
            "lastOnline": pres.get("lastOnline"),
            "presence_raw": pres,
        }

        if is_target:
            target_players.append(enriched)
        elif is_in_game:
            other_players.append(enriched)
        else:
            offline.append(enriched)

    # Sort: target players by username, other players by lastLocation then username
    target_players.sort(key=lambda x: (x["username"] or "").lower())
    other_players.sort(key=lambda x: ((x["lastLocation"] or ""), (x["username"] or "").lower()))
    offline.sort(key=lambda x: (x["username"] or "").lower())

    return target_players, other_players, offline


def print_results(
    target_players: List[Dict],
    other_players: List[Dict],
    offline_count: int,
    total_members: int,
    target_place_id: int,
    target_universe_id: Optional[int],
    game_name: Optional[str],
):
    sep = "=" * 80
    print("\n" + sep)
    print(" ROBLOX GROUP PRESENCE SCAN — RESULTS (Prioritized)")
    print(sep)
    print(f"Group members scanned: {total_members}")
    print(f"Target game: PlaceId {target_place_id} | UniverseId {target_universe_id or 'unknown'}"
          + (f" | \"{game_name}\"" if game_name else ""))
    print(f"  -> https://www.roblox.com/games/{target_place_id}/")
    print(f"Playing TARGET game: {len(target_players)}")
    print(f"Playing OTHER games: {len(other_players)}")
    print(f"Not in-game (offline/website/studio): {offline_count}")
    print(sep)

    # Section 1: Target game (highest priority)
    print("\n🎯  [PRIORITY 1] PLAYING TARGET GAME")
    print("-" * 80)
    if not target_players:
        print("  (none found — no group members currently in this exact game)")
        print("  Tip: This is accurate per presence API, but members with privacy set to hide game")
        print("       may not be detectable. Try re-scanning in a few minutes.")
    else:
        print(f"  Found {len(target_players)} member(s) in target game:\n")
        for i, u in enumerate(target_players, 1):
            print(f"  {i:>3}. {u['username']} ({u['displayName']})  [UID {u['userId']}]"
                  + (f" ✓ Verified" if u['hasVerifiedBadge'] else ""))
            print(f"       Role: {u['role']} (Rank {u['rank']}) | Location: {u['lastLocation']}")
            print(f"       PlaceId: {u['placeId']} | UniverseId: {u['universeId']} | Profile: https://www.roblox.com/users/{u['userId']}/profile")
            print(f"       Join? https://www.roblox.com/games/{u['placeId'] or target_place_id}  (if joins allowed)")
            print()

    # Section 2: Other games
    print("\n🎮  [PRIORITY 2] PLAYING OTHER GAMES")
    print("-" * 80)
    if not other_players:
        print("  (none — no other members currently in any game)")
    else:
        print(f"  Found {len(other_players)} member(s) playing other games:\n")
        # Show first 50 in terminal to avoid flood, rest in JSON
        display_limit = 100
        for i, u in enumerate(other_players[:display_limit], 1):
            loc = u['lastLocation'] or f"Place {u['placeId'] or 'unknown'}"
            print(f"  {i:>3}. {u['username']} ({u['displayName']}) [UID {u['userId']}] — {loc}")
            print(f"       Type: {u['presenceTypeName']} | PlaceId: {u['placeId']} | UniverseId: {u['universeId']}")
            print(f"       Profile: https://www.roblox.com/users/{u['userId']}/profile")
        if len(other_players) > display_limit:
            print(f"\n  ... and {len(other_players) - display_limit} more (see JSON export for full list)")

    print("\n" + sep)
    print(" Tip: Re-run every 2-5 minutes to catch new players. Avoid <60s intervals to respect rate limits.")
    print(sep + "\n")


def export_json(path: str, target_players, other_players, offline, meta: dict):
    out = {
        "meta": meta,
        "summary": {
            "total_members": meta["total_members"],
            "target_game_count": len(target_players),
            "other_game_count": len(other_players),
            "offline_count": len(offline),
        },
        "target_game_players": target_players,
        "other_game_players": other_players,
        "offline_or_website": offline,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    log.info(f"JSON exported to {path}")


def send_discord_webhook(webhook_url: str, target_players, other_players, meta: dict):
    """Send prioritized results to Discord webhook (optional future)."""
    if not webhook_url:
        webhook_url = os.environ.get(DISCORD_WEBHOOK_ENV, "")
    if not webhook_url:
        return
    # Discord limits: 2000 chars, 10 embeds max
    try:
        # Build simple embed
        title = f"Roblox Hunt Scan — {meta['total_members']} members"
        desc = f"**Target:** [{meta['targetGameName'] or meta['targetPlaceId']}]({meta['targetGameUrl']})\n"
        desc += f"**Playing TARGET:** {len(target_players)} | **Other games:** {len(other_players)}\n"
        if target_players:
            desc += "\n**🎯 TARGET GAME:**\n"
            for u in target_players[:10]:
                desc += f"• [{u['username']}](https://www.roblox.com/users/{u['userId']}/profile) — {u['role']}\n"
            if len(target_players) > 10:
                desc += f"_+{len(target_players)-10} more_\n"
        if other_players:
            desc += f"\n**🎮 Other games ({len(other_players)}):**\n"
            for u in other_players[:10]:
                loc = u['lastLocation'] or f"Place {u['placeId'] or 'unknown'}"
                desc += f"• {u['username']} — {loc}\n"
            if len(other_players) > 10:
                desc += f"_+{len(other_players)-10} more_\n"
        if not target_players and not other_players:
            desc += "\n_No one in game right now._"

        payload = {
            "embeds": [{
                "title": title,
                "description": desc[:4000],
                "color": 0x00ff00 if target_players else 0x808080,
                "url": meta['targetGameUrl'],
                "timestamp": meta['timestamp'],
                "footer": {"text": f"Group {meta['groupId']} • {meta['elapsed_seconds']}s • Shard {meta['settings'].get('shard','-')}"}
            }],
            "username": "Roblox Scanner"
        }
        # Truncate if needed
        import json as _json
        data = _json.dumps(payload).encode()
        req = requests.post(webhook_url, data=data, headers={"Content-Type": "application/json"}, timeout=10)
        if req.status_code in (200, 204):
            log.info(f"Discord webhook sent ({req.status_code})")
        else:
            log.warning(f"Discord webhook failed {req.status_code}: {req.text[:300]}")
    except Exception as e:
        log.warning(f"Discord webhook error: {e}")

def export_csv(path: str, target_players, other_players):
    # Combined prioritized CSV: target first, then others
    fieldnames = ["priority", "username", "displayName", "userId", "role", "rank",
                  "presenceType", "presenceTypeName", "lastLocation", "placeId", "rootPlaceId", "universeId", "profileUrl"]
    rows = []
    for u in target_players:
        rows.append({
            "priority": "TARGET_GAME",
            "username": u["username"],
            "displayName": u["displayName"],
            "userId": u["userId"],
            "role": u["role"],
            "rank": u["rank"],
            "presenceType": u["presenceType"],
            "presenceTypeName": u["presenceTypeName"],
            "lastLocation": u["lastLocation"],
            "placeId": u["placeId"],
            "rootPlaceId": u["rootPlaceId"],
            "universeId": u["universeId"],
            "profileUrl": f"https://www.roblox.com/users/{u['userId']}/profile",
        })
    for u in other_players:
        rows.append({
            "priority": "OTHER_GAME",
            "username": u["username"],
            "displayName": u["displayName"],
            "userId": u["userId"],
            "role": u["role"],
            "rank": u["rank"],
            "presenceType": u["presenceType"],
            "presenceTypeName": u["presenceTypeName"],
            "lastLocation": u["lastLocation"],
            "placeId": u["placeId"],
            "rootPlaceId": u["rootPlaceId"],
            "universeId": u["universeId"],
            "profileUrl": f"https://www.roblox.com/users/{u['userId']}/profile",
        })
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    log.info(f"CSV exported to {path} ({len(rows)} rows)")


def parse_args():
    p = argparse.ArgumentParser(
        description="Scan ROBLOX group members and prioritize those playing a target game (rate-limit safe).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--group-id", type=int, default=DEFAULT_GROUP_ID, help="ROBLOX group ID to scan")
    p.add_argument("--place-id", type=int, default=DEFAULT_PLACE_ID, help="Target PlaceId (e.g., 74205509034203)")
    p.add_argument("--universe-id", type=int, default=None, help="Target UniverseId (auto-resolved from PlaceId if not given)")
    p.add_argument("--json", type=str, default="roblox_scan_results.json", help="Path to write JSON results (empty to skip)")
    p.add_argument("--csv", type=str, default="", help="Path to write CSV results (empty to skip)")
    p.add_argument("--throttle", type=float, default=None, help="Override delay between requests (seconds). Higher = safer for huge groups")
    p.add_argument("--group-delay", type=float, default=DEFAULT_GROUP_DELAY, help="Delay between group page requests")
    p.add_argument("--presence-delay", type=float, default=DEFAULT_PRESENCE_DELAY, help="Delay between presence batches (per worker)")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="Concurrent workers for presence checks (1=sequential, 4=fast, 8=risky)")
    p.add_argument("--max-members", type=int, default=None, help="Limit members scanned (for testing). Default= all")
    p.add_argument("--progress", action="store_true", help="Show progress bars (requires tqdm)")
    p.add_argument("--verbose", action="store_true", help="Enable debug logging")
    p.add_argument("--no-auto-universe", action="store_true", help="Skip auto resolving UniverseId")
    # Cookie pool (6x speed boost)
    p.add_argument("--cookies", type=str, default="", help="Comma-separated .ROBLOSECURITY cookies (up to 6) — or set ROBLOSECURITY_1..6 env vars")
    p.add_argument("--cookie-file", type=str, default="", help="Path to file with one .ROBLOSECURITY per line (up to 6) — preferred secure method")
    # Sharding for GitHub Actions / multi-VM free parallel (0$)
    p.add_argument("--shard", type=int, default=None, help="Shard index (0-based) for GitHub Actions matrix - e.g., 0 of 3")
    p.add_argument("--shards", type=int, default=None, help="Total number of shards - e.g., 3 for 3 parallel runners")
    p.add_argument("--webhook", type=str, default="", help="Discord webhook URL to post results (or set DISCORD_WEBHOOK secret)")
    return p.parse_args()


def main():
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # === Load Cookie Pool (6x speed boost) ===
    cookies = load_cookies(args)
    pool = init_cookie_pool(cookies)
    pool_size = get_pool_size()
    # cookies empty -> pool has 1 unauthenticated session; else N authenticated sessions
    is_authenticated = len(cookies) > 0

    group_delay = args.throttle if args.throttle is not None else args.group_delay
    presence_delay = args.throttle if args.throttle is not None else args.presence_delay
    workers = args.workers

    # Auto-tune for multi-cookie: each cookie has its own 60 req/min, so we can go faster
    if is_authenticated and args.throttle is None:
        # Only auto-tune if user didn't explicitly set throttle
        if pool_size >= 2:
            # Presence: keep per-cookie delay but allow higher global throughput
            # User can still override with --presence-delay
            if args.presence_delay == DEFAULT_PRESENCE_DELAY:
                # Scale: 2 cookies -> 0.7s, 3+ cookies -> 0.4s, 6 cookies -> 0.35s
                if pool_size >= 6:
                    presence_delay = 0.35
                elif pool_size >= 4:
                    presence_delay = 0.4
                elif pool_size >= 3:
                    presence_delay = 0.5
                elif pool_size == 2:
                    presence_delay = 0.65
                log.info(f"Multi-cookie auto-tune: {pool_size} cookies → presence-delay {presence_delay}s (vs {DEFAULT_PRESENCE_DELAY}s single)")
            if args.workers == DEFAULT_WORKERS:
                workers = min(pool_size, 6)
                log.info(f"Multi-cookie auto-tune: scaling workers to {workers} (pool size {pool_size})")
            if args.group_delay == DEFAULT_GROUP_DELAY and pool_size >= 3:
                group_delay = 0.35
                log.info(f"Multi-cookie auto-tune: group-delay {group_delay}s (vs {DEFAULT_GROUP_DELAY}s single)")

    # Use progress if requested and tqdm available, else auto if tqdm present
    show_progress = args.progress or HAS_TQDM

    # Primary session for non-pooled calls (universe/game resolve) — uses round-robin
    session = get_next_session()

    # Resolve UniverseId
    target_universe = args.universe_id
    if target_universe is None and not args.no_auto_universe:
        target_universe = resolve_universe_id(session, args.place_id)
        if target_universe is None:
            target_universe = DEFAULT_UNIVERSE_ID  # fallback to known
            log.info(f"Using fallback UniverseId {target_universe}")

    game_name = None
    if target_universe:
        game_name = resolve_game_name(session, target_universe)
        if game_name:
            log.info(f'Target game name: "{game_name}"')

    # 1. Fetch all group members (cursor paginated, sequential, max 100/page)
    start = time.time()
    members = fetch_all_group_members(
        session,
        group_id=args.group_id,
        delay=group_delay,
        max_members=args.max_members,
        show_progress=show_progress,
    )

    if not members:
        log.error("No members fetched. Check group ID and try again.")
        sys.exit(1)

    # === Sharding for GitHub Actions (free parallel) ===
    # If --shard/--shards set, only check presence for this slice
    # Each shard still fetched all members (28 req) but only ~19 presence batches when shards=3
    # Example: 2771 members, shards=3 -> shard 0: 0-923, shard 1: 924-1846, shard 2: 1847-2771
    shard = args.shard
    shards = args.shards
    is_sharded = shard is not None and shards is not None
    if is_sharded:
        if not (0 <= shard < shards):
            log.error(f"Invalid shard {shard} for shards {shards} (must be 0 <= shard < shards)")
            sys.exit(1)
        total = len(members)
        # Slice members for this shard
        chunk = (total + shards - 1) // shards  # ceil
        start_idx = shard * chunk
        end_idx = min(start_idx + chunk, total)
        members_slice = members[start_idx:end_idx]
        log.info(f"Shard {shard}/{shards}: handling members {start_idx}-{end_idx-1} ({len(members_slice)}/{total})")
        # For presence we only need this slice's userIds, but keep full members for offline counts?
        # We'll check presence only for slice, and categorize only slice for target/other
        # Offline for shard = slice size - found, but final merge will combine
        members_for_presence = members_slice
    else:
        members_for_presence = members

    # 2. Fetch presences in efficient batched + concurrent way
    user_ids = [m["userId"] for m in members_for_presence]
    presences = fetch_all_presences(
        session,
        user_ids,
        batch_size=PRESENCE_BATCH_SIZE,
        workers=workers,
        delay=presence_delay,
        show_progress=show_progress,
    )

    # 3. Categorize + prioritize
    target_players, other_players, offline = categorize_users(
        members_for_presence, presences, args.place_id, target_universe
    )

    elapsed = time.time() - start
    # For sharded runs, show shard info; for full runs show total
    print_members_total = len(members_for_presence) if is_sharded else len(members)
    print_results(
        target_players, other_players, len(offline), print_members_total,
        args.place_id, target_universe, game_name
    )
    if is_sharded:
        log.info(f"Shard {shard}/{shards} completed in {elapsed:.1f}s ({len(members_for_presence)} slice, {len(presences)} presence responses)")
    else:
        log.info(f"Scan completed in {elapsed:.1f}s ({len(members)} members, {len(presences)} presence responses)")

    # 4. Exports
    # For sharded runs, total_members is slice size; merge job will sum
    meta_total = len(members_for_presence) if is_sharded else len(members)
    meta = {
        "groupId": args.group_id,
        "groupUrl": f"https://www.roblox.com/communities/{args.group_id}/",
        "targetPlaceId": args.place_id,
        "targetUniverseId": target_universe,
        "targetGameName": game_name,
        "targetGameUrl": f"https://www.roblox.com/games/{args.place_id}/",
        "total_members": meta_total,
        "full_group_size": len(members),
        "elapsed_seconds": round(elapsed, 2),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "settings": {
            "group_delay": group_delay,
            "presence_delay": presence_delay,
            "workers": workers,
            "batch_size": PRESENCE_BATCH_SIZE,
            "cookie_pool_size": pool_size,
            "authenticated": is_authenticated,
            "shard": shard if is_sharded else None,
            "shards": shards if is_sharded else None,
        }
    }
    # For sharded runs, also include shard info in JSON filename hint
    if is_sharded:
        meta["shard_info"] = f"{shard}/{shards} members {len(members_for_presence)}/{len(members)}"

    if args.json:
        try:
            export_json(args.json, target_players, other_players, offline, meta)
        except Exception as e:
            log.error(f"Failed to export JSON: {e}")

    if args.csv:
        try:
            export_csv(args.csv, target_players, other_players)
        except Exception as e:
            log.error(f"Failed to export CSV: {e}")

    # 5. Discord webhook (optional, for future - uses arg or DISCORD_WEBHOOK env secret)
    webhook_url = args.webhook or os.environ.get("DISCORD_WEBHOOK", "") or os.environ.get("DISCORD_WEBHOOK_URL", "")
    if webhook_url:
        # For sharded runs, only send webhook from shard 0 or when not sharded to avoid spam
        # But if each shard wants to send, they can - we'll send per-shard with shard footer
        # For now send always; merge job will send final combined webhook instead
        if not is_sharded or shard == 0:
            # Only shard 0 sends per-shard to avoid 3x spam; final merge sends combined
            if is_sharded and shards and shards > 1:
                log.info(f"Shard {shard}: skipping per-shard webhook (merge job will send combined)")
            else:
                send_discord_webhook(webhook_url, target_players, other_players, meta)
        else:
            log.info(f"Shard {shard}: skipping webhook (only shard 0 sends)")

    # Exit code: 0 if any target found, 1 if none? Let's keep 0 always but log hint
    if target_players:
        log.info(f"Found {len(target_players)} target players — check output above!")
    else:
        log.info("No target players online right now. Try again in a few minutes.")


if __name__ == "__main__":
    main()
