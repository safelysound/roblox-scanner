#!/usr/bin/env python3
"""
Auto-Follow all developer_ids.txt (102) with the alt that owns ROBLOSECURITY.
Robust version: handles 403 Challenge + 429 rate-limit with exponential backoff,
verifies via /followings, and retries until all done (or gives up with non-zero).

Key fixes for 2026-09-21 failure (0/102):
  - 1st 2 requests got 403 {"code":0,"message":"Challenge is required..."} (datacenter IP flag)
  - rest got 429 even after 5s sleep -> needs 60s+ backoff
  - previous 0.6s delay is too fast; now 12s + jitter
  - verifies at end by GET /v1/users/{myId}/followings and re-loops failed
"""
import os, time, sys, random, requests
from pathlib import Path

IDS_FILE = "config/developer_ids.txt"
FOLLOW_API = "https://friends.roblox.com/v1/users/{uid}/follow"
FOLLOWING_API = "https://friends.roblox.com/v1/users/{uid}/followings"
CSRF_URL = "https://auth.roblox.com/v2/logout"

# Tunables - slowest free path that still avoids 429/Challenge
BASE_DELAY = 12.0          # seconds between follows (12s = 5/min, safe for datacenter IP)
JITTER = 3.0               # +/- jitter
MAX_RETRIES_PER_UID = 4    # per UID attempts
MAX_PASSES = 3             # full passes over remaining IDs after first pass
RETRY_BACKOFF = [15, 30, 60, 120]  # seconds for 429/Challenge retries (exponential)

def get_cookie():
    try:
        if Path("cookie.txt").exists():
            v=Path("cookie.txt").read_text(encoding="utf-8").strip()
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
    seen=set(); uniq=[]
    for i in ids:
        if i not in seen:
            seen.add(i); uniq.append(i)
    return uniq

def get_my_id(session):
    try:
        r = session.get("https://users.roblox.com/v1/users/authenticated", timeout=10)
        if r.status_code==200:
            return r.json().get("id")
    except: pass
    return None

def get_followings(session, my_id):
    """Return set of userIds the alt is already following (paginated, up to 2000)."""
    following=set()
    cursor=""
    for _ in range(20):  # 20*100=2000 enough for 102
        url = f"{FOLLOWING_API.format(uid=my_id)}?limit=100&sortOrder=Asc"
        if cursor:
            url += f"&cursor={cursor}"
        try:
            r = session.get(url, timeout=15)
            if r.status_code!=200:
                print(f"  followings GET {r.status_code} {r.text[:120]}")
                break
            j=r.json()
            for entry in j.get("data", []):
                following.add(entry["id"])
            cursor=j.get("nextPageCursor")
            if not cursor:
                break
        except Exception as e:
            print(f"  followings error {e}")
            break
        time.sleep(0.5)
    return following

def follow_one(session, uid, attempt=1):
    url = FOLLOW_API.format(uid=uid)
    try:
        r = session.post(url, timeout=15)
        # success
        if r.status_code in (200,201):
            try:
                j=r.json() if r.text else {}
            except: j={}
            if j.get("success") is True or r.status_code==200:
                return ("ok", r.status_code, "")
            return ("ok", r.status_code, r.text[:200])
        # already following
        if r.status_code==400 and "already" in r.text.lower():
            return ("already", r.status_code, r.text[:200])
        # rate limited
        if r.status_code==429:
            retry_after = r.headers.get("Retry-After")
            try:
                wait = int(float(retry_after)) if retry_after else RETRY_BACKOFF[min(attempt-1, len(RETRY_BACKOFF)-1)]
            except:
                wait = RETRY_BACKOFF[min(attempt-1, len(RETRY_BACKOFF)-1)]
            # Challenge headers on 429 sometimes hide Challenge
            challenge = r.headers.get("rblx-challenge-type") or r.headers.get("rblx-challenge-id")
            msg = f"429 wait {wait}s" + (f" challenge {challenge}" if challenge else "")
            return ("retry", wait, msg + " " + r.text[:100])
        # challenge required (403)
        if r.status_code==403:
            body = r.text.lower()
            if "challenge" in body or "challenge" in str(r.headers).lower():
                # try to get new csrf from this response
                new_tok = r.headers.get("x-csrf-token")
                if new_tok:
                    session.headers["x-csrf-token"] = new_tok
                    print(f"    -> refreshed CSRF {new_tok[:8]}...")
                challenge_type = r.headers.get("rblx-challenge-type", "")
                challenge_id = r.headers.get("rblx-challenge-id", "")
                # Roblox challenge needs human - we can only back off and retry later
                wait = RETRY_BACKOFF[min(attempt-1, len(RETRY_BACKOFF)-1)]
                return ("retry", wait, f"403 Challenge {challenge_type or challenge_id or body[:80]} wait {wait}s")
            # normal CSRF validation failed -> refresh token
            if "x-csrf-token" in r.headers:
                new_tok = r.headers["x-csrf-token"]
                session.headers["x-csrf-token"] = new_tok
                return ("retry", 2, f"403 CSRF refreshed, retry")
            return ("fail", r.status_code, r.text[:200])
        # other 4xx/5xx -> retryable
        if r.status_code in (500,502,503,504):
            wait = RETRY_BACKOFF[min(attempt-1, len(RETRY_BACKOFF)-1)]
            return ("retry", wait, f"{r.status_code} server error wait {wait}s")
        return ("fail", r.status_code, r.text[:220])
    except requests.exceptions.RequestException as e:
        wait = RETRY_BACKOFF[min(attempt-1, len(RETRY_BACKOFF)-1)]
        return ("retry", wait, f"exception {e} wait {wait}s")

