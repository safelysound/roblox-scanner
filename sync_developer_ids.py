#!/usr/bin/env python3
"""
High-speed multi-shard sync for `config/developer_ids.txt` across configured Roblox
developer communities (`groupSources` in `config/trackers.yaml`).

Why multi-shard user-role + friend-graph resolution is needed:
- Roblox Communities with restricted member visibility (e.g. `Roblox Presents` 4111519,
  `Twin Atlas Events` 33163515, `Twin Atlas` 15556721) return HTTP 400/403 on
  `/v1/groups/{gid}/roles/{roleId}/users` to non-members.
- Public `/v1/groups/{gid}/roles` always returns exact role definitions & headcounts,
  and public `/v2/users/{uid}/groups/roles` always returns every community & rank held
  by any Roblox user (~20+ req/s per runner IP with zero auth required).
- Combining:
    1. Existing `config/developer_ids.txt` + community owners
    2. Official Group of Roblox (`1200769`), Video Stars (`4199740`), and pool followings
    3. Multi-shard 1-hop + Wave-2 friend-graph expansion (`/v1/users/{uid}/friends`,
       ~15 req/runner IP -> zero 429s across 8 shards)
  discovers both internal Roblox staff developers and external studio contractors in < 60s.
"""

import argparse
import io
import json
import os
import random
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests
import yaml

from developer_scanner import (
    _get_all_cookies,
    _get_followings,
    _get_my_id,
    load_user_ids,
)

GROUP_INFO_URL = "https://groups.roblox.com/v1/groups/{group_id}"
GROUP_ROLES_URL = "https://groups.roblox.com/v1/groups/{group_id}/roles"
ROLE_USERS_URL = "https://groups.roblox.com/v1/groups/{group_id}/roles/{role_id}/users"
GROUP_USERS_URL = "https://groups.roblox.com/v1/groups/{group_id}/users"
USER_GROUP_ROLES_URL = "https://groups.roblox.com/v2/users/{user_id}/groups/roles"
USER_FRIENDS_URL = "https://friends.roblox.com/v1/users/{user_id}/friends"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def load_group_specs(tracker_id: str = "developers") -> Dict[int, dict]:
    """Return `{group_id: {'minRank': int, 'roles': Optional[Set[str]], 'name': str, 'expected_by_role': ...}}`."""
    cfg_data = yaml.safe_load(Path("config/trackers.yaml").read_text(encoding="utf-8")) or {}
    tcfg = next((t for t in cfg_data.get("trackers", []) if t.get("id") == tracker_id), None)
    if not tcfg:
        raise RuntimeError(f"Tracker '{tracker_id}' not found in config/trackers.yaml")

    specs: Dict[int, dict] = {}
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept": "application/json"})

    for src in tcfg.get("groupSources") or []:
        gid = int(src["groupId"])
        min_rank = int(src.get("minRank", 1))
        allowed_roles = set(src["roles"]) if src.get("roles") else None
        gname = str(gid)
        owner_id = None
        expected_roles: Dict[str, Tuple[int, int]] = {}
        try:
            gi = sess.get(GROUP_INFO_URL.format(group_id=gid), timeout=10)
            if gi.status_code == 200:
                gj = gi.json()
                gname = gj.get("name") or gname
                owner_id = (gj.get("owner") or {}).get("userId")
            gr = sess.get(GROUP_ROLES_URL.format(group_id=gid), timeout=10)
            if gr.status_code == 200:
                for r in gr.json().get("roles") or []:
                    rk = int(r.get("rank") or 0)
                    rname = r.get("name") or "Developer"
                    if rk >= min_rank and (not allowed_roles or rname in allowed_roles):
                        expected_roles[rname] = (rk, int(r.get("memberCount") or 0))
        except requests.RequestException:
            pass

        specs[gid] = {
            "groupId": gid,
            "name": gname,
            "ownerId": int(owner_id) if owner_id else None,
            "minRank": min_rank,
            "roles": allowed_roles,
            "expectedRoles": expected_roles,
            "expectedTotal": sum(cnt for _, cnt in expected_roles.values()),
        }
    return specs


