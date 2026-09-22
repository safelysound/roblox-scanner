#!/usr/bin/env python3
"""
Universal Tracker Scanner — single scanner for all trackers (Admin, Video Stars, Developers).
Replaces roblox_group_scanner.py + developer_scanner.py.
Reads config/trackers.yaml (id, groupId, idsFile, extraIds) + supports sharding + 6-cookie pool.

Usage:
  python tracker_scanner.py --tracker admin --shard 0 --shards 3 --json shard_0.json --verbose
  python tracker_scanner.py --tracker developers --place-id 74205509034203 --json out.json
  python tracker_scanner.py --group-id 1200769 --place-id 74205509034203 --json out.json
  python tracker_scanner.py --ids-file config/developer_ids.txt --json out.json
"""
import argparse, json, csv, time, sys, os
from pathlib import Path
from typing import List, Dict, Any, Optional
import requests

# Try yaml, fallback to manual parse
try:
    import yaml
    HAS_YAML=True
except:
    HAS_YAML=False

# Import reused helpers from developer_scanner (kept for maintenance simplicity)
# We dynamically import to avoid duplication; fallback to local definitions if import fails
try:
    import developer_scanner as _dev
    _get_all_cookies = _dev._get_all_cookies
    _log_cookie_status = _dev._log_cookie_status
    _get_my_id = _dev._get_my_id
    _seeded_session = _dev._seeded_session
    _get_csrf_from_session = _dev._get_csrf_from_session
    _profile_shows_hunt = _dev._profile_shows_hunt
    fetch_user_info = _dev.fetch_user_info
    fetch_presences = _dev.fetch_presences
    _get_followings = _dev._get_followings
    _cookie_headers = _dev._cookie_headers
    resolve_universe_id = _dev.resolve_universe_id if hasattr(_dev, 'resolve_universe_id') else lambda pid: 10766456501
    resolve_game_name = _dev.resolve_game_name if hasattr(_dev, 'resolve_game_name') else lambda uid: "The Hunt: Roblox 20"
    HAS_DEV=True
except Exception as e:
    print(f"Failed to import developer_scanner: {e}, using fallback", file=sys.stderr)
    HAS_DEV=False
    # Fallback minimal definitions (should not happen if file exists)
    def _get_all_cookies():
        pool=[]; seen=set()
        for k in ["ROBLOX_COOKIE","ROBLOSECURITY","ROBLOSECURITY_1","ROBLOSECURITY_2","ROBLOSECURITY_3","ROBLOSECURITY_4","ROBLOSECURITY_5"]:
            v=os.environ.get(k,"")
            if v and v.strip():
                v=v.strip().strip('"').strip("'")
                if v.startswith(".ROBLOSECURITY="): v=v.split("=",1)[1]
                if v and v not in seen:
                    seen.add(v); pool.append((k,v))
        return pool
    def _log_cookie_status(): return False
    def _get_my_id(h): return None
    def _seeded_session(ck):
        import requests as _rq
        s=_rq.Session(); s.headers.update({"User-Agent":"Mozilla/5.0","Accept":"text/html"})
        if ck: s.headers.update({"Cookie": ck})
        try: s.get("https://www.roblox.com/home", timeout=10)
        except: pass
        return s
    def fetch_user_info(uid):
        try:
            r=requests.get(f"https://users.roblox.com/v1/users/{uid}", timeout=10, headers={"User-Agent":"Mozilla/5.0"})
            if r.status_code==200:
                j=r.json(); return {"userId":j.get("id"),"username":j.get("name"),"displayName":j.get("displayName"),"hasVerifiedBadge":j.get("hasVerifiedBadge",False)}
        except: pass
        return {"userId":uid,"username":f"user{uid}","displayName":f"user{uid}","hasVerifiedBadge":False}
    def fetch_presences(uids):
        return {}
    def _get_followings(mid, hdr): return set()
    def _cookie_headers(): return {"User-Agent":"Mozilla/5.0","Content-Type":"application/json"}
    def _profile_shows_hunt(uid, hdr): return False
    def resolve_universe_id(pid): return 10766456501
    def resolve_game_name(uid): return "The Hunt: Roblox 20"

DEFAULT_PLACE_ID = 74205509034203
DEFAULT_UNIVERSE_ID = 10766456501
GROUP_API = "https://groups.roblox.com/v1/groups/{group_id}/users"
GROUP_PAGE_LIMIT = 100

