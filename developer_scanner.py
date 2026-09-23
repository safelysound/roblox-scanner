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
DEFAULT_PLACE_ID = 74205509034203
DEFAULT_UNIVERSE_ID = 10766456501

# ================= Cookie support (fixes Offline-but-Follow-shows-Hunt) =================
# Reads alt cookie that Follows the 102 devs. Presence then returns InGame instead of Offline.
# Supports: ROBLOSECURITY, ROBLOX_COOKIE, ROBLOSECURITY_1 (all read, first found used)
def _get_roblox_cookie() -> str:
    for key in ["ROBLOX_COOKIE", "ROBLOSECURITY", "ROBLOSECURITY_1", "ROBLOSECURITY_2", "ROBLOSECURITY_3", "ROBLOSECURITY_4", "ROBLOSECURITY_5", "ROBLOSECURITY_6", "ROBLOSECURITY1", "ROBLOSECURITY2"]:
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
    for key in ["ROBLOX_COOKIE", "ROBLOSECURITY", "ROBLOSECURITY_1", "ROBLOSECURITY_2", "ROBLOSECURITY_3", "ROBLOSECURITY_4", "ROBLOSECURITY_5", "ROBLOSECURITY_6"]:
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
    # Use Session to preserve RBXID etc; also check www.roblox.com/home for auth
    try:
        import requests as _req, sys, re
        ck = headers.get("Cookie","")
        # First check if www.roblox.com sees us as logged in (bypasses 9002)
        try:
            s=_req.Session()
            s.headers.update({"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language":"en-US,en;q=0.9", "Referer":"https://www.roblox.com/", "Cookie": ck})
            rh=s.get("https://www.roblox.com/home", timeout=10)
            auth_m = re.search(r'"isAuthenticated"\s*:\s*true', rh.text)
            uid_m = re.search(r'"userId"\s*:\s*(\d+)', rh.text)
            print(f"home check status {rh.status_code} isAuth {bool(auth_m)} hasCookie {bool(ck)}", file=sys.stderr)
            if auth_m and uid_m:
                return int(uid_m.group(1))
        except Exception as e:
            print(f"home auth check error {e}", file=sys.stderr)
        r=_req.get("https://users.roblox.com/v1/users/authenticated", timeout=10, headers={"User-Agent":"Mozilla/5.0","Cookie": ck, "Referer":"https://www.roblox.com/", "Accept":"application/json"})
        if r.status_code==200:
            print(f"my_id {r.json().get('id')} ok", file=sys.stderr)
            return r.json().get("id")
        else:
            print(f"my_id status {r.status_code} body {r.text[:200]!r}", file=sys.stderr)
            r2=_req.get("https://users.roblox.com/v1/users/authenticated", timeout=10, headers={"User-Agent":"Mozilla/5.0","Cookie": ck, "Referer":"https://www.roblox.com/", "Origin":"https://www.roblox.com"})
            print(f"my_id retry status {r2.status_code} body {r2.text[:200]!r}", file=sys.stderr)
            if r2.status_code==200:
                return r2.json().get("id")
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