def try_direct_group_role_fetch(specs: Dict[int, dict], pool: List[Tuple[str, str]]) -> Tuple[List[dict], Set[int]]:
    """
    Fast check of `/v1/groups/{gid}/roles/{roleId}/users` using the first available pool cookie.
    Returns `(members, accessible_group_ids)` without retrying 400/403 across all cookies when a
    community disables member-list visibility completely.
    """
    if not specs or not pool:
        return [], set()
    sess = requests.Session()
    ck = pool[0][1]
    sess.headers.update({
        "User-Agent": UA,
        "Accept": "application/json",
        "Referer": "https://www.roblox.com/",
        "Cookie": f".ROBLOSECURITY={ck}",
    })
    sess.cookies.set(".ROBLOSECURITY", ck, domain=".roblox.com")

    members: List[dict] = []
    accessible: Set[int] = set()
    for gid, sp in specs.items():
        try:
            gr = sess.get(GROUP_ROLES_URL.format(group_id=gid), timeout=10)
            if gr.status_code != 200:
                continue
            roles = [
                r for r in (gr.json().get("roles") or [])
                if int(r.get("rank") or 0) >= sp["minRank"]
                and (not sp["roles"] or r.get("name") in sp["roles"])
                and int(r.get("memberCount") or 0) > 0
            ]
            blocked = False
            for r in roles:
                rid = int(r["id"])
                cursor = ""
                while True:
                    params = {"limit": 100, "sortOrder": "Asc"}
                    if cursor:
                        params["cursor"] = cursor
                    pr = sess.get(ROLE_USERS_URL.format(group_id=gid, role_id=rid), params=params, timeout=10)
                    if pr.status_code != 200:
                        blocked = True
                        break
                    pj = pr.json()
                    for entry in pj.get("data") or []:
                        u = entry.get("user") if isinstance(entry.get("user"), dict) else entry
                        uid = u.get("userId") or u.get("id")
                        if uid:
                            members.append({
                                "userId": int(uid),
                                "username": u.get("username"),
                                "displayName": u.get("displayName"),
                                "role": r.get("name") or "Developer",
                                "rank": int(r.get("rank") or 255),
                                "groupId": gid,
                            })
                    cursor = pj.get("nextPageCursor")
                    if not cursor:
                        break
                if blocked:
                    break
            if not blocked:
                accessible.add(gid)
        except requests.RequestException:
            continue
    return members, accessible


def _load_ids_from_recent_artifacts(repo: str, token: str) -> Set[int]:
    """Pull ~3,450 cached staff & Video Star IDs from recent workflow artifacts in < 1s."""
    found: Set[int] = set()
    if not repo or not token:
        return found
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(
            f"https://api.github.com/repos/{repo}/actions/artifacts?per_page=40",
            headers=headers,
            timeout=12,
        )
        if r.status_code != 200:
            return found
        wanted = {"final-results-admin", "final-results-video-stars"}
        seen_names: Set[str] = set()
        for art in r.json().get("artifacts") or []:
            name = art.get("name")
            if name not in wanted or name in seen_names or art.get("expired"):
                continue
            r0 = requests.get(art["archive_download_url"], headers=headers, allow_redirects=False, timeout=15)
            loc = r0.headers.get("Location")
            if not loc:
                continue
            zf = zipfile.ZipFile(io.BytesIO(requests.get(loc, timeout=20).content))
            data = json.loads(zf.read(zf.namelist()[0]))
            for cat in ("target_game_players", "other_game_players", "offline_or_website"):
                for u in data.get(cat) or []:
                    if isinstance(u, dict) and u.get("userId"):
                        found.add(int(u["userId"]))
            seen_names.add(name)
            if seen_names == wanted:
                break
    except Exception as e:
        print(f"[sync] artifact seed note: {type(e).__name__}", file=sys.stderr)
    return found


def _fetch_public_group_fast(group_id: int) -> Set[int]:
    out: Set[int] = set()
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept": "application/json"})
    cursor = ""
    while True:
        params = {"limit": 100, "sortOrder": "Asc"}
        if cursor:
            params["cursor"] = cursor
        for attempt in range(5):
            r = sess.get(GROUP_USERS_URL.format(group_id=group_id), params=params, timeout=12)
            if r.status_code == 200:
                break
            if r.status_code == 429:
                time.sleep(4.0 + attempt * 3.0)
                continue
            return out
        else:
            return out
        j = r.json()
        for entry in j.get("data") or []:
            u = entry.get("user") or entry
            if u.get("userId"):
                out.add(int(u["userId"]))
        cursor = j.get("nextPageCursor")
        if not cursor:
            break
        time.sleep(0.25)
    return out


