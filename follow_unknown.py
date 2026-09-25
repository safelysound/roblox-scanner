#!/usr/bin/env python3
"""
Follow Unknowns per embed — 2 per account then 30s cooldown, rotate.

Usage in cloud (GitHub Actions, device off):
  python follow_unknown.py --tracker developers --json roblox_scan_results_developers.json
  python follow_unknown.py --tracker all --json roblox_scan_results.json  # will detect
  python follow_unknown.py --file shard_developers_0.json  # explicit file

Reads tracker JSON (same as discord_updater), finds users with:
  Playing: Unknown (Must Follow/Game Status Hidden)
  => presenceType 2, placeId is None, isFollowing false (and filtered per discord_updater logic)

Pool: ROBLOX_COOKIE, ROBLOSECURITY, ROBLOSECURITY_1..5 (6 accounts)
Rule: each account max 2 follows, then 30s cooldown before reuse, rotate until all Unknown are followed.
Runs in background on GitHub Actions after embed update, no device needed.

Failure handling (pacing above is unchanged):
  - a user is skipped after MAX_TRIES_PER_USER failed attempts (challenge / error / other failure)
  - the run stops early when 2 x pool-size attempts in a row fail, or when Roblox answers 429 that
    many times in a row (everything is blocked / rate limited; stop instead of hammering Roblox and
    risking the accounts, the next run picks up where this one left off)
  - the run stops gracefully after --max-seconds (default 600)

Exit 0 if all followed or none, 2 if some failed / were not attempted.
"""
import os, sys, time, json, argparse, requests, random
from collections import Counter
from pathlib import Path

FOLLOW_API = "https://friends.roblox.com/v1/users/{uid}/follow"
CSRF_URL = "https://auth.roblox.com/v2/logout"
MAX_TRIES_PER_USER = 3

def _normalize(raw):
    raw=raw.strip().strip('"').strip("'")
    if raw.startswith(".ROBLOSECURITY="): raw=raw.split("=",1)[1]
    return raw

def get_all_cookies():
    pool=[]; seen=set()
    for k in ["ROBLOX_COOKIE","ROBLOSECURITY","ROBLOSECURITY_1","ROBLOSECURITY_2","ROBLOSECURITY_3","ROBLOSECURITY_4","ROBLOSECURITY_5","ROBLOSECURITY_6"]:
        v=os.environ.get(k,"")
        if v and v.strip():
            v=_normalize(v)
            if v and v not in seen:
                seen.add(v); pool.append((k,v))
    return pool

def get_csrf(sess):
    try:
        r=sess.post(CSRF_URL, timeout=10)
        tok=r.headers.get("x-csrf-token")
        if tok: return tok
    except: pass
    return None

def follow_one(sess, uid):
    url=FOLLOW_API.format(uid=uid)
    try:
        r=sess.post(url, timeout=15)
        if r.status_code in (200,201):
            return ("ok", r.status_code, "")
        if r.status_code==400 and "already" in r.text.lower():
            return ("already", r.status_code, r.text[:200])
        if r.status_code==429:
            retry_after=r.headers.get("Retry-After")
            try: wait=int(float(retry_after)) if retry_after else 5
            except: wait=5
            return ("retry", wait, f"429 {r.text[:120]}")
        if r.status_code==403:
            body=r.text.lower()
            # Try to refresh csrf if provided
            if "x-csrf-token" in r.headers:
                try: sess.headers["x-csrf-token"]=r.headers["x-csrf-token"]
                except: pass
            if "challenge" in body:
                return ("challenge", r.status_code, r.text[:200])
            return ("fail", r.status_code, r.text[:200])
        return ("fail", r.status_code, r.text[:300])
    except Exception as e:
        return ("error", 0, str(e))