def _profile_shows_hunt(user_id, headers):
    """Profile scraping fallback for hidden even when Following (e.g., 91512961, 92501615).
    Tries multiple endpoints that the website uses to show Hunt when Following.
    Free fix: get CSRF from HTML meta (not auth.roblox.com which 401s), then presence with CSRF — tries pool.
    """
    import requests as _req, re, sys
    # Try pool in order so the cookie that actually follows reveals Hunt
    pool=_get_all_cookies()
    # Build candidate cookies: pool first, then fallback to headers cookie
    candidates=[]
    seen=set()
    for _,c in pool:
        cc=f".ROBLOSECURITY={c}"
        if cc not in seen:
            candidates.append(cc)
            seen.add(cc)
    hdr_ck=headers.get("Cookie","")
    if hdr_ck and hdr_ck not in seen:
        candidates.append(hdr_ck)
    ck=candidates[0] if candidates else headers.get("Cookie","")
    csrf_from_html=None
    # Helper to try presence with a given ck
    def _try_presence_with_ck(try_ck, tok):
        try:
            sess=_seeded_session(try_ck)
            h3={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)","Content-Type":"application/json","Accept":"application/json","Referer":f"https://www.roblox.com/users/{user_id}/profile","Origin":"https://www.roblox.com","Cookie": try_ck, "x-csrf-token": tok}
            r3=sess.post("https://presence.roblox.com/v1/presence/users", json={"userIds":[user_id]}, timeout=10, headers=h3)
            print(f"profile scrape {user_id}: CSRF presence via {try_ck[:14]}... status {r3.status_code}, body {r3.text[:500]!r}", file=sys.stderr)
            # Also log isFollowing check for that cookie's followings
            try:
                # quick check if this cookie follows target via friends API (less verbose)
                pass
            except: pass
            if r3.status_code==200:
                j=r3.json().get("userPresences",[{}])[0]
                pid=j.get("placeId")
                if pid and str(pid)=="74205509034203":
                    print(f"profile scrape {user_id}: found Hunt via presence+CSRF pool", file=sys.stderr)
                    return True
                if j.get("universeId")==10766456501:
                    print(f"profile scrape {user_id}: found Hunt via universe+CSRF pool", file=sys.stderr)
                    return True
        except Exception as e:
            print(f"profile scrape {user_id} pool presence error {e}", file=sys.stderr)
        return False

    # Use Session to keep RBXID + alt cookie together (helps bypass 9002)
    sess=_req.Session()
    sess.headers.update({"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36","Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Accept-Language":"en-US,en;q=0.9","Accept-Encoding":"gzip, deflate","Cache-Control":"no-cache","Pragma":"no-cache"})
    if ck:
        sess.headers.update({"Cookie": ck})
    # Try 1: Profile HTML with alt cookie - check for Hunt in embedded JSON, HTML, and also check Following button
    try:
        h2={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36","Referer":f"https://www.roblox.com/users/{user_id}/profile","Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Accept-Language":"en-US,en;q=0.9","Cookie": ck}
        r=sess.get(f"https://www.roblox.com/users/{user_id}/profile", timeout=15, headers=h2)
        print(f"profile scrape {user_id}: HTML status {r.status_code}, len {len(r.text) if r else 0}", file=sys.stderr)
        if r.status_code==200:
            t=r.text
            # Extract CSRF from HTML for later presence call (avoids datacenter 401 on auth.roblox.com)
            m=re.search(r'<meta\s+name="csrf-token"\s+[^>]*data-token="([^"]+)"', t)
            if not m:
                m=re.search(r'<meta\s+name="csrf-token"\s+[^>]*content="([^"]+)"', t)
            if not m:
                m=re.search(r"Roblox\.XsrfToken\.setToken\('([^']+)'\)", t)
            if not m:
                m=re.search(r'"csrfToken"\s*:\s*"([^"]+)"', t)
            if m:
                csrf_from_html=m.group(1)
                print(f"profile scrape {user_id}: CSRF from HTML {csrf_from_html[:8]}...", file=sys.stderr)
            else:
                logged = "Sign Out" in t or "data-userid" in t.lower()
                print(f"profile scrape {user_id}: no CSRF in HTML loggedIn?{logged}", file=sys.stderr)
            found=False
            if "74205509034203" in t:
                print(f"profile scrape {user_id}: found Hunt via place in HTML", file=sys.stderr)
                found=True
            if "10766456501" in t:
                print(f"profile scrape {user_id}: found Hunt universe in HTML", file=sys.stderr)
                found=True
            if "The Hunt: Roblox 20" in t or "The Hunt Roblox 20" in t:
                print(f"profile scrape {user_id}: found Hunt name in HTML", file=sys.stderr)
                found=True
            if "roblox.com/games/74205509034203" in t:
                print(f"profile scrape {user_id}: found Hunt game link", file=sys.stderr)
                found=True
            if found:
                return True
            if '"placeId":74205509034203' in t or '"placeId":"74205509034203"' in t:
                print(f"profile scrape {user_id}: found Hunt via JSON placeId", file=sys.stderr)
                return True
            # Check if profile shows Following (means alt cookie is seen as logged in)
            is_following_html = 'data-following="true"' in t.lower() or 'following' in t.lower() and f'data-userid="{user_id}"' in t.lower()
            # More direct: look for Follow/Following button near user id
            if is_following_html:
                print(f"profile scrape {user_id}: HTML shows Following marker", file=sys.stderr)
            else:
                # log snippet around Follow button
                idx=t.lower().find("follow")
                snippet = t[max(0,idx-200):idx+200] if idx>=0 else "no follow keyword"
                print(f"profile scrape {user_id}: HTML not Following snippet {snippet[:300]!r}", file=sys.stderr)
            print(f"profile scrape {user_id}: no Hunt in HTML (first 500 chars: {t[:500]!r})", file=sys.stderr)
    except Exception as e:
        print(f"profile scrape {user_id} HTML error {e}", file=sys.stderr)
    # Try 2: Presence with CSRF — try each pool cookie until Hunt found (fresh CSRF per cookie)
    try:
        print(f"profile scrape {user_id}: trying {len(candidates)} pool cookies for Hunt", file=sys.stderr)
        for try_ck in candidates:
            sess=_seeded_session(try_ck)
            # Fresh CSRF per cookie (html from that cookie's session)
            tok=None
            try:
                rh=sess.get("https://www.roblox.com/home", timeout=10, headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)","Referer":"https://www.roblox.com/"})
                import re as _re2
                m=_re2.search(r'<meta\s+name="csrf-token"\s+[^>]*data-token="([^"]+)"', rh.text)
                if not m:
                    m=_re2.search(r'<meta\s+name="csrf-token"\s+[^>]*content="([^"]+)"', rh.text)
                if m:
                    tok=m.group(1)
                    print(f"profile scrape {user_id}: HTML CSRF {tok[:6]}... via {try_ck[:14]} html len {len(rh.text)}", file=sys.stderr)
            except: pass
            if not tok:
                # fallback to previous html token or auth
                tok=csrf_from_html
                if tok:
                    print(f"profile scrape {user_id}: fallback to first HTML CSRF {tok[:6]}... via {try_ck[:14]}", file=sys.stderr)
            if not tok:
                try:
                    r2=sess.post("https://auth.roblox.com/v2/logout", timeout=10, headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)","Cookie": try_ck, "Referer":"https://www.roblox.com/", "Origin":"https://www.roblox.com"})
                    tok=r2.headers.get("x-csrf-token")
                    print(f"profile scrape {user_id}: CSRF auth {tok[:8] if tok else None} status {r2.status_code} via {try_ck[:14]}", file=sys.stderr)
                except: pass
            else:
                print(f"profile scrape {user_id}: using fresh HTML CSRF {tok[:6]}... via {try_ck[:14]}", file=sys.stderr)
            if tok and _try_presence_with_ck(try_ck, tok):
                return True
            # Also try without Origin
            if tok:
                try:
                    sess2=_seeded_session(try_ck)
                    h3b={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)","Content-Type":"application/json","Accept":"application/json","Referer":"https://www.roblox.com/","Cookie": try_ck, "x-csrf-token": tok}
                    r3b=sess2.post("https://presence.roblox.com/v1/presence/users", json={"userIds":[user_id]}, timeout=10, headers=h3b)
                    print(f"profile scrape {user_id}: CSRF retry status {r3b.status_code}, body {r3b.text[:300]!r} via {try_ck[:14]}", file=sys.stderr)
                    if r3b.status_code==200:
                        j=r3b.json().get("userPresences",[{}])[0]
                        if j.get("placeId") and str(j.get("placeId"))=="74205509034203":
                            print(f"profile scrape {user_id}: found Hunt via presence+CSRF retry pool", file=sys.stderr)
                            return True
                except: pass
    except Exception as e:
        print(f"profile scrape {user_id} CSRF presence error {e}", file=sys.stderr)
    # Try 3: Mobile presence endpoint — loop pool
    try:
        import sys as _sys3
        for try_ck in candidates:
            tok3 = csrf_from_html
            h_mobile = {"User-Agent":"Roblox/Android","Accept":"application/json","Cookie": try_ck}
            if tok3:
                h_mobile["x-csrf-token"] = tok3
                h_mobile["Referer"]="https://www.roblox.com/"
            sess_m=_seeded_session(try_ck)
            r_m = sess_m.post("https://presence.roblox.com/v1/presence/users", json={"userIds":[user_id]}, timeout=10, headers=h_mobile)
            print(f"profile scrape {user_id}: mobile presence via {try_ck[:14]} status {r_m.status_code} body {r_m.text[:300]!r}", file=_sys3.stderr)
            if r_m.status_code==200:
                j = r_m.json().get("userPresences",[{}])[0]
                if j.get("placeId") and str(j.get("placeId"))=="74205509034203":
                    print(f"profile scrape {user_id}: found Hunt via mobile presence pool", file=_sys3.stderr)
                    return True
                if j.get("universeId")==10766456501:
                    print(f"profile scrape {user_id}: found Hunt via mobile universe pool", file=_sys3.stderr)
                    return True
    except Exception as e:
        print(f"profile scrape {user_id} mobile presence error {e}", file=__import__("sys").stderr)
    print(f"profile scrape {user_id}: no Hunt found after all tries", file=sys.stderr)
    return False
def _get_followings(my_id, headers):
    following=set()
    if not my_id:
        return following
    try:
        import requests as _req
        ck=headers.get("Cookie","")
        sess=_seeded_session(ck)
        cursor=""
        for _ in range(20):
            url=f"https://friends.roblox.com/v1/users/{my_id}/followings?limit=100&sortOrder=Asc"
            if cursor:
                url+=f"&cursor={cursor}"
            r=sess.get(url, timeout=10, headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)","Referer":"https://www.roblox.com/", "Origin":"https://www.roblox.com", "Accept":"application/json"})
            if r.status_code!=200:
                print(f"followings fetch status {r.status_code} body {r.text[:200]!r}", file=__import__("sys").stderr)
                # try without Origin fallback
                if r.status_code in (401,403,429):
                    r2=_req.get(url, timeout=10, headers={"User-Agent":"Mozilla/5.0","Cookie": headers.get("Cookie",""), "Referer":"https://www.roblox.com/"})
                    print(f"followings retry status {r2.status_code}", file=__import__("sys").stderr)
                    if r2.status_code!=200:
                        break
                    r=r2
                else:
                    break
            j=r.json()
            for e in j.get("data",[]):
                following.add(e["id"])
            cursor=j.get("nextPageCursor")
            if not cursor:
                break
    except Exception as e:
        print(f"followings error {e}", file=__import__("sys").stderr)
    return following

def resolve_universe_id(place_id: int) -> Optional[int]:
    try:
        r = requests.get(UNIVERSE_API.format(place_id=place_id), timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return r.json().get("universeId")
    except:
        pass
    return DEFAULT_UNIVERSE_ID

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

def fetch_presences(user_ids: List[int]) -> Dict[int, Dict[str, Any]]:
    # Batch 50 — authenticated if ROBLOSECURITY present (Follow bypass for Offline→Hunt)
    headers = _cookie_headers()
    if not hasattr(fetch_presences, "_logged"):
        _log_cookie_status()
        fetch_presences._logged = True
    # Get CSRF for authenticated presence - try HTML first (free, not blocked) then auth fallback
    csrf_token=None
    sess_fp=None
    if "Cookie" in headers and headers.get("Cookie"):
        try:
            import re
            ck2=headers.get("Cookie","")
            sess_fp=_seeded_session(ck2)
            # Try HTML CSRF first (www.roblox.com not challenged)
            try:
                rh=sess_fp.get("https://www.roblox.com/home", timeout=10, headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)","Referer":"https://www.roblox.com/"})
                if rh.status_code==200:
                    m=re.search(r'<meta\s+name="csrf-token"\s+[^>]*data-token="([^"]+)"', rh.text)
                    if not m:
                        m=re.search(r'<meta\s+name="csrf-token"\s+[^>]*content="([^"]+)"', rh.text)
                    if not m:
                        m=re.search(r"Roblox\.XsrfToken\.setToken\('([^']+)'\)", rh.text)
                    if m:
                        csrf_token=m.group(1)
                        print(f"presence CSRF from HTML {csrf_token[:8]}...", file=sys.stderr)
            except Exception as e:
                print(f"presence HTML CSRF failed {e}", file=sys.stderr)
            if not csrf_token:
                r_csrf=sess_fp.post("https://auth.roblox.com/v2/logout", timeout=10, headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)","Cookie": ck2, "Referer":"https://www.roblox.com/", "Origin":"https://www.roblox.com"})
                csrf_token=r_csrf.headers.get("x-csrf-token")
                print(f"presence CSRF auth status {r_csrf.status_code} tok {csrf_token[:8] if csrf_token else None}", file=sys.stderr)
            if csrf_token:
                headers["x-csrf-token"]=csrf_token
                headers["Referer"]="https://www.roblox.com/"
                headers["Origin"]="https://www.roblox.com"
                print(f"presence CSRF {csrf_token[:8]}...", file=sys.stderr)
        except Exception as e:
            print(f"presence CSRF fetch failed {e}", file=sys.stderr)
    results={}
    for i in range(0, len(user_ids), PRESENCE_BATCH_SIZE):
        batch=user_ids[i:i+PRESENCE_BATCH_SIZE]
        try:
            if sess_fp and "x-csrf-token" in headers:
                r=sess_fp.post(PRESENCE_API, json={"userIds": batch}, timeout=15, headers=headers)
            else:
                r=requests.post(PRESENCE_API, json={"userIds": batch}, timeout=15, headers=headers)
            # If 403 due to bad cookie/CSRF, retry unauthenticated as fallback
            if r.status_code==403 and "Cookie" in headers:
                print("Presence 403 with cookie — retrying unauthenticated (cookie may be invalid/expired)", file=sys.stderr)
                headers_no_cookie = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
                r=requests.post(PRESENCE_API, json={"userIds": batch}, timeout=15, headers=headers_no_cookie)
            if r.status_code==200:
                for p in r.json().get("userPresences",[]):
                    results[p["userId"]]=p
            else:
                print(f"Presence batch failed {r.status_code}: {r.text[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"Presence error {e}", file=sys.stderr)
        time.sleep(0.5)
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

    presences=fetch_presences(ids_for_presence)
    print(f"Got {len(presences)}/{len(ids_for_presence)} presence responses")

    # Fetch followings for isFollowing flag using pool (any of 6 follows -> true)
    headers_for_follow=_cookie_headers()
    pool=_get_all_cookies()
    followings=set()
    my_ids=[]
    for key, ck in pool:
        hdr={"Cookie": f".ROBLOSECURITY={ck}"}
        mid=_get_my_id(hdr)
        if mid:
            my_ids.append(mid)
            f=_get_followings(mid, hdr)
            print(f"pool {key} -> my_id {mid} following {len(f)}", file=sys.stderr)
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
            if _profile_shows_hunt(uid, headers_for_follow):
                print(f"profile scrape: {uid} shows Hunt via profile (hidden presence but profile reveals Hunt)", file=sys.stderr)
                place_id=74205509034203
                root_place=74205509034203
                univ=10766456501
                last_loc="The Hunt: Roblox 20"
                # Also mark as Following for discord display
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