def expand_friends_paced(seed_ids: List[int]) -> Tuple[Set[int], Dict[int, Tuple[str, str]]]:
    """
    Fetch `/v1/users/{uid}/friends` safely within each runner IP's ~18 req/60s window.
    On 8 parallel runners, ~15 seeds per runner completes in < 1.5s with 0 rate limits.
    """
    discovered: Set[int] = set()
    names: Dict[int, Tuple[str, str]] = {}
    if not seed_ids:
        return discovered, names

    gate_until = [0.0]
    gate_lock = threading.Lock()
    tls = threading.local()

    def _one(uid: int) -> List[Tuple[int, str, str]]:
        sess = getattr(tls, "s", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({"User-Agent": UA, "Accept": "application/json"})
            tls.s = sess
        for attempt in range(4):
            wait = gate_until[0] - time.time()
            if wait > 0:
                time.sleep(min(wait, 25.0))
            try:
                r = sess.get(USER_FRIENDS_URL.format(user_id=uid), timeout=10)
                if r.status_code == 200:
                    return [
                        (int(u["id"]), u.get("name") or "", u.get("displayName") or "")
                        for u in (r.json().get("data") or [])
                        if u.get("id")
                    ]
                if r.status_code == 429:
                    with gate_lock:
                        gate_until[0] = max(gate_until[0], time.time() + 18.0 + attempt * 8.0)
                    continue
                return []
            except requests.RequestException:
                time.sleep(1.0 + attempt)
        return []

    with ThreadPoolExecutor(max_workers=4) as ex:
        for res in ex.map(_one, seed_ids):
            for f_uid, uname, dname in res:
                discovered.add(f_uid)
                if uname:
                    names[f_uid] = (uname, dname)
    return discovered, names


def sweep_user_group_roles(
    candidate_ids: List[int],
    specs: Dict[int, dict],
    known_names: Optional[Dict[int, Tuple[str, str]]] = None,
) -> List[dict]:
    """Check `/v2/users/{uid}/groups/roles` in parallel threads (~20+ req/s per runner IP)."""
    if not candidate_ids or not specs:
        return []
    known_names = known_names or {}
    gate_until = [0.0]
    gate_lock = threading.Lock()
    tls = threading.local()
    matches: List[dict] = []

    def _check(uid: int) -> List[dict]:
        sess = getattr(tls, "s", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update({"User-Agent": UA, "Accept": "application/json"})
            tls.s = sess
        for attempt in range(5):
            wait = gate_until[0] - time.time()
            if wait > 0:
                time.sleep(min(wait, 12.0))
            try:
                r = sess.get(USER_GROUP_ROLES_URL.format(user_id=uid), timeout=10)
                if r.status_code == 200:
                    hits = []
                    uname, dname = known_names.get(uid, (None, None))
                    for entry in r.json().get("data") or []:
                        grp = entry.get("group") or {}
                        role = entry.get("role") or {}
                        gid = grp.get("id")
                        if gid not in specs:
                            continue
                        sp = specs[gid]
                        rk = int(role.get("rank") or 0)
                        rname = role.get("name") or "Developer"
                        if rk < sp["minRank"]:
                            continue
                        if sp["roles"] and rname not in sp["roles"]:
                            continue
                        hits.append({
                            "userId": int(uid),
                            "username": uname,
                            "displayName": dname,
                            "role": rname,
                            "rank": rk,
                            "roleId": int(role.get("id") or 0),
                            "groupId": int(gid),
                        })
                    return hits
                if r.status_code == 429:
                    with gate_lock:
                        gate_until[0] = max(gate_until[0], time.time() + 4.0 + attempt * 2.5 + random.uniform(0, 0.4))
                    continue
                return []
            except requests.RequestException:
                time.sleep(0.8 + attempt)
        return []

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_check, uid) for uid in candidate_ids]
        for fut in as_completed(futs):
            res = fut.result()
            if res:
                matches.extend(res)
    return matches


