#!/usr/bin/env python3
"""
Merge sharded scan results into final prioritized results.
Usage: python merge_shards.py shard_0.json shard_1.json shard_2.json --json final.json --csv final.csv
"""

import argparse
import json
import csv
import sys
import glob
from pathlib import Path
from typing import List, Dict

def merge_shards(paths: List[str], json_out: str = "", csv_out: str = ""):
    # Expand globs for Windows (cmd doesn't expand shard_*.json)
    expanded: List[str] = []
    for p in paths:
        if "*" in p or "?" in p or "[" in p:
            expanded.extend(glob.glob(p))
        else:
            expanded.append(p)
    paths = expanded
    if not paths:
        print("No shard files found! Did you use quotes around shard_*.json on Windows?", file=sys.stderr)
        print("Windows fix: python merge_shards.py shard_0.json shard_1.json shard_2.json --json roblox_scan_results.json", file=sys.stderr)
        sys.exit(1)

    all_target = []
    all_other = []
    all_offline = []
    metas = []
    total_elapsed = 0
    group_id = None
    target_place = None
    target_universe = None
    game_name = None
    game_url = None
    group_url = None

    for p in paths:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        meta = data.get("meta", {})
        metas.append(meta)
        group_id = meta.get("groupId", group_id)
        target_place = meta.get("targetPlaceId", target_place)
        target_universe = meta.get("targetUniverseId", target_universe)
        game_name = meta.get("targetGameName", game_name)
        game_url = meta.get("targetGameUrl", game_url)
        group_url = meta.get("groupUrl", group_url)
        total_elapsed = max(total_elapsed, meta.get("elapsed_seconds", 0))
        all_target.extend(data.get("target_game_players", []))
        all_other.extend(data.get("other_game_players", []))
        all_offline.extend(data.get("offline_or_website", []))

    # Deduplicate by userId (in case shards overlapped due to ceil)
    def dedup(lst):
        seen = set()
        out = []
        for u in lst:
            uid = u.get("userId")
            if uid not in seen:
                seen.add(uid)
                out.append(u)
        return out

    all_target = dedup(all_target)
    all_other = dedup(all_other)
    all_offline = dedup(all_offline)

    # Sort as in scanner
    all_target.sort(key=lambda x: (x.get("username") or "").lower())
    all_other.sort(key=lambda x: ((x.get("lastLocation") or ""), (x.get("username") or "").lower()))
    all_offline.sort(key=lambda x: (x.get("username") or "").lower())

    # Reconcile: if someone appears in both target and other due to race, keep target
    target_ids = {u["userId"] for u in all_target}
    all_other = [u for u in all_other if u["userId"] not in target_ids]
    other_ids = {u["userId"] for u in all_other}
    all_offline = [u for u in all_offline if u["userId"] not in target_ids and u["userId"] not in other_ids]

    total_members = len(all_target) + len(all_other) + len(all_offline)
    # Use first meta timestamp or now
    import time
    timestamp = metas[0].get("timestamp") if metas else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    final_meta = {
        "groupId": group_id,
        "groupUrl": group_url,
        "targetPlaceId": target_place,
        "targetUniverseId": target_universe,
        "targetGameName": game_name,
        "targetGameUrl": game_url,
        "total_members": total_members,
        "elapsed_seconds": round(total_elapsed, 2),
        "timestamp": timestamp,
        "settings": {
            "shards": len(paths),
            "merged_from": [m.get("settings", {}) for m in metas],
        },
        "shard_metas": metas,
    }

    final = {
        "meta": final_meta,
        "summary": {
            "total_members": total_members,
            "target_game_count": len(all_target),
            "other_game_count": len(all_other),
            "offline_count": len(all_offline),
        },
        "target_game_players": all_target,
        "other_game_players": all_other,
        "offline_or_website": all_offline,
    }

    if json_out:
        Path(json_out).write_text(json.dumps(final, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Merged {len(paths)} shards -> {json_out} (target={len(all_target)} other={len(all_other)} offline={len(all_offline)} total={total_members})")
    else:
        json.dump(final, sys.stdout, indent=2, ensure_ascii=False)

    if csv_out:
        fieldnames = ["priority","username","displayName","userId","role","rank","presenceType","presenceTypeName","lastLocation","placeId","rootPlaceId","universeId","profileUrl"]
        rows = []
        for u in all_target:
            rows.append({
                "priority":"TARGET_GAME",
                "username":u.get("username"),
                "displayName":u.get("displayName"),
                "userId":u.get("userId"),
                "role":u.get("role"),
                "rank":u.get("rank"),
                "presenceType":u.get("presenceType"),
                "presenceTypeName":u.get("presenceTypeName"),
                "lastLocation":u.get("lastLocation"),
                "placeId":u.get("placeId"),
                "rootPlaceId":u.get("rootPlaceId"),
                "universeId":u.get("universeId"),
                "profileUrl":f"https://www.roblox.com/users/{u.get('userId')}/profile",
            })
        for u in all_other:
            rows.append({
                "priority":"OTHER_GAME",
                "username":u.get("username"),
                "displayName":u.get("displayName"),
                "userId":u.get("userId"),
                "role":u.get("role"),
                "rank":u.get("rank"),
                "presenceType":u.get("presenceType"),
                "presenceTypeName":u.get("presenceTypeName"),
                "lastLocation":u.get("lastLocation"),
                "placeId":u.get("placeId"),
                "rootPlaceId":u.get("rootPlaceId"),
                "universeId":u.get("universeId"),
                "profileUrl":f"https://www.roblox.com/users/{u.get('userId')}/profile",
            })
        with open(csv_out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
        print(f"CSV -> {csv_out} ({len(rows)} rows)")

    return final

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Merge sharded scan JSONs")
    p.add_argument("shards", nargs="+", help="Shard JSON files")
    p.add_argument("--json", type=str, default="roblox_scan_results.json", help="Output JSON")
    p.add_argument("--csv", type=str, default="", help="Output CSV")
    args = p.parse_args()
    merge_shards(args.shards, args.json, args.csv)