def load_unknowns(json_path, tracker=None):
    data=json.loads(Path(json_path).read_text())
    # Reuse same filtering as discord_updater for Unknown
    target=data.get("target_game_players",[])
    other=data.get("other_game_players",[])
    # Use same TARGET_PLACE_ID
    TARGET="74205509034203"
    filtered=[]
    for u in target+other:
        pid=u.get("placeId") or u.get("rootPlaceId")
        ptype=u.get("presenceType")
        try: ptype_int=int(ptype) if ptype is not None else None
        except: ptype_int=None
        is_following=bool(u.get("isFollowing"))
        if ptype_int in (1,3) or ptype_int is None:
            continue
        if pid is not None and str(pid)==TARGET:
            continue # Hunt visible, not Unknown
        if ptype_int==2 and pid is None and u.get("universeId") is None:
            if is_following:
                continue # InGame hidden even when Following -> Do not show per spec
            else:
                filtered.append(u)
                continue
        continue
    # Also ensure isFollowing false (Unknown must be not following)
    unknowns=[u for u in filtered if not bool(u.get("isFollowing"))]
    # Dedupe by userId, preserve order
    seen=set(); out=[]
    for u in unknowns:
        uid=u.get("userId")
        if uid and uid not in seen:
            seen.add(uid); out.append(uid)
    return out, data

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--tracker", default="developers", help="tracker id or all")
    ap.add_argument("--json", dest="json_file", default="", help="explicit json file")
    ap.add_argument("--file", dest="file", default="", help="alias for --json")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-seconds", type=int, default=600, help="stop gracefully after this many seconds")
    args=ap.parse_args()
    json_file=args.json_file or args.file
    if not json_file:
        # auto-detect based on tracker
        mapping={
            "admin":"roblox_scan_results.json",
            "video-stars":"roblox_scan_results_video.json",
            "developers":"roblox_scan_results_developers.json",
        }
        if args.tracker in mapping:
            json_file=mapping[args.tracker]
        else:
            # all -> will be handled by caller looping, but if called with all, pick developers?
            print(f"--tracker {args.tracker} needs --json file (admin/video-stars/developers)", file=sys.stderr)
            sys.exit(2)
    if not Path(json_file).exists():
        print(f"No {json_file}, nothing to follow (0 Unknown)", file=sys.stderr)
        sys.exit(0)
    unknowns, data = load_unknowns(json_file, args.tracker)
    print(f"Tracker {args.tracker} file {json_file} -> {len(unknowns)} Unknown to follow: {unknowns[:20]}{' ...' if len(unknowns)>20 else ''}", file=sys.stderr)
    if not unknowns:
        print("No Unknowns, done")
        sys.exit(0)
    if args.dry_run:
        print(f"Dry run, would follow {unknowns}")
        sys.exit(0)

    pool=get_all_cookies()
    if not pool:
        print("No cookies found (ROBLOX_COOKIE/ROBLOSECURITY_1..5)", file=sys.stderr)
        sys.exit(1)
    print(f"Pool {len(pool)}: {[k for k,_ in pool]}", file=sys.stderr)

    # Build sessions per cookie
    sessions=[]
    for k, ck in pool:
        s=requests.Session()
        s.headers.update({
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Referer":"https://www.roblox.com/",
            "Origin":"https://www.roblox.com",
            "Content-Type":"application/json",
            "Accept":"application/json",
        })
        s.cookies.set(".ROBLOSECURITY", ck, domain=".roblox.com")
        tok=get_csrf(s)
        if tok: s.headers["x-csrf-token"]=tok
        sessions.append({"key":k, "sess":s, "follows":0, "cooldown_until":0})

    total=len(unknowns)
    idx=0
    attempts=0
    start=time.time()
    deadline=start+args.max_seconds
    followed=set(); skipped=[]
    stats=Counter()
    tries={}            # uid -> failed attempts so far
    consec_fail=0       # failed attempts in a row, across users/accounts
    consec_429=0        # 429 rate-limit answers in a row
    abort_after=2*len(pool)
    stop_reason=""
    # Round-robin, 2 follows per account then a 30s cooldown for that account
    while idx < len(unknowns):
        if time.time() >= deadline:
            stop_reason=f"time budget of {args.max_seconds}s reached"
            break
        uid=unknowns[idx]
        now=time.time()
        # Find available session
        avail=None
        earliest=None
        for sess in sessions:
            if now < sess["cooldown_until"]:
                # still cooling
                if earliest is None or sess["cooldown_until"] < earliest:
                    earliest=sess["cooldown_until"]
                continue
            # if cooldown expired, reset follows
            if sess["cooldown_until"] !=0 and now >= sess["cooldown_until"]:
                sess["follows"]=0
                sess["cooldown_until"]=0
            if sess["follows"] < 2:
                avail=sess
                break
        if avail is None:
            # all on cooldown, wait until earliest (but never past the deadline)
            wait = max(0, earliest - now) + 0.2
            if now + wait >= deadline:
                stop_reason=f"time budget of {args.max_seconds}s reached"
                break
            print(f"All {len(pool)} accounts on cooldown (2/2), waiting {wait:.1f}s until {earliest:.0f}...", file=sys.stderr)
            time.sleep(wait)
            continue
        # Attempt follow with avail
        key=avail["key"]; sess=avail["sess"]
        print(f"[{idx+1}/{total}] Follow {uid} via {key} ({avail['follows']+1}/2, cooldown {avail['cooldown_until']:.0f})...", flush=True, file=sys.stderr)
        status, code, msg = follow_one(sess, uid)
        attempts+=1
        stats[status]+=1
        if status in ("ok","already"):
            print(f"  -> {status} {code}", file=sys.stderr)
            followed.add(uid)
            consec_fail=0
            consec_429=0
            avail["follows"]+=1
            if avail["follows"] >= 2:
                avail["cooldown_until"]=time.time()+30
                print(f"  {key} reached 2/2, cooling 30s until {avail['cooldown_until']:.0f}", file=sys.stderr)
            idx+=1
            # small jitter between follows to avoid 429
            time.sleep(random.uniform(0.6,1.2))
            continue
        if status=="retry":
            wait=code
            consec_429+=1
            print(f"  -> 429 retry {wait}s {msg[:120]}", file=sys.stderr)
            if consec_429 >= abort_after:
                stop_reason=f"{consec_429} rate-limit (429) answers in a row; backing off until the next run"
                break
            # treat as cooldown for this account
            avail["cooldown_until"]=time.time()+ max(wait,30)
            avail["follows"]=0
            time.sleep(min(wait, max(0, deadline-time.time())))
            continue
        # challenge / fail / error: count against this user and against the run
        consec_fail+=1
        tries[uid]=tries.get(uid,0)+1
        if status=="challenge":
            print(f"  -> 403 challenge {msg[:120]} -> cooling {key} 30s", file=sys.stderr)
            avail["cooldown_until"]=time.time()+30
            avail["follows"]=0
            time.sleep(1)
        else:
            print(f"  -> {status} {code} {msg[:200]} -> cooling {key} 5s", file=sys.stderr)
            avail["cooldown_until"]=time.time()+5
            time.sleep(1)
        if consec_fail >= abort_after:
            stop_reason=f"{consec_fail} attempts in a row failed (last: {status} {code}); all accounts look blocked"
            break
        if tries[uid] >= MAX_TRIES_PER_USER:
            print(f"  skipping {uid} after {tries[uid]} failed attempts", file=sys.stderr)
            skipped.append(uid)
            idx+=1

    elapsed=time.time()-start
    not_followed=[u for u in unknowns if u not in followed]
    summary=(f"followed {len(followed)}/{total} in {elapsed:.0f}s | requests={attempts} "
             + " ".join(f"{k}={v}" for k,v in sorted(stats.items()))
             + f" | skipped={len(skipped)} not_attempted={len(not_followed)-len(skipped)}")
    print(f"Done: {summary}", file=sys.stderr)
    if stop_reason:
        print(f"Stopped early: {stop_reason}", file=sys.stderr)
    # Annotation (readable via the Checks API even when raw logs aren't available)
    level="notice" if not not_followed else "warning"
    print(f"::{level} title=follow {args.tracker}::{summary}" + (f" | stopped: {stop_reason}" if stop_reason else ""))
    if not_followed:
        print(f"Not followed {len(not_followed)}: {not_followed[:20]}{' ...' if len(not_followed)>20 else ''}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)

if __name__=="__main__":
    main()
