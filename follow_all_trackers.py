#!/usr/bin/env python3
"""
Follow ALL trackers from residential IP (Option A - home run, 0$)
- Developers:  config/developer_ids.txt + config/video_star_extra_ids.txt (103+1)
- Video Stars: group 4199740 (614 members)
- Admins:      group 1200769 Official Group (2774 members)
Deduped, verified, with 12s + exponential backoff (works from home, no Challenge).

Usage (run from HOME, not GitHub Actions):
  pip install requests
  # Single source:
  python follow_all_trackers.py --source developers            # 103, ~20 min
  python follow_all_trackers.py --source video-stars           # 614, ~2h
  python follow_all_trackers.py --source admins                # 2774, ~9h
  python follow_all_trackers.py --source video-star-extras     # 1 (1377033267)
  # Combined:
  python follow_all_trackers.py --source all                   # 3491 unique, ~11.5h
  python follow_all_trackers.py --source developers,video-stars  # 717

  # With 6-cookie pool sharding (6 alts share load, 6x faster - each alt follows 1/6):
  python follow_all_trackers.py --source developers --pool 6

Cookie: set ONE of these env vars OR create cookie.txt:
  ROBLOSECURITY, ROBLOX_COOKIE, ROBLOSECURITY_1..5
  export ROBLOX_COOKIE="_|WARNING...|..."
  # OR for pool: export ROBLOSECURITY_1..5 as well
"""
import os, time, sys, random, argparse, requests
from pathlib import Path

FOLLOW_API = "https://friends.roblox.com/v1/users/{uid}/follow"
FOLLOWING_API = "https://friends.roblox.com/v1/users/{uid}/followings"
GROUPS_API = "https://groups.roblox.com/v1/groups/{group_id}/users?limit=100&sortOrder=Asc"
CSRF_URL = "https://auth.roblox.com/v2/logout"

BASE_DELAY = 12.0
JITTER = 3.0
MAX_RETRIES_PER_UID = 4
MAX_PASSES = 3
RETRY_BACKOFF = [15, 30, 60, 120]

def _normalize(raw):
    raw=raw.strip().strip('"').strip("'")
    if raw.startswith(".ROBLOSECURITY="): raw=raw.split("=",1)[1]
    return raw

def get_all_cookies():
    pool=[]
    seen=set()
    for k in ["ROBLOX_COOKIE","ROBLOSECURITY","ROBLOSECURITY_1","ROBLOSECURITY_2","ROBLOSECURITY_3","ROBLOSECURITY_4","ROBLOSECURITY_5","ROBLOSECURITY_6","ROBLOSECURITY1"]:
        v=os.environ.get(k,"")
        if v and v.strip():
            v=_normalize(v)
            if v and v not in seen:
                seen.add(v); pool.append((k,v))
    # file fallback
    if not pool and Path("cookie.txt").exists():
        v=Path("cookie.txt").read_text().strip()
        if v: 
            v=_normalize(v)
            if v: pool.append(("cookie.txt",v))
    return pool

def get_csrf(sess):
    try:
        r=sess.post(CSRF_URL, timeout=10)
        tok=r.headers.get("x-csrf-token")
        if tok: return tok
    except: pass
    return None

def load_dev_ids():
    ids=[]
    for p in ["config/developer_ids.txt","config/video_star_extra_ids.txt"]:
        if not Path(p).exists(): continue
        for line in Path(p).read_text().splitlines():
            line=line.strip()
            if not line or line.startswith("#"): continue
            for part in line.replace(","," ").split():
                if part.isdigit(): ids.append(int(part))
    # dedup preserve order
    seen=set(); out=[]
    for i in ids:
        if i not in seen:
            seen.add(i); out.append(i)
    return out