def _request_with_backoff(session, method, url, max_retries=5, **kwargs):
    import random
    for attempt in range(1, max_retries+1):
        try:
            resp = session.request(method, url, timeout=15, **kwargs)
            if resp.status_code == 429:
                retry = resp.headers.get("Retry-After")
                try:
                    wait = int(float(retry)) if retry else (2 ** attempt)
                except:
                    wait = 2 ** attempt
                time.sleep(wait + random.uniform(0,1))
                continue
            return resp
        except:
            if attempt == max_retries:
                return None
            time.sleep(1+attempt)
    return None

def fetch_group_members(group_id, max_members=None, delay=0.6, verbose=False):
    pool = _get_all_cookies()
    sess = requests.Session()
    sess.headers.update({"User-Agent":"Mozilla/5.0","Accept":"application/json","Referer":"https://www.roblox.com/"})
    if pool:
        ck = pool[0][1]
        sess.headers.update({"Cookie": f".ROBLOSECURITY={ck}"})
        sess.cookies.set(".ROBLOSECURITY", ck, domain=".roblox.com")
    members=[]
    cursor=""
    page=0
    # memberCount for logging
    try:
        r = _request_with_backoff(sess, "GET", f"https://groups.roblox.com/v1/groups/{group_id}")
        if r and r.status_code==200 and verbose:
            print(f"Group {group_id} reports {r.json().get('memberCount')} members", file=sys.stderr)
    except:
        pass
    while True:
        page+=1
        url = GROUP_API.format(group_id=group_id)
        params={"limit": GROUP_PAGE_LIMIT, "sortOrder":"Asc"}
        if cursor:
            params["cursor"]=cursor
        r = _request_with_backoff(sess, "GET", url, params=params)
        if not r or r.status_code!=200:
            print(f"Group {group_id} page {page} failed {r.status_code if r else 'no resp'}", file=sys.stderr)
            break
        j=r.json()
        batch=j.get("data",[])
        if not batch:
            break
        for entry in batch:
            user=entry.get("user",{})
            role=entry.get("role",{})
            members.append({
                "userId": user.get("userId"),
                "username": user.get("username"),
                "displayName": user.get("displayName"),
                "hasVerifiedBadge": user.get("hasVerifiedBadge", False),
                "role": role.get("name"),
                "rank": role.get("rank"),
                "roleId": role.get("id"),
            })
            if max_members and len(members) >= max_members:
                return members[:max_members]
        if verbose:
            print(f"  Page {page}: +{len(batch)} (total {len(members)})", file=sys.stderr)
        cursor=j.get("nextPageCursor")
        if not cursor:
            break
        time.sleep(delay + __import__("random").uniform(-0.1,0.1))
    if verbose:
        print(f"Fetched {len(members)} members for group {group_id}", file=sys.stderr)
    return members

def load_tracker_config(tracker_id):
    p = Path("config/trackers.yaml")
    if not p.exists():
        p = Path("config/trackers.json")
        if p.exists():
            data=json.loads(p.read_text())
            for t in data.get("trackers",[]):
                if t.get("id")==tracker_id:
                    return t
            print(f"Tracker {tracker_id} not found in {p}", file=sys.stderr)
            return None
    txt=p.read_text()
    data=None
    if HAS_YAML:
        try:
            data=yaml.safe_load(txt)
        except Exception as e:
            print(f"yaml load failed {e}", file=sys.stderr)
            data=None
    if data is None:
        # manual minimal parse
        try:
            blocks=txt.split("- id:")
            for block in blocks[1:]:
                first_line=block.strip().splitlines()[0]
                bid=first_line.strip().split()[0]
                if bid==tracker_id:
                    t={"id": bid}
                    for line in block.splitlines()[1:]:
                        line=line.strip()
                        if not line or line.startswith("#"): continue
                        if line.startswith("-") or "trackers:" in line or "settings:" in line:
                            continue
                        if ":" in line:
                            k,v=line.split(":",1)
                            k=k.strip(); v=v.strip().strip('"').strip("'")
                            if v.startswith("[") and v.endswith("]"):
                                inner=v[1:-1].strip()
                                if inner:
                                    t["extraIds"]=[int(x.strip()) for x in inner.split(",") if x.strip().isdigit()]
                                else:
                                    t["extraIds"]=[]
                            elif v.isdigit():
                                t[k]=int(v)
                            elif k=="extraIds":
                                pass
                            else:
                                if "#" in v:
                                    v=v.split("#")[0].strip().strip('"').strip("'")
                                t[k]=v
                    return t
        except Exception as e2:
            print(f"manual parse failed {e2}", file=sys.stderr)
        print(f"Tracker {tracker_id} not found (install pyyaml: pip install pyyaml)", file=sys.stderr)
        return None
    for t in data.get("trackers",[]):
        if t.get("id")==tracker_id:
            return t
    print(f"Tracker {tracker_id} not found in config/trackers.yaml", file=sys.stderr)
    return None

