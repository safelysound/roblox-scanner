#!/usr/bin/env python3
"""
High-speed multi-shard sync for `config/developer_ids.txt` across configured Roblox
developer communities (`groupSources` in `config/trackers.yaml`).

4-stage zero-bottleneck pipeline:
1. `collect` (1 job, ~5s): Resolves target group role specs (`expectedTotal` per role),
   checks if any community exposes `/roles/{id}/users` directly, and builds the initial
   candidate & friend-seed sets from `config/developer_ids.txt`, community owners,
   cached `final-results-admin` (`1200769`), `final-results-video-stars` (`4199740`),
   and pool account followings.
2. `expand` (8 parallel shards, ~5s): Each runner expands `/v1/users/{uid}/friends` for
   its `1/8` slice of seed developers (~17 req/runner IP -> under the 18 req/min IP window).
3. `sweep` (12 parallel shards, ~2.5 min): Each runner combines `base_candidates` + all 8
   `expand` shards and checks `/v2/users/{uid}/groups/roles` ONLY on `uid % 12 == shard`.
4. `merge` (1 job, ~2s, 0 network calls): Deduplicates all 12 shards, logs exact per-group
   & per-role discovery coverage, updates `config/developer_ids.txt`, and writes
   `sync_report.json`.
"""

import argparse
import io
import json
import os
import random
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
    fetch_user_infos,
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

    friend_seeds = sorted(
        existing
        | {sp["ownerId"] for sp in specs.values() if sp.get("ownerId")}
        | {537603049}
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


def cmd_expand(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.collect_json).read_text(encoding="utf-8"))
    shard = int(args.shard)
    shards = int(args.shards)
    my_seeds = [u for i, u in enumerate(payload["friend_seeds"]) if i % shards == shard]
    t0 = time.time()
    friends, friend_names = expand_friends_paced(my_seeds)
    print(
        f"[expand {shard}/{shards}] expanded {len(my_seeds)} developer friend lists -> "
        f"{len(friends)} friends in {time.time() - t0:.1f}s",
        file=sys.stderr,
    )
    Path(args.out).write_text(
        json.dumps({
            "shard": shard,
            "friends": sorted(friends),
            "friend_names": {str(k): list(v) for k, v in friend_names.items()},
        }),
        encoding="utf-8",
    )
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.collect_json).read_text(encoding="utf-8"))
    specs = {
        int(k): {**v, "roles": set(v["roles"]) if v.get("roles") else None}
        for k, v in payload["specs"].items()
    }
    accessible = set(payload.get("accessible_groups") or [])
    target_specs = {gid: sp for gid, sp in specs.items() if gid not in accessible}

    full_pool: Set[int] = set(payload["base_candidates"])
    friend_names: Dict[int, Tuple[str, str]] = {}
    for p in sorted(Path(args.friends_dir).glob("friends_shard_*.json")):
        fd = json.loads(p.read_text(encoding="utf-8"))
        full_pool.update(int(x) for x in fd.get("friends") or [])
        for k, v in (fd.get("friend_names") or {}).items():
            friend_names[int(k)] = (v[0], v[1])

    shard = int(args.shard)
    shards = int(args.shards)
    local_pool = sorted(uid for uid in full_pool if uid % shards == shard)

    t0 = time.time()
    matches = sweep_user_group_roles(local_pool, target_specs, known_names=friend_names)
    print(
        f"[sweep {shard}/{shards}] checked {len(local_pool)}/{len(full_pool)} candidates "
        f"in {time.time() - t0:.1f}s -> {len(matches)} target Developer+ memberships",
        file=sys.stderr,
    )
    Path(args.out).write_text(
        json.dumps({
            "shard": shard,
            "shards": shards,
            "checked_count": len(local_pool),
            "matches": matches,
        }),
        encoding="utf-8",
    )
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.collect_json).read_text(encoding="utf-8"))
    specs = {
        int(k): {**v, "roles": set(v["roles"]) if v.get("roles") else None}
        for k, v in payload["specs"].items()
    }

    total_checked = 0
    all_matches: List[dict] = list(payload.get("direct_members") or [])
    for p in sorted(Path(args.shards_dir).glob("sync_shard_*.json")):
        sd = json.loads(p.read_text(encoding="utf-8"))
        total_checked += int(sd.get("checked_count") or 0)
        all_matches.extend(sd.get("matches") or [])

    by_group_users: Dict[int, Set[int]] = {gid: set() for gid in specs}
    by_group_role: Dict[Tuple[int, str], Set[int]] = {}
    best_member: Dict[int, dict] = {}
    all_roles_per_user: Dict[int, Set[Tuple[str, int, str, int]]] = {}
    for m in all_matches:
        uid = int(m["userId"])
        gid = int(m["groupId"])
        rname = m.get("role") or "Developer"
        by_group_users.setdefault(gid, set()).add(uid)
        by_group_role.setdefault((gid, rname), set()).add(uid)
        all_roles_per_user.setdefault(uid, set()).add((specs[gid]["name"], gid, rname, int(m.get("rank") or 255)))
        if uid not in best_member or (not best_member[uid].get("username") and m.get("username")):
            best_member[uid] = m

    missing_names = [u for u, m in best_member.items() if not m.get("username")]
    if missing_names:
        try:
            uinfos = fetch_user_infos(missing_names)
            for u, inf in uinfos.items():
                best_member[u]["username"] = inf.get("username")
                best_member[u]["displayName"] = inf.get("displayName")
        except Exception:
            pass

    existing = set(load_user_ids(args.ids_file)) if Path(args.ids_file).exists() else set()

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
        f"\n[merge] Total checked candidates={total_checked} | "
        f"existing={len(existing)} | synced={len(synced_ids)} | "
        f"newly_added={len(added)} | new_total={len(merged)}"
    )
    for uid in added:
        m = best_member[uid]
        roles_str = "; ".join(f"{gn}: {rn}" for gn, _, rn, _ in sorted(all_roles_per_user[uid]))
        print(f"  + {m.get('username') or 'user'} ({uid}) -> {roles_str}")

    group_counts = {gid: len(uids) for gid, uids in by_group_users.items()}
    if added and not args.dry_run:
        write_developer_ids_file(Path(args.ids_file), merged, group_counts=group_counts)
        print(f"[merge] updated {args.ids_file} with {len(merged)} sorted IDs (+{len(added)} new)")
    elif not added:
        print(f"[merge] {args.ids_file} already contains all {len(merged)} IDs")

    if args.report_json:
        Path(args.report_json).write_text(
            json.dumps({
                "checked_candidates": total_checked,
                "existing_count": len(existing),
                "synced_count": len(synced_ids),
                "added_count": len(added),
                "merged_count": len(merged),
                "added": [
                    {
                        "userId": uid,
                        "username": best_member[uid].get("username"),
                        "displayName": best_member[uid].get("displayName"),
                        "roles": [f"{gn}: {rn}" for gn, _, rn, _ in sorted(all_roles_per_user[uid])],
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

    p_exp = sub.add_parser("expand")
    p_exp.add_argument("--collect-json", required=True)
    p_exp.add_argument("--shard", type=int, required=True)
    p_exp.add_argument("--shards", type=int, required=True)
    p_exp.add_argument("--out", required=True)

    p_sw = sub.add_parser("sweep")
    p_sw.add_argument("--collect-json", required=True)
    p_sw.add_argument("--friends-dir", required=True)
    p_sw.add_argument("--shard", type=int, required=True)
    p_sw.add_argument("--shards", type=int, required=True)
    p_sw.add_argument("--out", required=True)

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
    if args.cmd == "expand":
        return cmd_expand(args)
    if args.cmd == "sweep":
        return cmd_sweep(args)
    if args.cmd == "union":
        return cmd_union(args)
    return cmd_merge(args)


if __name__ == "__main__":
    sys.exit(main())