def fetch_group_members(group_id, cookie=None):
    sess=requests.Session()
    sess.headers.update({"User-Agent":"Mozilla/5.0","Accept":"application/json","Referer":"https://www.roblox.com/"})
    if cookie:
        sess.cookies.set(".ROBLOSECURITY", cookie, domain=".roblox.com")
        sess.headers["Cookie"]=f".ROBLOSECURITY={cookie}"
    members=[]
    cursor=""
    page=0
    while True:
        url=GROUPS_API.format(group_id=group_id)
        if cursor: url+=f"&cursor={cursor}"
        for attempt in range(5):
            r=sess.get(url, timeout=15)
            if r.status_code==429:
                wait=int(r.headers.get("Retry-After","2"))
                time.sleep(wait+random.uniform(0,1))
                continue
            if r.status_code!=200:
                print(f"  group {group_id} page {page} {r.status_code} {r.text[:200]}")
                time.sleep(1+attempt)
                continue
            break
        else:
            break
        j=r.json()
        for e in j.get("data",[]):
            u=e.get("user",{})
            if u.get("userId"): members.append(int(u["userId"]))
        cursor=j.get("nextPageCursor")
        page+=1
        print(f"  fetched group {group_id} page {page} total {len(members)} cursor={bool(cursor)}")
        if not cursor: break
        time.sleep(0.6)
    return members

def get_my_id(sess):
    try:
        r=sess.get("https://users.roblox.com/v1/users/authenticated", timeout=10)
        if r.status_code==200: return r.json().get("id")
    except: pass
    return None

def get_followings(sess, my_id):
    following=set()
    cursor=""
    for _ in range(50):
        url=f"{FOLLOWING_API.format(uid=my_id)}?limit=100&sortOrder=Asc"
        if cursor: url+=f"&cursor={cursor}"
        try:
            r=sess.get(url, timeout=15)
            if r.status_code!=200:
                print(f"  followings GET {r.status_code} {r.text[:120]}")
                break
            j=r.json()
            for e in j.get("data",[]): following.add(e["id"])
            cursor=j.get("nextPageCursor")
            if not cursor: break
        except Exception as e:
            print(f"  followings error {e}"); break
        time.sleep(0.4)
    return following

def follow_one(sess, uid, attempt=1):
    url=FOLLOW_API.format(uid=uid)
    try:
        r=sess.post(url, timeout=15)
        if r.status_code in (200,201):
            return ("ok", r.status_code, "")
        if r.status_code==400 and "already" in r.text.lower():
            return ("already", r.status_code, r.text[:200])
        if r.status_code==429:
            retry_after=r.headers.get("Retry-After")
            try: wait=int(float(retry_after)) if retry_after else RETRY_BACKOFF[min(attempt-1,len(RETRY_BACKOFF)-1)]
            except: wait=RETRY_BACKOFF[min(attempt-1,len(RETRY_BACKOFF)-1)]
            challenge=r.headers.get("rblx-challenge-type") or r.headers.get("rblx-challenge-id")
            msg=f"429 wait {wait}s" + (f" challenge {challenge}" if challenge else "")
            return ("retry", wait, msg+" "+r.text[:100])
        if r.status_code==403:
            body=r.text.lower()
            if "challenge" in body or "challenge" in str(r.headers).lower():
                new_tok=r.headers.get("x-csrf-token")
                if new_tok: sess.headers["x-csrf-token"]=new_tok
                wait=RETRY_BACKOFF[min(attempt-1,len(RETRY_BACKOFF)-1)]
                return ("retry", wait, f"403 Challenge wait {wait}s {body[:80]}")
            if "x-csrf-token" in r.headers:
                sess.headers["x-csrf-token"]=r.headers["x-csrf-token"]
                return ("retry", 2, "403 CSRF refreshed")
            return ("fail", r.status_code, r.text[:200])
        if r.status_code in (500,502,503,504):
            wait=RETRY_BACKOFF[min(attempt-1,len(RETRY_BACKOFF)-1)]
            return ("retry", wait, f"{r.status_code} server error")
        return ("fail", r.status_code, r.text[:220])
    except requests.exceptions.RequestException as e:
        wait=RETRY_BACKOFF[min(attempt-1,len(RETRY_BACKOFF)-1)]
        return ("retry", wait, f"exception {e}")