def parse_args():
    p=argparse.ArgumentParser(description="Universal Tracker Scanner (group + ID list, sharded)")
    p.add_argument("--tracker", type=str, default="", help="Tracker id from config/trackers.yaml (admin, video-stars, developers)")
    p.add_argument("--group-id", type=int, default=None, help="Group ID (overrides tracker)")
    p.add_argument("--user-ids-file", type=str, default="", help="IDs file (overrides tracker)")
    p.add_argument("--extra-ids", type=str, default="", help="Comma-separated extra IDs")
    p.add_argument("--extra-ids-file", type=str, default="", help="Extra IDs file")
    p.add_argument("--place-id", type=int, default=DEFAULT_PLACE_ID, help="Target PlaceId")
    p.add_argument("--universe-id", type=int, default=None, help="Target UniverseId")
    p.add_argument("--json", type=str, default="", help="Output JSON path")
    p.add_argument("--csv", type=str, default="", help="Output CSV path")
    p.add_argument("--shard", type=int, default=None, help="Shard index")
    p.add_argument("--shards", type=int, default=None, help="Total shards")
    p.add_argument("--max-members", type=int, default=None, help="Limit members for testing")
    p.add_argument("--verbose", action="store_true", help="Verbose")
    p.add_argument("--user-ids", type=str, default="", help=argparse.SUPPRESS)
    return p.parse_args()

def resolve_tracker(args):
    group_id = args.group_id
    ids_file = args.user_ids_file
    extra_ids = []
    place_id = args.place_id
    if args.extra_ids:
        for part in args.extra_ids.replace(",", " ").split():
            if part.isdigit():
                extra_ids.append(int(part))
    if args.extra_ids_file and Path(args.extra_ids_file).exists():
        for line in Path(args.extra_ids_file).read_text().splitlines():
            line=line.strip()
            if line and not line.startswith("#") and line.isdigit():
                extra_ids.append(int(line))
    if args.tracker:
        cfg = load_tracker_config(args.tracker)
        if not cfg:
            print(f"Failed to load tracker {args.tracker}", file=sys.stderr)
            sys.exit(1)
        if group_id is None and "groupId" in cfg:
            group_id = int(cfg["groupId"])
        if not ids_file and cfg.get("idsFile"):
            ids_file = cfg["idsFile"]
        if cfg.get("extraIds"):
            for eid in cfg["extraIds"]:
                try: extra_ids.append(int(eid))
                except: pass
        if cfg.get("extraIdsFile") and Path(cfg["extraIdsFile"]).exists():
            for line in Path(cfg["extraIdsFile"]).read_text().splitlines():
                line=line.strip()
                if line and not line.startswith("#") and line.isdigit():
                    extra_ids.append(int(line))
    return group_id, ids_file, extra_ids, place_id