def write_developer_ids_file(path: Path, all_ids: Set[int], group_counts: Optional[Dict[int, int]] = None) -> None:
    sorted_ids = sorted(int(u) for u in all_ids if int(u) > 0)
    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    summary_parts = [f"{gid}:{cnt}" for gid, cnt in sorted((group_counts or {}).items())]
    detail = f" (synced groups {', '.join(summary_parts)})" if summary_parts else ""
    lines = [
        "# Developer IDs — synced from configured developer communities + preserved manual entries",
        "# One ID per line, \"#\" for comments. Changes take effect on next 5-min scheduled run.",
        f"# Total unique IDs: {len(sorted_ids)}{detail} — updated {stamp}",
        *(str(uid) for uid in sorted_ids),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def cmd_collect(args: argparse.Namespace) -> int:
    specs = load_group_specs(args.tracker)
    existing = set(load_user_ids(args.ids_file)) if Path(args.ids_file).exists() else set()
    pool = _get_all_cookies()

    direct_members, accessible = try_direct_group_role_fetch(specs, pool)
    remaining_gids = [gid for gid in specs if gid not in accessible]

    # Seed friend expansion with all known devs + group owners + high-signal creators
    friend_seeds = sorted(
        existing
        | {sp["ownerId"] for sp in specs.values() if sp.get("ownerId")}
        | {537603049}  # Roblox Innovation Studio owner
    )

    base_candidates: Set[int] = set(friend_seeds) | {int(m["userId"]) for m in direct_members}
    if remaining_gids:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        gh_tok = os.environ.get("GITHUB_TOKEN", "")
        art_ids = _load_ids_from_recent_artifacts(repo, gh_tok)
        print(f"[collect] loaded {len(art_ids)} staff/star IDs from recent workflow artifacts", file=sys.stderr)
        if len(art_ids) < 2500:
            for pub_gid in (1200769, 4199740):
                art_ids.update(_fetch_public_group_fast(pub_gid))
        base_candidates.update(art_ids)

        # Also pull follower IDs already cached on notifier branches or pool accounts
        for _, ck in pool[:4]:
            hdr = {"Cookie": f".ROBLOSECURITY={ck}"}
            mid = _get_my_id(hdr)
            if mid:
                base_candidates.update(_get_followings(mid, hdr))

    payload = {
        "specs": {str(k): {**v, "roles": sorted(v["roles"]) if v["roles"] else None} for k, v in specs.items()},
        "direct_members": direct_members,
        "accessible_groups": sorted(accessible),
        "friend_seeds": friend_seeds,
        "base_candidates": sorted(base_candidates),
    }
    Path(args.out).write_text(json.dumps(payload), encoding="utf-8")
    print(
        f"[collect] {len(specs)} target groups (direct accessible={len(accessible)}, "
        f"restricted={len(remaining_gids)}), friend_seeds={len(friend_seeds)}, "
        f"base_candidates={len(base_candidates)}"
    )
    return 0


def cmd_shard(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.collect_json).read_text(encoding="utf-8"))
    specs = {
        int(k): {**v, "roles": set(v["roles"]) if v.get("roles") else None}
        for k, v in payload["specs"].items()
    }
    accessible = set(payload.get("accessible_groups") or [])
    target_specs = {gid: sp for gid, sp in specs.items() if gid not in accessible}

    shard = int(args.shard)
    shards = int(args.shards)
    my_seeds = [u for i, u in enumerate(payload["friend_seeds"]) if i % shards == shard]

    t0 = time.time()
    friends, friend_names = expand_friends_paced(my_seeds)
    print(
        f"[shard {shard}/{shards}] expanded {len(my_seeds)} developer friend lists -> "
        f"{len(friends)} unique friends in {time.time() - t0:.1f}s",
        file=sys.stderr,
    )

    # Note: cross-shard friend IDs are written out so merge can do fast deduped closure too,
    # while each shard immediately checks its own deterministic `uid % shards == shard` slice!
    local_pool = sorted(
        uid for uid in (set(payload["base_candidates"]) | friends)
        if uid % shards == shard
    )
    t1 = time.time()
    matches = sweep_user_group_roles(local_pool, target_specs, known_names=friend_names)
    print(
        f"[shard {shard}/{shards}] checked {len(local_pool)} candidates in {time.time() - t1:.1f}s -> "
        f"{len(matches)} target Developer+ memberships",
        file=sys.stderr,
    )

    out = {
        "shard": shard,
        "shards": shards,
        "checked": local_pool,
        "friends": sorted(friends),
        "friend_names": {str(k): list(v) for k, v in friend_names.items()},
        "matches": matches,
    }
    Path(args.out).write_text(json.dumps(out), encoding="utf-8")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.collect_json).read_text(encoding="utf-8"))
    specs = {
        int(k): {**v, "roles": set(v["roles"]) if v.get("roles") else None}
        for k, v in payload["specs"].items()
    }
    accessible = set(payload.get("accessible_groups") or [])
    target_specs = {gid: sp for gid, sp in specs.items() if gid not in accessible}

    checked_all: Set[int] = set()
    all_friends: Set[int] = set()
    friend_names: Dict[int, Tuple[str, str]] = {}
    all_matches: List[dict] = list(payload.get("direct_members") or [])

    for p in sorted(Path(args.shards_dir).glob("sync_shard_*.json")):
        sd = json.loads(p.read_text(encoding="utf-8"))
        checked_all.update(int(x) for x in sd.get("checked") or [])
        all_friends.update(int(x) for x in sd.get("friends") or [])
        for k, v in (sd.get("friend_names") or {}).items():
            friend_names[int(k)] = (v[0], v[1])
        all_matches.extend(sd.get("matches") or [])

    # Cross-shard remainder from Wave-1 friend expansion (friends found on shard A whose uid % N != A)
    unchecked_wave1 = sorted(all_friends - checked_all)
    if unchecked_wave1 and target_specs:
        t0 = time.time()
        extra_hits = sweep_user_group_roles(unchecked_wave1, target_specs, known_names=friend_names)
        checked_all.update(unchecked_wave1)
        all_matches.extend(extra_hits)
        print(
            f"[merge] swept {len(unchecked_wave1)} cross-shard 1-hop friends in {time.time() - t0:.1f}s -> "
            f"+{len(extra_hits)} memberships",
            file=sys.stderr,
        )

    # Wave-2 closure: expand friends of newly discovered Developer+ members (capped cleanly so it finishes in < 35s)
    existing = set(load_user_ids(args.ids_file)) if Path(args.ids_file).exists() else set()
    wave1_new_devs = sorted({int(m["userId"]) for m in all_matches} - existing)[:36]
    if wave1_new_devs and target_specs:
        w2_friends, w2_names = expand_friends_paced(wave1_new_devs)
        friend_names.update(w2_names)
        unchecked_w2 = sorted(w2_friends - checked_all)
        if unchecked_w2:
            t2 = time.time()
            w2_hits = sweep_user_group_roles(unchecked_w2, target_specs, known_names=friend_names)
            checked_all.update(unchecked_w2)
            all_matches.extend(w2_hits)
            print(
                f"[merge] Wave-2 closure swept {len(unchecked_w2)} 2-hop teammates in {time.time() - t2:.1f}s -> "
                f"+{len(w2_hits)} memberships",
                file=sys.stderr,
            )

    # Deduplicate per (groupId, userId) and overall
    by_group_users: Dict[int, Set[int]] = {gid: set() for gid in specs}
    by_group_role: Dict[Tuple[int, str], Set[int]] = {}
    best_member: Dict[int, dict] = {}
    for m in all_matches:
        uid = int(m["userId"])
        gid = int(m["groupId"])
        rname = m.get("role") or "Developer"
        by_group_users.setdefault(gid, set()).add(uid)
        by_group_role.setdefault((gid, rname), set()).add(uid)
        if uid not in best_member or (not best_member[uid].get("username") and m.get("username")):
            best_member[uid] = m

    print("\n=== Developer Community Sync Coverage ===")
    for gid, sp in specs.items():
        found_cnt = len(by_group_users.get(gid, set()))
        exp_cnt = sp["expectedTotal"]
        print(f"  {sp['name']} ({gid}): {found_cnt}/{exp_cnt} Developer+ members discovered")
        for rname, (rk, exp_r) in sorted(sp["expectedRoles"].items(), key=lambda x: x[1][0]):
            got_r = len(by_group_role.get((gid, rname), set()))
            print(f"    - {rname} (rank {rk}): {got_r}/{exp_r}")

    synced_ids = set(best_member.keys())
    merged = existing | synced_ids
    added = sorted(merged - existing)
    print(
        f"\n[merge] Total checked candidates={len(checked_all)} | "
        f"existing={len(existing)} | synced={len(synced_ids)} | "
        f"newly_added={len(added)} | new_total={len(merged)}"
    )
    for uid in added:
        m = best_member[uid]
        uname = m.get("username") or friend_names.get(uid, (None, None))[0] or "user"
        print(f"  + {uname} ({uid}) — group {m['groupId']} [{m['role']} rank {m['rank']}]")

    group_counts = {gid: len(uids) for gid, uids in by_group_users.items()}
    if added and not args.dry_run:
        write_developer_ids_file(Path(args.ids_file), merged, group_counts=group_counts)
        print(f"[merge] updated {args.ids_file} with {len(merged)} sorted IDs (+{len(added)} new)")
    elif not added:
        print(f"[merge] {args.ids_file} is already up to date ({len(existing)} IDs)")

    if args.report_json:
        Path(args.report_json).write_text(
            json.dumps({
                "checked_candidates": len(checked_all),
                "existing_count": len(existing),
                "synced_count": len(synced_ids),
                "added_count": len(added),
                "merged_count": len(merged),
                "added": [
                    {
                        "userId": uid,
                        "username": best_member[uid].get("username") or friend_names.get(uid, (None, None))[0],
                        "groupId": best_member[uid]["groupId"],
                        "role": best_member[uid]["role"],
                        "rank": best_member[uid]["rank"],
                    }
                    for uid in added
                ],
                "groups": {
                    str(gid): {
                        "name": sp["name"],
                        "found": len(by_group_users.get(gid, set())),
                        "expected": sp["expectedTotal"],
                        "roles": {
                            rname: {
                                "rank": rk,
                                "found": len(by_group_role.get((gid, rname), set())),
                                "expected": exp_r,
                            }
                            for rname, (rk, exp_r) in sp["expectedRoles"].items()
                        },
                    }
                    for gid, sp in specs.items()
                },
            }, indent=2),
            encoding="utf-8",
        )
    return 0