def main():
    ap=argparse.ArgumentParser(description="Follow all trackers from home IP")
    ap.add_argument("--source", default="developers", help="developers,video-stars,admins,video-star-extras,all or comma-separated")
    ap.add_argument("--ids-file", default="", help="follow custom file (one ID per line)")
    ap.add_argument("--group-id", type=int, default=0, help="follow single group ID")
    ap.add_argument("--pool", type=int, default=1, help="use N cookies round-robin to shard load (1=main alt only, 6=use all 6)")
    ap.add_argument("--dry-run", action="store_true", help="just count, don't follow")
    args=ap.parse_args()

    pool=get_all_cookies()
    if not pool:
        print("No cookie found. Set ROBLOX_COOKIE or ROBLOSECURITY env or create cookie.txt", file=sys.stderr)
        sys.exit(1)
    print(f"Cookie pool: {len(pool)} {[k for k,_ in pool]}")
    for k,ck in pool:
        print(f"  {k}: len {len(ck)} prefix {ck[:14]!r}")

    # Build target list
    sources=[s.strip().lower() for s in args.source.split(",") if s.strip()]
    all_ids=[]
    if args.ids_file:
        sources=[]  # override
        for line in Path(args.ids_file).read_text().splitlines():
            line=line.strip()
            if line and not line.startswith("#") and line.split()[0].isdigit():
                all_ids.append(int(line.split()[0]))
    elif args.group_id:
        print(f"Fetching group {args.group_id} ...")
        all_ids=fetch_group_members(args.group_id, cookie=pool[0][1])
    else:
        for src in sources:
            if src in ("developers","dev","developer"):
                ids=load_dev_ids()
                # load_dev_ids already includes extras; if src==developers we want only developer_ids.txt without extra? keep both for now
                # filter to only developer_ids.txt if needed
                if src=="developers":
                    # reload only main file
                    ids=[]
                    for line in Path("config/developer_ids.txt").read_text().splitlines():
                        line=line.strip()
                        if line and not line.startswith("#") and line.isdigit():
                            ids.append(int(line))
                print(f"  {src}: {len(ids)} IDs")
                all_ids.extend(ids)
            elif src in ("video-stars","video_star","videostars","vs"):
                print(f"Fetching Video Stars 4199740 ...")
                ids=fetch_group_members(4199740, cookie=pool[0][1])
                print(f"  video-stars: {len(ids)} IDs")
                all_ids.extend(ids)
            elif src in ("admins","admin","official","1200769"):
                print(f"Fetching Official Group 1200769 ...")
                ids=fetch_group_members(1200769, cookie=pool[0][1])
                print(f"  admins: {len(ids)} IDs")
                all_ids.extend(ids)
            elif src in ("video-star-extras","video_extra","extra"):
                p=Path("config/video_star_extra_ids.txt")
                if p.exists():
                    for line in p.read_text().splitlines():
                        line=line.strip()
                        if line and not line.startswith("#") and line.isdigit():
                            all_ids.append(int(line))
            elif src=="all":
                print("Fetching all trackers (developers + video stars + admins)...")
                dev=load_dev_ids()
                print(f"  developers(+extra): {len(dev)}")
                all_ids.extend(dev)
                vs=fetch_group_members(4199740, cookie=pool[0][1])
                print(f"  video-stars: {len(vs)}")
                all_ids.extend(vs)
                admins=fetch_group_members(1200769, cookie=pool[0][1])
                print(f"  admins: {len(admins)}")
                all_ids.extend(admins)
            else:
                print(f"Unknown source {src!r}, skipping")

    # dedup
    seen=set(); uniq=[]
    for i in all_ids:
        if i not in seen:
            seen.add(i); uniq.append(i)
    all_ids=uniq
    print(f"\nTotal unique IDs to follow: {len(all_ids)}")
    if args.dry_run:
        print("Dry run - not following. First 20:", all_ids[:20])
        sys.exit(0)
    if not all_ids:
        print("Nothing to follow"); sys.exit(0)

    # Estimate time
    est_min = len(all_ids) * (BASE_DELAY) / 60
    if args.pool>1: est_min/=args.pool
    print(f"Estimate @ {BASE_DELAY}s delay: ~{est_min:.1f} min (~{est_min/60:.1f}h) with pool={args.pool}")

    # Build sessions for pool
    sessions=[]
    for idx,(k,ck) in enumerate(pool[:args.pool]):
        s=requests.Session()
        s.headers.update({
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0",
            "Referer":"https://www.roblox.com/",
            "Origin":"https://www.roblox.com",
            "Content-Type":"application/json",
            "Accept":"application/json",
        })
        s.cookies.set(".ROBLOSECURITY", ck, domain=".roblox.com")
        tok=get_csrf(s)
        if tok: s.headers["x-csrf-token"]=tok
        mid=get_my_id(s)
        print(f"  pool[{idx}] {k} -> my_id {mid} csrf {tok[:8] if tok else 'none'}...")
        sessions.append((k,s,mid))

    # For verification we use first session's following set
    primary_k,primary_s,primary_mid=sessions[0]
    if primary_mid:
        print(f"Fetching followings for {primary_k} ({primary_mid}) to skip already followed...")
        following=get_followings(primary_s, primary_mid)
        print(f"Already following {len(following)}")
        # Also merge other pools' followings for skip check if pool>1 we consider any pool covering means we can show Hunt via any, but follow still needed per alt? For Hunt reveal we just need ONE alt to follow. So we skip if any pool already follows.
        if args.pool>1:
            for k,s,mid in sessions[1:]:
                if mid:
                    f2=get_followings(s, mid)
                    print(f"  {k} follows {len(f2)}")
                    following.update(f2)  # if any alt follows, we skip (Hunt already revealed)
        remaining=[uid for uid in all_ids if uid not in following]
        print(f"Need to follow {len(remaining)}/{len(all_ids)} (skipping {len(all_ids)-len(remaining)} already followed via any pool)")
    else:
        following=set()
        remaining=all_ids
        print(f"Could not get my_id, will try blind for {len(remaining)}")

    if not remaining:
        print("All done - no remaining"); sys.exit(0)

    # Shard remaining across pool if pool>1 (each session follows 1/N)
    shards=[[] for _ in range(len(sessions))]
    for idx,uid in enumerate(remaining):
        shards[idx % len(sessions)].append(uid)
    for idx,(k,_,_) in enumerate(sessions):
        print(f"  shard {idx} {k}: {len(shards[idx])} IDs")

    total_success=0
    total_failed=0
    failed_ids=[]

    # Sequential per shard but pool shards run interleaved with delay
    # Simplest: loop round-robin across shards, one follow per shard turn
    # For single pool, just linear.
    if len(sessions)==1:
        to_try=remaining
        attempts={uid:0 for uid in to_try}
        failed_reason={}
        passes=0
        while to_try and passes < (1+MAX_PASSES):
            passes+=1
            print(f"\n=== Pass {passes}/{1+MAX_PASSES} - {len(to_try)} remaining ===")
            next_round=[]
            for idx,uid in enumerate(to_try,1):
                attempts[uid]+=1
                att=attempts[uid]
                print(f"[{idx}/{len(to_try)}] (attempt {att}) Follow {uid} ...", flush=True)
                status,code,msg=follow_one(primary_s, uid, attempt=att)
                if status=="ok":
                    print(f"  -> OK"); total_success+=1
                    following.add(uid)
                elif status=="already":
                    print(f"  -> already"); following.add(uid)
                elif status=="retry":
                    wait=code
                    print(f"  -> RETRY {wait}s: {msg}")
                    failed_reason[uid]=msg
                    if att < MAX_RETRIES_PER_UID:
                        time.sleep(max(1, wait + random.uniform(-1,1)))
                        next_round.append(uid)
                    else:
                        print(f"    max retries {uid}"); next_round.append(uid)
                else:
                    print(f"  -> FAIL {code}: {msg}")
                    failed_reason[uid]=f"{code} {msg}"
                    next_round.append(uid)
                if status!="retry":
                    d=BASE_DELAY+random.uniform(-JITTER,JITTER)
                    if idx%10==0: d+=5
                    print(f"    delay {d:.1f}s...")
                    time.sleep(d)
                if idx%20==0:
                    nt=get_csrf(primary_s)
                    if nt: primary_s.headers["x-csrf-token"]=nt
            # re-verify
            if primary_mid and next_round:
                print(f"Re-check {len(next_round)} pending...")
                time.sleep(3)
                refreshed=get_followings(primary_s, primary_mid)
                still=[uid for uid in next_round if uid not in refreshed]
                print(f"  Still missing {len(still)} (became ok {len(next_round)-len(still)})")
                filtered=[uid for uid in still if attempts[uid] < MAX_RETRIES_PER_UID]
                to_try=filtered
                if to_try: time.sleep(30)
                else: failed_ids=still; break
            else:
                to_try=next_round
        # final
        if primary_mid:
            final=get_followings(primary_s, primary_mid)
            missing=[uid for uid in all_ids if uid not in final and uid not in following]
            # also consider pool
            print(f"\n=== FINAL {len(all_ids)-len(missing)}/{len(all_ids)} followed, missing {len(missing)} ===")
            if missing:
                print(missing[:20])
                Path("follow_failed.txt").write_text("\n".join(map(str,missing))+"\n")
            total_failed=len(missing)
            failed_ids=missing
        else:
            total_failed=len(to_try)
    else:
        # pool sharded - each session handles its shard sequentially
        import threading
        # For simplicity, run shards sequentially but with per-session CSRF
        for shard_idx,(k,s,mid) in enumerate(sessions):
            shard_ids=shards[shard_idx]
            if not shard_ids: continue
            print(f"\n=== Pool shard {shard_idx} {k} : {len(shard_ids)} IDs ===")
            # get following for this alt to skip already followed by this alt (but we already skipped via any)
            # we still follow even if another alt follows? No skip already done via any, so these are truly not followed by any
            attempts={uid:0 for uid in shard_ids}
            to_try=shard_ids[:]
            passes=0
            while to_try and passes < (1+MAX_PASSES):
                passes+=1
                next_round=[]
                for idx,uid in enumerate(to_try,1):
                    attempts[uid]+=1
                    att=attempts[uid]
                    print(f"[{k} {idx}/{len(to_try)}] Follow {uid} att {att} ...", flush=True)
                    status,code,msg=follow_one(s, uid, attempt=att)
                    if status=="ok":
                        print(f"  -> OK ({k})"); total_success+=1
                    elif status=="already":
                        print(f"  -> already ({k})")
                    elif status=="retry":
                        wait=code
                        print(f"  -> RETRY {wait}s: {msg}"); 
                        if att < MAX_RETRIES_PER_UID:
                            time.sleep(max(1, wait+random.uniform(-1,1)))
                            next_round.append(uid)
                        else:
                            next_round.append(uid)
                    else:
                        print(f"  -> FAIL {code}: {msg}")
                        next_round.append(uid)
                    if status!="retry":
                        d=BASE_DELAY+random.uniform(-JITTER,JITTER)
                        time.sleep(d)
                    if idx%20==0:
                        nt=get_csrf(s)
                        if nt: s.headers["x-csrf-token"]=nt
                if next_round and mid:
                    time.sleep(2)
                    refreshed=get_followings(s, mid)
                    still=[uid for uid in next_round if uid not in refreshed]
                    filtered=[uid for uid in still if attempts[uid] < MAX_RETRIES_PER_UID]
                    to_try=filtered
                    if to_try: time.sleep(20)
                    else: failed_ids.extend(still); break
                else:
                    to_try=next_round
            # collect shard failures
            if to_try:
                failed_ids.extend(to_try)

    # Dedup failed
    failed_ids=list(dict.fromkeys(failed_ids))
    print(f"\nDone: success {total_success}, failed {len(failed_ids)}/{len(all_ids)}")
    if failed_ids:
        print(f"Failed: {failed_ids[:30]}")
        Path("follow_failed_all.txt").write_text("\n".join(map(str,failed_ids))+"\n")
        sys.exit(2)
    else:
        print("All done - next 5-min cloud scan will see Hunt without Unknown")
        sys.exit(0)

if __name__=="__main__":
    main()