def tracker_main_logic():
    args = parse_args()
    group_id, ids_file, extra_ids, place_id = resolve_tracker(args)
    verbose = args.verbose
    all_members=[]
    group_members=[]
    if group_id is not None:
        if verbose:
            print(f"Tracker {args.tracker or group_id}: fetching group {group_id}", file=sys.stderr)
        group_members = fetch_group_members(group_id, max_members=args.max_members, delay=0.6, verbose=verbose)
        all_members.extend(group_members)
    file_ids=[]
    if ids_file and Path(ids_file).exists():
        try:
            # try developer helper
            if HAS_DEV and hasattr(_dev, 'load_user_ids'):
                file_ids = _dev.load_user_ids(ids_file)
            elif HAS_DEV and hasattr(_dev, 'load_ids'):
                file_ids = _dev.load_ids(ids_file)
            else:
                raise Exception("no loader")
        except:
            file_ids=[]
            for line in Path(ids_file).read_text().splitlines():
                line=line.strip()
                if not line or line.startswith("#"): continue
                for part in line.replace(",", " ").split():
                    if part.isdigit():
                        file_ids.append(int(part))
            seen=set(); uniq=[]
            for i in file_ids:
                if i not in seen:
                    seen.add(i); uniq.append(i)
            file_ids=uniq
        if args.max_members and len(file_ids) > args.max_members:
            file_ids=file_ids[:args.max_members]
        if verbose:
            print(f"IDS file {ids_file}: {len(file_ids)} IDs", file=sys.stderr)
        for uid in file_ids:
            if any(m["userId"]==uid for m in all_members):
                continue
            info = fetch_user_info(uid)
            info.update({"role": "Developer", "rank": 255, "roleId": 0})
            all_members.append(info)
            time.sleep(0.05)
    if extra_ids:
        if verbose:
            print(f"Extra IDs: {extra_ids}", file=sys.stderr)
        for uid in extra_ids:
            if any(m["userId"]==uid for m in all_members):
                continue
            info = fetch_user_info(uid)
            info.update({"role": "Developer", "rank": 255, "roleId": 0})
            all_members.append(info)
            time.sleep(0.05)
    if not all_members:
        print("No members/IDs found for tracker", file=sys.stderr)
        sys.exit(1)
    # dedup
    seen=set(); uniq_members=[]
    for m in all_members:
        uid=m.get("userId")
        if uid not in seen:
            seen.add(uid); uniq_members.append(m)
    all_members=uniq_members
    print(f"Tracker {args.tracker or 'custom'}: total unique {len(all_members)} (group {len(group_members)} + file {len(file_ids)} + extra {len(extra_ids)})", file=sys.stderr)
    # sharding
    is_sharded = args.shard is not None and args.shards is not None
    if is_sharded:
        total=len(all_members)
        chunk=(total + args.shards - 1)//args.shards
        start=args.shard*chunk
        end=min(start+chunk, total)
        members_slice=all_members[start:end]
        print(f"Shard {args.shard}/{args.shards}: {len(members_slice)}/{total} (indices {start}-{end-1})", file=sys.stderr)
        members_for_presence=members_slice
    else:
        members_for_presence=all_members
    # resolve universe
    universe=args.universe_id or (resolve_universe_id(place_id) if HAS_DEV else DEFAULT_UNIVERSE_ID) or DEFAULT_UNIVERSE_ID
    game_name=resolve_game_name(universe) if HAS_DEV else "The Hunt: Roblox 20"
    if game_name and verbose:
        print(f'Target game: "{game_name}" Universe {universe}', file=sys.stderr)
    ids_for_presence=[m["userId"] for m in members_for_presence]
    # log cookie status (like dev does)
    if HAS_DEV:
        try: _log_cookie_status()
        except: pass
    presences=fetch_presences(ids_for_presence)
    print(f"Got {len(presences)}/{len(ids_for_presence)} presence responses", file=sys.stderr)
    # isFollowing pool
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
    if not pool:
        my_id=_get_my_id(headers_for_follow)
        if my_id:
            my_ids.append(my_id)
            followings=_get_followings(my_id, headers_for_follow)
    if my_ids:
        print(f"Pool {len(pool)} accounts my_ids {my_ids} total unique followings {len(followings)} for isFollowing", file=sys.stderr)
        my_id=my_ids[0]
    else:
        my_id=None
        print("Pool no my_id - unauthenticated", file=sys.stderr)
    target=[]; other=[]; offline=[]
    for m in members_for_presence:
        uid=m["userId"]
        pres=presences.get(uid)
        if not pres:
            offline.append({**m, "presenceType": 0, "presenceTypeName":"Offline", "placeId": None, "rootPlaceId": None, "universeId": None, "lastLocation": "", "isFollowing": False, "presence_raw": None})
            continue
        ptype=pres.get("userPresenceType",0)
        place_id_p=pres.get("placeId")
        root_place=pres.get("rootPlaceId")
        univ=pres.get("universeId")
        last_loc=pres.get("lastLocation","")
        is_in_game=(ptype==2)
        is_target=False
        if is_in_game:
            if universe and univ==universe:
                is_target=True
            elif place_id_p==place_id or root_place==place_id:
                is_target=True
        is_following = uid in followings if followings else False
        if not is_following and my_id and len(followings)==0 and ptype==2 and place_id_p is None:
            try:
                ck2=headers_for_follow.get("Cookie","")
                h2={"User-Agent":"Mozilla/5.0","Referer":"https://www.roblox.com/","Accept":"application/json","Cookie": ck2}
                sess2=_seeded_session(ck2) if ck2 else requests.Session()
                r=sess2.get(f"https://friends.roblox.com/v1/users/{my_id}/followings?limit=100&sortOrder=Asc", timeout=10, headers=h2)
                if r.status_code==200 and any(e.get("id")==uid for e in r.json().get("data",[])):
                    is_following=True
                    print(f"per-user isFollowing true for {uid}", file=sys.stderr)
            except: pass
        if ptype==2 and place_id_p is None and root_place is None and univ is None:
            if _profile_shows_hunt(uid, headers_for_follow):
                print(f"profile scrape: {uid} shows Hunt via profile", file=sys.stderr)
                place_id_p=74205509034203
                root_place=74205509034203
                univ=10766456501
                last_loc="The Hunt: Roblox 20"
                is_following=True
                is_target=True
                is_in_game=True
        enriched={**m, "presenceType": ptype, "presenceTypeName": {0:"Offline",1:"Online",2:"InGame",3:"InStudio"}.get(ptype,str(ptype)), "lastLocation": last_loc, "placeId": place_id_p, "rootPlaceId": root_place, "universeId": univ, "gameId": pres.get("gameId"), "lastOnline": pres.get("lastOnline"), "isFollowing": is_following, "presence_raw": pres}
        if is_target:
            target.append(enriched)
        elif is_in_game:
            other.append(enriched)
        else:
            offline.append(enriched)
    target.sort(key=lambda x: (x["username"] or "").lower())
    other.sort(key=lambda x: ((x["lastLocation"] or ""), (x["username"] or "").lower()))
    total_members=len(members_for_presence)
    print(f"Target Hunt: {len(target)} | Other: {len(other)} | Offline: {len(offline)} | Total slice {total_members}", file=sys.stderr)
    meta_group = group_id if group_id is not None else (f"tracker:{args.tracker}" if args.tracker else "developer_list")
    group_url = f"https://www.roblox.com/communities/{group_id}/" if group_id else ""
    meta={
        "groupId": meta_group,
        "groupUrl": group_url,
        "targetPlaceId": place_id,
        "targetUniverseId": universe,
        "targetGameName": game_name,
        "targetGameUrl": f"https://www.roblox.com/games/{place_id}/",
        "total_members": total_members,
        "full_group_size": len(all_members),
        "elapsed_seconds": 0,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "settings": {"shard": args.shard, "shards": args.shards, "source": args.tracker or "custom", "groupId": group_id, "idsFile": ids_file},
        "shard_metas": [],
    }
    if is_sharded:
        meta["shard_info"]=f"{args.shard}/{args.shards} members {len(members_for_presence)}/{len(all_members)}"
    out={
        "meta": meta,
        "summary": {"total_members": total_members, "target_game_count": len(target), "other_game_count": len(other), "offline_count": len(offline)},
        "target_game_players": target,
        "other_game_players": other,
        "offline_or_website": offline,
    }
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"JSON exported to {args.json}", file=sys.stderr)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w=csv.DictWriter(f, fieldnames=["priority","username","displayName","userId","role","rank","presenceType","presenceTypeName","lastLocation","placeId","rootPlaceId","universeId","profileUrl"])
            w.writeheader()
            for u in target:
                w.writerow({"priority":"TARGET_GAME","username":u.get("username"),"displayName":u.get("displayName"),"userId":u.get("userId"),"role":u.get("role"),"rank":u.get("rank"),"presenceType":u.get("presenceType"),"presenceTypeName":u.get("presenceTypeName"),"lastLocation":u.get("lastLocation"),"placeId":u.get("placeId"),"rootPlaceId":u.get("rootPlaceId"),"universeId":u.get("universeId"),"profileUrl":f"https://www.roblox.com/users/{u.get('userId')}/profile"})
            for u in other:
                w.writerow({"priority":"OTHER_GAME","username":u.get("username"),"displayName":u.get("displayName"),"userId":u.get("userId"),"role":u.get("role"),"rank":u.get("rank"),"presenceType":u.get("presenceType"),"presenceTypeName":u.get("presenceTypeName"),"lastLocation":u.get("lastLocation"),"placeId":u.get("placeId"),"rootPlaceId":u.get("rootPlaceId"),"universeId":u.get("universeId"),"profileUrl":f"https://www.roblox.com/users/{u.get('userId')}/profile"})
        print(f"CSV exported to {args.csv}", file=sys.stderr)
    return 0

def main():
    # If --tracker or --group-id present use unified logic, else fallback to original dev main for backward compat
    if any(x.startswith("--tracker") or x.startswith("--group-id") for x in sys.argv):
        sys.exit(tracker_main_logic())
    # else try original dev logic
    if HAS_DEV and hasattr(_dev, 'main'):
        # call original dev main (will parse its own args)
        _dev.main()
    else:
        sys.exit(tracker_main_logic())

if __name__ == "__main__":
    main()
