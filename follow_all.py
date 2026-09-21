#!/usr/bin/env python3
"""
Auto-Follow all developer_ids.txt (102) with the alt that owns ROBLOSECURITY.
Uses same cookie you already added as GitHub Secret ROBLOX_COOKIE / ROBLOSECURITY.

Usage (local, 30s):
  export ROBLOSECURITY="_|WARNING:-DO-NOT-SHARE-THIS.--..."
  python follow_all.py

Or in GitHub Actions it will read the secret automatically.

This calls: POST https://friends.roblox.com/v1/users/{targetId}/follow  with X-CSRF-TOKEN
"""
import os, time, sys, requests
from pathlib import Path

IDS_FILE = "config/developer_ids.txt"
FOLLOW_API = "https://friends.roblox.com/v1/users/{uid}/follow"  # POST
CSRF_URL = "https://auth.roblox.com/v2/logout"  # to get token

def get_cookie():
    # Try file first to avoid Windows | quoting issues
    try:
        import pathlib
        if pathlib.Path("cookie.txt").exists():
            v=pathlib.Path("cookie.txt").read_text(encoding="utf-8").strip()
            if v:
                if v.startswith(".ROBLOSECURITY="):
                    v=v.split("=",1)[1]
                return v.strip().strip('"').strip("'")
    except: pass
    for k in ["ROBLOSECURITY", "ROBLOX_COOKIE", "ROBLOSECURITY_1"]:
        v = os.environ.get(k, "")
        if v and v.strip():
            v = v.strip().strip('"').strip("'")
            if v.startswith(".ROBLOSECURITY="):
                v = v.split("=",1)[1]
            if v:
                return v
    return ""

def get_csrf(session):
    # Roblox returns token even on 403
    try:
        r = session.post(CSRF_URL, timeout=10)
        tok = r.headers.get("x-csrf-token")
        if tok:
            return tok
    except: pass
    return None

def load_ids(path):
    ids=[]
    for line in Path(path).read_text().splitlines():
        line=line.strip()
        if not line or line.startswith("#"): continue
        for part in line.replace(",", " ").split():
            if part.isdigit():
                ids.append(int(part))
    # dedupe
    seen=set(); uniq=[]
    for i in ids:
        if i not in seen:
            seen.add(i); uniq.append(i)
    return uniq

def main():
    cookie = get_cookie()
    if not cookie:
        print("No ROBLOSECURITY/ROBLOX_COOKIE in env. Set it first:", file=sys.stderr)
        print('  export ROBLOSECURITY="_|WARNING:-DO-NOT-SHARE-THIS..."', file=sys.stderr)
        sys.exit(1)

    ids = load_ids(IDS_FILE)
    print(f"Loaded {len(ids)} IDs from {IDS_FILE}")

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.roblox.com/",
        "Origin": "https://www.roblox.com",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    s.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")
    # CSRF
    token = get_csrf(s)
    if token:
        s.headers["x-csrf-token"] = token
        print(f"CSRF token {token[:8]}...")
    else:
        print("Warning: no CSRF token — follow may 403", file=sys.stderr)

    # Who is this alt? (so you see it's the right account)
    try:
        r = s.get("https://users.roblox.com/v1/users/authenticated", timeout=10)
        if r.status_code==200 and r.json().get("id"):
            print(f"Alt authenticated as @{r.json()['name']} ({r.json()['id']})")
        else:
            print(f"Alt auth check failed: {r.status_code} {r.text[:200]}", file=sys.stderr)
    except Exception as e:
        print(f"Auth check error: {e}", file=sys.stderr)

    # Check already following to skip
    # GET https://friends.roblox.com/v1/users/{myId}/followings?limit=100
    # Instead just try follow and handle "Already following" gracefully
    success=0; skipped=0; failed=0
    for idx, uid in enumerate(ids, 1):
        url = FOLLOW_API.format(uid=uid)
        try:
            r = s.post(url, timeout=15)
            if r.status_code in (200,201):
                # 200 = followed, check body
                j = r.json() if r.text else {}
                # Roblox returns {"success":true} or similar
                if j.get("success") is True or r.status_code==200:
                    print(f"{idx}/{len(ids)} Follow {uid} -> OK ({r.status_code})")
                    success+=1
                else:
                    print(f"{idx}/{len(ids)} Follow {uid} -> {r.text[:200]}")
                    success+=1
            elif r.status_code==400 and "already" in r.text.lower():
                print(f"{idx}/{len(ids)} Follow {uid} -> already following (skipped)")
                skipped+=1
            elif r.status_code==429:
                retry = int(r.headers.get("Retry-After", "10") or 10)
                print(f"{idx}/{len(ids)} 429 rate-limited -> sleep {retry}s")
                time.sleep(retry+1)
                # retry once
                r2 = s.post(url, timeout=15)
                if r2.status_code in (200,201):
                    print(f"  retry {uid} -> OK")
                    success+=1
                else:
                    print(f"  retry {uid} -> {r2.status_code} {r2.text[:200]}")
                    failed+=1
            else:
                # Try to refresh CSRF if 403
                if r.status_code==403 and "x-csrf-token" in r.headers:
                    new_tok = r.headers["x-csrf-token"]
                    s.headers["x-csrf-token"] = new_tok
                    print(f"  403 CSRF refresh {new_tok[:8]} -> retry")
                    r2 = s.post(url, timeout=15)
                    if r2.status_code in (200,201):
                        print(f"  retry {uid} -> OK")
                        success+=1
                    else:
                        print(f"  retry {uid} -> {r2.status_code} {r2.text[:200]}")
                        failed+=1
                else:
                    print(f"{idx}/{len(ids)} Follow {uid} -> {r.status_code} {r.text[:200]}")
                    failed+=1
        except Exception as e:
            print(f"{idx}/{len(ids)} Follow {uid} -> exception {e}")
            failed+=1

        time.sleep(0.6)  # ~100/min safe

    print(f"\nDone: {success} followed, {skipped} already, {failed} failed / {len(ids)} total")

if __name__=="__main__":
    main()
