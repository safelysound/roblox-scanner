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
import csv
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
    for key in ["ROBLOSECURITY", "ROBLOX_COOKIE", "ROBLOSECURITY_1", "ROBLOSECURITY_2", "ROBLOSECURITY_3", "ROBLOSECURITY_4", "ROBLOSECURITY_5", "ROBLOSECURITY_6", "ROBLOSECURITY1", "ROBLOSECURITY2"]:
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

def _log_cookie_status():
    ck = _get_roblox_cookie()
    if ck:
        print(f"Using ROBLOSECURITY cookie (len {len(ck)}, prefix {ck[:20]}...) — authenticated presence (Follow bypass)", file=sys.stderr)
        return True
    else:
        print("No ROBLOSECURITY/ROBLOX_COOKIE found — unauthenticated presence (Offline-hidden devs will stay Offline)", file=sys.stderr)
        return False


def _get_my_id(headers):
    try:
        import requests as _req
        r=_req.get("https://users.roblox.com/v1/users/authenticated", timeout=10, headers={"User-Agent":"Mozilla/5.0","Cookie": headers.get("Cookie","")})
        if r.status_code==200:
            return r.json().get("id")
    except: pass
    return None

def _profile_shows_hunt(user_id, headers):
    """Profile scraping fallback for hidden even when Following (e.g., 91512961, 92501615)."""
    try:
        import requests as _req
        ck=headers.get("Cookie","")
        h2={"User-Agent":"Mozilla/5.0","Referer":"https://www.roblox.com/","Accept":"text/html","Cookie": ck}
        r=_req.get(f"https://www.roblox.com/users/{user_id}/profile", timeout=15, headers=h2)
        if r.status_code!=200:
            return False
        t=r.text
        return "74205509034203" in t or "The Hunt: Roblox 20" in t
    except Exception as e:
        print(f"profile scrape {user_id} error {e}", file=sys.stderr)
        return False

def _get_followings(my_id, headers):
    following=set()
    if not my_id:
        return following
    try:
        import requests as _req
        cursor=""
        for _ in range(20):
            url=f"https://friends.roblox.com/v1/users/{my_id}/followings?limit=100&sortOrder=Asc"
            if cursor:
                url+=f"&cursor={cursor}"
            r=_req.get(url, timeout=10, headers={"User-Agent":"Mozilla/5.0","Cookie": headers.get("Cookie","")})
            if r.status_code!=200:
                break
            j=r.json()
            for e in j.get("data",[]):
                following.add(e["id"])
            cursor=j.get("nextPageCursor")
            if not cursor:
                break
    except: pass
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

def fetch_user_info(user_id: int) -> Dict[str, Any]:
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
    results={}
    for i in range(0, len(user_ids), PRESENCE_BATCH_SIZE):
        batch=user_ids[i:i+PRESENCE_BATCH_SIZE]
        try:
            r=requests.post(PRESENCE_API, json={"userIds": batch}, timeout=15, headers=headers)
            # If 403 due to bad cookie/CSRF, retry unauthenticated as fallback
            if r.status_code==403 and "Cookie" in headers:
                print(f"Presence 403 with cookie — retrying unauthenticated (cookie may be invalid/expired)", file=sys.stderr)
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

    # Fetch user infos for display names (for those in slice)
    members=[]
    for uid in ids_for_presence:
        info=fetch_user_info(uid)
        # Need role/rank for compatibility with group scanner — use Developer tag
        info.update({"role": "Developer", "rank": 255, "roleId": 0})
        members.append(info)
        time.sleep(0.1)

    presences=fetch_presences(ids_for_presence)
    print(f"Got {len(presences)}/{len(ids_for_presence)} presence responses")

    # Fetch followings for isFollowing flag (if cookie present)
    headers_for_follow=_cookie_headers()
    my_id=_get_my_id(headers_for_follow)
    followings=_get_followings(my_id, headers_for_follow) if my_id else set()
    if my_id:
        print(f"Alt {my_id} following {len(followings)} for isFollowing flag")

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
                h2={"User-Agent":"Mozilla/5.0","Referer":"https://www.roblox.com/","Accept":"application/json","Cookie": ck2}
                r=_req.get(f"https://friends.roblox.com/v1/users/{my_id}/followings?limit=100&sortOrder=Asc", timeout=10, headers=h2)
                if r.status_code==200 and any(e.get("id")==uid for e in r.json().get("data",[])):
                    is_following=True
                    print(f"per-user isFollowing true for {uid}", file=sys.stderr)
            except: pass
        # Profile scraping fallback for hidden even when Following (e.g., 91512961, 92501615) — website shows Hunt even when presence hides
        if ptype==2 and place_id is None and root_place is None and univ is None and is_following:
            if _profile_shows_hunt(uid, headers_for_follow):
                print(f"profile scrape: {uid} shows Hunt via profile (hidden presence but Following reveals Hunt)", file=sys.stderr)
                place_id=74205509034203
                root_place=74205509034203
                univ=10766456501
                last_loc="The Hunt: Roblox 20"
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