def main():
    cookie = get_cookie()
    if not cookie:
        print("No ROBLOSECURITY/ROBLOX_COOKIE in env.", file=sys.stderr)
        sys.exit(1)
    ids = load_ids(IDS_FILE)
    print(f"Loaded {len(ids)} IDs from {IDS_FILE}")

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0",
        "Referer": "https://www.roblox.com/",
        "Origin": "https://www.roblox.com",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    s.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")
    token = get_csrf(s)
    if token:
        s.headers["x-csrf-token"] = token
        print(f"CSRF token {token[:8]}...")
    else:
        print("Warning: no CSRF token", file=sys.stderr)

    my_id = get_my_id(s)
    if my_id:
        print(f"Alt authenticated as {my_id}")
        # Check who we already follow to skip
        print("Fetching current followings (to skip already followed)...")
        following = get_followings(s, my_id)
        print(f"Already following {len(following)} users")
        already_skipped = [uid for uid in ids if uid in following]
        if already_skipped:
            print(f"  Skipping {len(already_skipped)} already followed: {already_skipped[:10]}{'...' if len(already_skipped)>10 else ''}")
    else:
        print("Could not get myId, will try blind", file=sys.stderr)
        following=set()
        my_id = None

    remaining = [uid for uid in ids if uid not in following]
    print(f"Need to follow {len(remaining)}/{len(ids)}")
    if not remaining:
        print("All done — no remaining.")
        sys.exit(0)

    total_success=0
    total_already=len(following.intersection(set(ids)))
    total_failed=0
    # Track attempts per uid
    attempts = {uid:0 for uid in remaining}
    failed_last_reason={}

    # First pass + up to MAX_PASSES re-passes for retryable failures
    to_try = remaining[:]
    passes = 0
    while to_try and passes < (1+MAX_PASSES):
        passes+=1
        print(f"\n=== Pass {passes}/{1+MAX_PASSES} — {len(to_try)} remaining ===")
        next_round=[]
        for idx, uid in enumerate(to_try, 1):
            attempts[uid]+=1
            att = attempts[uid]
            print(f"[{idx}/{len(to_try)}] (attempt {att}/{MAX_RETRIES_PER_UID}) Follow {uid} ...", flush=True)
            status, code, msg = follow_one(s, uid, attempt=att)
            if status=="ok":
                print(f"  -> OK ({code})")
                total_success+=1
                following.add(uid)
            elif status=="already":
                print(f"  -> already following (skip)")
                total_already+=1
                following.add(uid)
            elif status=="retry":
                # code is wait seconds here
                wait = code
                print(f"  -> RETRY in {wait}s: {msg}")
                failed_last_reason[uid]=msg
                if att < MAX_RETRIES_PER_UID:
                    # sleep for backoff before next uid? But we already need to wait for this uid's retry
                    # Instead queue for next_round and sleep now
                    jitter = random.uniform(-1, 1)
                    sleep_for = max(1, wait + jitter)
                    print(f"    sleeping {sleep_for:.1f}s for backoff...")
                    time.sleep(sleep_for)
                    next_round.append(uid)
                else:
                    print(f"    max retries reached for {uid}, giving up this pass")
                    next_round.append(uid)  # still keep for final verification, but will count as failed
            else: # fail
                print(f"  -> FAIL {code}: {msg}")
                failed_last_reason[uid]=f"{code} {msg}"
                if att < MAX_RETRIES_PER_UID and code not in (400, 401, 403, 404): # don't retry hard fails except challenge which is handled as retry
                    next_round.append(uid)
                elif "Challenge" in msg and att < MAX_RETRIES_PER_UID:
                    next_round.append(uid)
                else:
                    # final fail, keep for summary but don't retry forever
                    next_round.append(uid)

            # Delay between follows (only if not already sleeping for retry)
            if status!="retry":
                d = BASE_DELAY + random.uniform(-JITTER, JITTER)
                # every 10 follows, add extra 5s to cool down
                if idx % 10 == 0:
                    d += 5
                print(f"    delay {d:.1f}s...", flush=True)
                time.sleep(d)
            # refresh CSRF every 20 follows (token can expire)
            if idx % 20 == 0:
                nt = get_csrf(s)
                if nt:
                    s.headers["x-csrf-token"] = nt
                    print(f"    refreshed CSRF {nt[:8]}")

        # After a full pass, re-verify via API which are actually still unfollowed (Roblox may have succeeded despite 429)
        if my_id and next_round:
            print(f"\nPass {passes} done. Re-checking {len(next_round)} still pending via followings API...")
            time.sleep(3)
            refreshed = get_followings(s, my_id)
            still_missing = [uid for uid in next_round if uid not in refreshed]
            became_ok = len(next_round) - len(still_missing)
            if became_ok:
                print(f"  {became_ok} became followed during pass (verified)")
                total_success += became_ok
                total_already += 0
            print(f"  Still missing after verification: {len(still_missing)}")
            # filter next_round to only those truly missing and not exceeded max retries?
            # keep only those with attempts < MAX_RETRIES_PER_UID, others will be final failed
            filtered=[]
            for uid in still_missing:
                if attempts[uid] < MAX_RETRIES_PER_UID:
                    filtered.append(uid)
                else:
                    print(f"  {uid} exhausted retries ({failed_last_reason.get(uid,'')})")
            to_try = filtered
            if to_try:
                print(f"  Will retry {len(to_try)} in next pass after 30s cooldown...")
                time.sleep(30)
            else:
                print("  No retryable remaining.")
                # keep failed list for final report
                to_try = still_missing  # for final failed count
                break
        else:
            to_try = next_round

        # If we've done MAX_RETRIES, break
        if passes >= 1+MAX_PASSES:
            break

    # Final verification
    if my_id:
        final_following = get_followings(s, my_id)
        missing_final = [uid for uid in ids if uid not in final_following]
        succeeded_final = len(ids) - len(missing_final)
        print(f"\n=== FINAL VERIFICATION ===")
        print(f"Target: {len(ids)} | Following (verified): {succeeded_final} | Missing: {len(missing_final)}")
        if missing_final:
            print(f"Missing IDs ({len(missing_final)}): {missing_final[:20]}{'...' if len(missing_final)>20 else ''}")
            for uid in missing_final[:10]:
                print(f"  {uid} last reason: {failed_last_reason.get(uid,'')}")
        total_success = succeeded_final - total_already if succeeded_final>=total_already else succeeded_final
        total_failed = len(missing_final)
        # Write missing to file for next resume
        Path("follow_failed.txt").write_text("\n".join(map(str, missing_final)) + "\n" if missing_final else "")
        if missing_final:
            print(f"Wrote {len(missing_final)} missing to follow_failed.txt for resume")
    else:
        missing_final = to_try
        total_failed = len(missing_final)

    print(f"\nDone: {total_success} newly followed, {total_already} already, {total_failed} failed / {len(ids)} total")

    # Exit code: success only if all done
    if total_failed>0:
        print(f"ERROR: {total_failed} still not followed — workflow will be marked FAILED so you get Discord ping and can re-run.", file=sys.stderr)
        print("Tip: Re-run workflow in 10 min; GitHub datacenter IP cooldown helps. Or run locally from home IP (no rate-limit).", file=sys.stderr)
        sys.exit(2)
    else:
        print("All 102 followed — next tracker tick will see Hunt.")
        sys.exit(0)

if __name__=="__main__":
    main()
