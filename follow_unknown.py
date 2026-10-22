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
    # Keep aged/verified accounts first (ROBLOX_COOKIE, ROBLOSECURITY, ROBLOSECURITY_1..20): newer unverified
    # accounts receive '403 Challenge is required' from datacenter IPs while consuming the runner IP's
    # ~2 follows / 60s sliding window before verified accounts get a chance.
    for k in ["ROBLOX_COOKIE", "ROBLOSECURITY"] + [f"ROBLOSECURITY_{i}" for i in range(1, 21)]:
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
    try:
        import re
        rh=sess.get("https://www.roblox.com/home", timeout=10)
        if rh.status_code==200:
            m=re.search(r'<meta\s+name="csrf-token"\s+[^>]*data-token="([^"]+)"', rh.text) or re.search(r'<meta\s+name="csrf-token"\s+[^>]*content="([^"]+)"', rh.text)
            if m: return m.group(1)
    except: pass
    return None

def follow_one(sess, uid):
    url=FOLLOW_API.format(uid=uid)
    for csrf_try in range(2):
        try:
            r=sess.post(url, timeout=15)
            if r.status_code in (200,201):
                return ("ok", r.status_code, "")
            if r.status_code==400 and "already" in r.text.lower():
                return ("already", r.status_code, r.text[:200])
            if r.status_code==429:
                retry_after=r.headers.get("Retry-After")
                try: wait=int(float(retry_after)) if retry_after else 62
                except: wait=62
                # friends.roblox.com enforces ~2 POST /follow per ~60s per runner IP while returning Retry-After: 5;
                # waiting 62s lets the full 60s IP window refill instead of burning consecutive retries.
                return ("retry", max(wait, 62), f"429 {r.text[:120]}")
            if r.status_code==403:
                body=r.text.lower()
                if "x-csrf-token" in r.headers and csrf_try == 0 and "challenge" not in body:
                    sess.headers["x-csrf-token"]=r.headers["x-csrf-token"]
                    continue
                if "challenge" in body:
                    return ("challenge", r.status_code, r.text[:200])
                return ("fail", r.status_code, r.text[:200])
            return ("fail", r.status_code, r.text[:300])
        except Exception as e:
            return ("error", 0, str(e))
    return ("fail", 403, "csrf retry exhausted")

def load_unknowns(json_path, tracker=None):
    data=json.loads(Path(json_path).read_text(encoding="utf-8"))
    target=data.get("target_game_players",[])
    other=data.get("other_game_players",[])
    meta=data.get("meta") or {}
    any_game=bool(meta.get("anyGame", False))
    target_place=str(meta.get("targetPlaceId") or "116463962153167")
    raw_univ=meta.get("targetUniverseId")
    try:
        target_universe=int(raw_univ) if raw_univ is not None else (10770728771 if target_place=="116463962153167" else None)
    except (TypeError, ValueError):
        target_universe=10770728771 if target_place=="116463962153167" else None

    filtered=[]
    for u in target+other:
        pid=u.get("placeId") or u.get("rootPlaceId")
        ptype=u.get("presenceType")
        try: ptype_int=int(ptype) if ptype is not None else None
        except: ptype_int=None
        is_following=bool(u.get("isFollowing"))
        if ptype_int in (1,3) or ptype_int is None:
            continue
        if pid is not None:
            if any_game or str(u.get("placeId"))==target_place or str(u.get("rootPlaceId"))==target_place:
                continue  # target game already visible, not Unknown
            if target_universe is not None and u.get("universeId") is not None:
                try:
                    if int(u.get("universeId"))==target_universe:
                        continue
                except (TypeError, ValueError):
                    pass
        if ptype_int==2 and pid is None and u.get("universeId") is None:
            if is_following:
                continue  # InGame hidden even when Following -> Do not show per spec
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
        sessions.append({"key":k, "sess":s, "follows":0, "cooldown_until":0, "dead":False})

    total=len(unknowns)
    idx=0
    rr_idx=0
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
    # True round-robin across non-challenged accounts, 2 follows per account then a 30s cooldown
    while idx < len(unknowns):
        if time.time() >= deadline:
            stop_reason=f"time budget of {args.max_seconds}s reached"
            break
        uid=unknowns[idx]
        now=time.time()
        live_sessions=[s for s in sessions if not s["dead"]]
        if not live_sessions:
            stop_reason="all accounts hit challenge/auth block this run"
            break
        avail=None
        earliest=None
        for step in range(len(sessions)):
            cand_i = (rr_idx + step) % len(sessions)
            sess = sessions[cand_i]
            if sess["dead"]:
                continue
            if now < sess["cooldown_until"]:
                if earliest is None or sess["cooldown_until"] < earliest:
                    earliest=sess["cooldown_until"]
                continue
            if sess["cooldown_until"] != 0 and now >= sess["cooldown_until"]:
                sess["follows"]=0
                sess["cooldown_until"]=0
            if sess["follows"] < 2:
                avail=sess
                rr_idx = cand_i
                break
        if avail is None:
            wait = max(0, (earliest or now) - now) + 0.2
            if now + wait >= deadline:
                stop_reason=f"time budget of {args.max_seconds}s reached"
                break
            print(f"All {len(live_sessions)} active accounts on cooldown (2/2), waiting {wait:.1f}s until {earliest:.0f}...", file=sys.stderr)
            time.sleep(wait)
            continue
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
                rr_idx = (rr_idx + 1) % len(sessions)
                print(f"  {key} reached 2/2, cooling 30s until {avail['cooldown_until']:.0f}", file=sys.stderr)
            idx+=1
            time.sleep(random.uniform(0.6,1.2))
            continue
        if status=="retry":
            wait=code
            consec_429+=1
            print(f"  -> 429 retry {wait}s {msg[:120]}", file=sys.stderr)
            if consec_429 >= abort_after:
                stop_reason=f"{consec_429} rate-limit (429) answers in a row; backing off until the next run"
                break
            avail["cooldown_until"]=time.time()+ max(wait,30)
            avail["follows"]=0
            rr_idx = (rr_idx + 1) % len(sessions)
            if time.time() + wait >= deadline:
                stop_reason=f"time budget of {args.max_seconds}s reached while waiting for 60s follow window"
                break
            time.sleep(wait)
            continue
        # challenge / fail / error
        consec_fail+=1
        if status=="challenge":
            # Retire challenged account for the rest of this run (don't count against uid or loop back to it!)
            print(f"  -> 403 challenge on {key} -> retiring {key} for this run and trying next account", file=sys.stderr)
            avail["dead"]=True
            rr_idx = (rr_idx + 1) % len(sessions)
            time.sleep(0.8)
            continue
        tries[uid]=tries.get(uid,0)+1
        print(f"  -> {status} {code} {msg[:200]} -> cooling {key} 5s", file=sys.stderr)
        avail["cooldown_until"]=time.time()+5
        rr_idx = (rr_idx + 1) % len(sessions)
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