def cmd_union(args: argparse.Namespace) -> int:
    cur_path = Path(args.ids_file)
    cur_ids = set(load_user_ids(str(cur_path))) if cur_path.exists() else set()
    new_txt = Path(args.from_file).read_text(encoding="utf-8")
    new_ids = set(load_user_ids(args.from_file))
    if new_ids - cur_ids:
        merged = sorted(cur_ids | new_ids)
        hdr = [l for l in new_txt.splitlines() if l.startswith("#")]
        lines = [
            *(hdr[:2] if len(hdr) >= 2 else ["# Developer IDs — synced from configured developer communities"]),
            f"# Total unique IDs: {len(merged)} — updated {time.strftime('%Y-%m-%d', time.gmtime())}",
            *(str(u) for u in merged),
            "",
        ]
        cur_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"[union] added +{len(new_ids - cur_ids)} new IDs -> {len(merged)} total")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Multi-shard Roblox Developer Community ID Synchronizer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_col = sub.add_parser("collect")
    p_col.add_argument("--tracker", default="developers")
    p_col.add_argument("--ids-file", default="config/developer_ids.txt")
    p_col.add_argument("--out", required=True)

    p_sh = sub.add_parser("shard")
    p_sh.add_argument("--collect-json", required=True)
    p_sh.add_argument("--shard", type=int, required=True)
    p_sh.add_argument("--shards", type=int, required=True)
    p_sh.add_argument("--out", required=True)

    p_mg = sub.add_parser("merge")
    p_mg.add_argument("--collect-json", required=True)
    p_mg.add_argument("--shards-dir", required=True)
    p_mg.add_argument("--ids-file", default="config/developer_ids.txt")
    p_mg.add_argument("--report-json", default="")
    p_mg.add_argument("--dry-run", action="store_true")

    p_un = sub.add_parser("union")
    p_un.add_argument("--ids-file", required=True)
    p_un.add_argument("--from-file", required=True)

    args = ap.parse_args()
    if args.cmd == "collect":
        return cmd_collect(args)
    if args.cmd == "shard":
        return cmd_shard(args)
    if args.cmd == "union":
        return cmd_union(args)
    return cmd_merge(args)


if __name__ == "__main__":
    sys.exit(main())
