#!/usr/bin/env python3
"""
Sync Developer user IDs from configured Roblox Communities / group role thresholds
(`groupSources` in config/trackers.yaml) and optionally persist newly discovered IDs
back into `config/developer_ids.txt`.

Design:
1. Primary fast path: authenticates with `.ROBLOSECURITY` cookies from the pool and queries
   `/v1/groups/{groupId}/roles` + `/v1/groups/{groupId}/roles/{roleId}/users?limit=100`
   only for roles matching `rank >= minRank` (and optional `roles` name allowlist).
2. Automatic restricted-group fallback (CLI / sync mode): if a community restricts member
   list visibility even to signed-in non-members (HTTP 400/403), sweeps the known candidate
   universe (Official Group of Roblox 1200769 + accessible dev groups + followed pool accounts)
   via public `/v2/users/{uid}/groups/roles` with bounded concurrency and 429 backoff.
3. Additive union: existing entries in `config/developer_ids.txt` are always preserved.
"""

import argparse
import sys
import time
import random
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

GROUP_ROLES_URL = "https://groups.roblox.com/v1/groups/{group_id}/roles"
ROLE_USERS_URL = "https://groups.roblox.com/v1/groups/{group_id}/roles/{role_id}/users"
GROUP_USERS_URL = "https://groups.roblox.com/v1/groups/{group_id}/users"
USER_GROUP_ROLES_URL = "https://groups.roblox.com/v2/users/{user_id}/groups/roles"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _make_sessions(pool: Optional[List[Tuple[str, str]]] = None) -> List[Tuple[str, requests.Session]]:
    if pool is None:
        pool = _get_all_cookies()
    sessions: List[Tuple[str, requests.Session]] = []
    for key, ck in pool:
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json",
            "Referer": "https://www.roblox.com/",
            "Cookie": f".ROBLOSECURITY={ck}",
        })
        s.cookies.set(".ROBLOSECURITY", ck, domain=".roblox.com")
        sessions.append((key, s))
    if not sessions:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "application/json"})
        sessions.append(("unauthenticated", s))
    return sessions


def _get_with_pool_failover(
    sessions: List[Tuple[str, requests.Session]],
    url: str,
    params: Optional[ dict ] = None,
    start_idx: int = 0,
    max_attempts: int = 6,
) -> Tuple[Optional[dict], int]:
    last_status = 0
    n = len(sessions)
    idx = start_idx % n
    for attempt in range(max_attempts):
        _, sess = sessions[idx % n]
        try:
            r = sess.get(url, params=params, timeout=15)
            last_status = r.status_code
            if r.status_code == 200:
                return r.json(), 200
            if r.status_code == 429:
                # Rotate to another cookie session first; back off if IP window is saturated
                idx += 1
                try:
                    ra = float(r.headers.get("Retry-After") or 0)
                except ValueError:
                    ra = 0.0
                time.sleep(max(ra, 2.5 + attempt * 2.0) + random.uniform(0, 0.5))
                continue
            if r.status_code in (400, 401, 403):
                # Try remaining distinct sessions in case only one cookie lacks permission / is stale
                if attempt + 1 < min(n, max_attempts):
                    idx += 1
                    continue
                return None, r.status_code
            if r.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None, r.status_code
        except requests.RequestException:
            idx += 1
            time.sleep(1.0 + attempt)
    return None, last_status


def _fetch_public_group_ids(group_id: int, sessions: List[Tuple[str, requests.Session]]) -> Set[int]:
    """Fetch all user IDs from a large public group (e.g. Official Group of Roblox 1200769)."""
    out: Set[int] = set()
    cursor = ""
    page = 0
    while True:
        page += 1
        params = {"limit": 100, "sortOrder": "Asc"}
        if cursor:
            params["cursor"] = cursor
        data, status = _get_with_pool_failover(
            sessions,
            GROUP_USERS_URL.format(group_id=group_id),
            params=params,
            start_idx=page,
        )
        if status != 200 or not data:
            break
        batch = data.get("data") or []
        for entry in batch:
            u = entry.get("user") or entry
            uid = u.get("userId") or u.get("id")
            if uid:
                out.add(int(uid))
        cursor = data.get("nextPageCursor")
        if not cursor:
            break
        time.sleep(0.35)
    return out


def _sweep_restricted_groups_via_user_roles(
    restricted_specs: Dict[int, Tuple[int, Optional[Set[str]]]],
    candidate_ids: Set[int],
    verbose: bool = False,
) -> List[dict]:
    """
    Fallback for communities whose member list endpoint returns 400/403 to non-members:
    `/v2/users/{uid}/groups/roles` is always public and returns every community & rank for `uid`.
    """
    if not restricted_specs or not candidate_ids:
        return []
    ordered = sorted(candidate_ids)
    if verbose:
        print(
            f"[sync] sweeping {len(ordered)} candidate users via /v2/users/*/groups/roles "
            f"for restricted group(s) {list(restricted_specs.keys())}",
            file=sys.stderr,
        )
    found: List[dict] = []
    rate_gate = [0.0]

    def _check_uid(uid: int) -> List[dict]:
        sess = requests.Session()
        sess.headers.update({"User-Agent": UA, "Accept": "application/json"})
        for attempt in range(5):
            wait = rate_gate[0] - time.time()
            if wait > 0:
                time.sleep(min(wait, 10.0))
            try:
                r = sess.get(USER_GROUP_ROLES_URL.format(user_id=uid), timeout=12)
                if r.status_code == 200:
                    hits = []
                    for entry in r.json().get("data") or []:
                        grp = entry.get("group") or {}
                        role = entry.get("role") or {}
                        gid = grp.get("id")
                        if gid not in restricted_specs:
                            continue
                        min_rank, allowed_names = restricted_specs[gid]
                        rk = int(role.get("rank") or 0)
                        rname = role.get("name") or "Developer"
                        if rk < min_rank:
                            continue
                        if allowed_names and rname not in allowed_names:
                            continue
                        hits.append({
                            "userId": int(uid),
                            "username": None,
                            "displayName": None,
                            "hasVerifiedBadge": False,
                            "role": rname,
                            "rank": rk,
                            "roleId": int(role.get("id") or 0),
                            "groupId": int(gid),
                        })
                    return hits
                if r.status_code == 429:
                    rate_gate[0] = max(rate_gate[0], time.time() + 4.0 + attempt * 2.0)
                    time.sleep(4.0 + attempt * 2.0)
                    continue
                return []
            except requests.RequestException:
                time.sleep(1.0 + attempt)
        return []

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(_check_uid, uid) for uid in ordered]
        for fut in as_completed(futs):
            res = fut.result()
            if res:
                found.extend(res)
    if verbose:
        print(f"[sync] fallback discovered {len(found)} role memberships across restricted group(s)", file=sys.stderr)
    return found


def fetch_group_sources(
    group_sources: List[dict],
    pool: Optional[List[Tuple[str, str]]] = None,
    allow_user_roles_fallback: bool = False,
    seed_candidate_ids: Optional[Set[int]] = None,
    delay: float = 0.25,
    verbose: bool = False,
) -> List[dict]:
    """
    Fetch deduplicated member records from a list of `{groupId, minRank, roles?}` dicts.
    Returns list of dicts compatible with `tracker_scanner.py` (`userId`, `username`, `displayName`,
    `hasVerifiedBadge`, `role`, `rank`, `roleId`, `groupId`).
    """
    if not group_sources:
        return []
    if pool is None:
        pool = _get_all_cookies()
    sessions = _make_sessions(pool)
    members: List[dict] = []
    restricted: Dict[int, Tuple[int, Optional[Set[str]]]] = {}
    req_counter = 0

    for src in group_sources:
        gid = int(src["groupId"])
        min_rank = int(src.get("minRank", 1))
        role_names = set(src["roles"]) if src.get("roles") else None

        roles_data, status = _get_with_pool_failover(
            sessions,
            GROUP_ROLES_URL.format(group_id=gid),
            start_idx=req_counter,
        )
        req_counter += 1
        if status != 200 or not roles_data:
            print(f"[sync] group {gid}: failed to fetch roles (HTTP {status})", file=sys.stderr)
            restricted[gid] = (min_rank, role_names)
            continue

        roles = sorted(roles_data.get("roles") or [], key=lambda r: int(r.get("rank") or 0))
        matched_roles = [
            r for r in roles
            if int(r.get("rank") or 0) >= min_rank
            and (not role_names or r.get("name") in role_names)
        ]

        group_expected = sum(int(r.get("memberCount") or 0) for r in matched_roles)
        group_fetched = 0
        group_blocked = False

        for role in matched_roles:
            rid = int(role["id"])
            rname = role.get("name") or "Developer"
            rrank = int(role.get("rank") or 255)
            expected = int(role.get("memberCount") or 0)
            if expected == 0:
                continue

            cursor = ""
            role_users = 0
            while True:
                params = {"limit": 100, "sortOrder": "Asc"}
                if cursor:
                    params["cursor"] = cursor
                page_data, pstatus = _get_with_pool_failover(
                    sessions,
                    ROLE_USERS_URL.format(group_id=gid, role_id=rid),
                    params=params,
                    start_idx=req_counter,
                )
                req_counter += 1
                if pstatus != 200 or not page_data:
                    print(
                        f"[sync] group {gid} role '{rname}' (rank {rrank}, expected {expected}): "
                        f"HTTP {pstatus}",
                        file=sys.stderr,
                    )
                    if pstatus in (400, 401, 403):
                        group_blocked = True
                    break

                for entry in page_data.get("data") or []:
                    u = entry.get("user") if isinstance(entry.get("user"), dict) else entry
                    uid = u.get("userId") or u.get("id")
                    if not uid:
                        continue
                    members.append({
                        "userId": int(uid),
                        "username": u.get("username"),
                        "displayName": u.get("displayName"),
                        "hasVerifiedBadge": bool(u.get("hasVerifiedBadge", False)),
                        "role": rname,
                        "rank": rrank,
                        "roleId": rid,
                        "groupId": gid,
                    })
                    role_users += 1

                cursor = page_data.get("nextPageCursor")
                if not cursor:
                    break
                time.sleep(delay)

            group_fetched += role_users
            if verbose:
                print(
                    f"[sync] group {gid} role '{rname}' (rank {rrank}): {role_users}/{expected} fetched",
                    file=sys.stderr,
                )
            time.sleep(delay)

        if group_blocked and group_fetched == 0 and group_expected > 0:
            restricted[gid] = (min_rank, role_names)
        elif verbose:
            print(f"[sync] group {gid} total: {group_fetched}/{group_expected} Developer+ members", file=sys.stderr)

    if restricted and allow_user_roles_fallback:
        candidates: Set[int] = set(seed_candidate_ids or set())
        candidates.update(m["userId"] for m in members if m.get("userId"))
        # Include Official Group of Roblox (1200769), Video Stars (4199740), and pool followings
        for pub_gid in (1200769, 4199740):
            candidates.update(_fetch_public_group_ids(pub_gid, sessions))
        for _, ck in (pool or []):
            hdr = {"Cookie": f".ROBLOSECURITY={ck}"}
            mid = _get_my_id(hdr)
            if mid:
                candidates.update(_get_followings(mid, hdr))
        members.extend(_sweep_restricted_groups_via_user_roles(restricted, candidates, verbose=verbose))

    # Deduplicate preserving first-seen rich metadata
    deduped: Dict[int, dict] = {}
    for m in members:
        uid = int(m["userId"])
        if uid not in deduped or (not deduped[uid].get("username") and m.get("username")):
            deduped[uid] = m
    return list(deduped.values())


def write_developer_ids_file(path: Path, all_ids: Set[int], group_counts: Optional[Dict[int, int]] = None) -> None:
    sorted_ids = sorted(int(u) for u in all_ids if int(u) > 0)
    stamp = time.strftime("%Y-%m-%d", time.gmtime())
    summary_parts = [f"group {gid}: {cnt}" for gid, cnt in (group_counts or {}).items()]
    detail = f" ({', '.join(summary_parts)})" if summary_parts else ""
    lines = [
        "# Developer IDs — automatically synced from configured groupSources + manual entries",
        "# One ID per line, \"#\" for comments. Existing/manual IDs are always preserved.",
        f"# Total unique IDs: {len(sorted_ids)}{detail} — synced {stamp}",
        *(str(uid) for uid in sorted_ids),
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync Developer IDs from Roblox group role thresholds")
    ap.add_argument("--tracker", default="developers", help="Tracker id in config/trackers.yaml")
    ap.add_argument("--ids-file", default="config/developer_ids.txt", help="Target developer IDs file")
    ap.add_argument("--extra-from-file", default="", help="Optional second IDs file to union (for git merge persistence)")
    ap.add_argument("--dry-run", action="store_true", help="Print changes without writing file")
    ap.add_argument("--verbose", action="store_true", help="Log per-group and per-role details")
    args = ap.parse_args()

    ids_path = Path(args.ids_file)
    existing = set(load_user_ids(str(ids_path))) if ids_path.exists() else set()

    if args.extra_from_file:
        extra_path = Path(args.extra_from_file)
        extra_ids = set(load_user_ids(str(extra_path))) if extra_path.exists() else set()
        merged = existing | extra_ids
        added = sorted(merged - existing)
        if not added:
            print(f"[sync] {ids_path} already contains all {len(existing)} IDs (0 new)")
            return 0
        if not args.dry_run:
            write_developer_ids_file(ids_path, merged)
        print(f"[sync] persisted +{len(added)} new IDs into {ids_path} ({len(existing)} -> {len(merged)}): {added}")
        return 0

    cfg_data = yaml.safe_load(Path("config/trackers.yaml").read_text(encoding="utf-8")) or {}
    tcfg = next((t for t in cfg_data.get("trackers", []) if t.get("id") == args.tracker), None)
    if not tcfg:
        print(f"[sync] tracker '{args.tracker}' not found in config/trackers.yaml", file=sys.stderr)
        return 1

    group_sources = tcfg.get("groupSources") or []
    if not group_sources:
        print(f"[sync] no groupSources configured on '{args.tracker}'; keeping {len(existing)} IDs")
        return 0

    pool = _get_all_cookies()
    print(f"[sync] starting sync with {len(pool)} cookie account(s), {len(existing)} existing IDs in {ids_path}", file=sys.stderr)
    synced_members = fetch_group_sources(
        group_sources,
        pool=pool,
        allow_user_roles_fallback=True,
        seed_candidate_ids=existing,
        verbose=args.verbose,
    )

    by_group: Dict[int, int] = {}
    for m in synced_members:
        gid = m.get("groupId")
        if gid:
            by_group[int(gid)] = by_group.get(int(gid), 0) + 1

    synced_ids = {int(m["userId"]) for m in synced_members}
    merged = existing | synced_ids
    new_members = sorted(
        [m for m in synced_members if int(m["userId"]) not in existing],
        key=lambda m: int(m["userId"]),
    )

    print(
        f"[sync] fetched {len(synced_ids)} unique Developer+ users across {len(group_sources)} group(s) "
        f"| existing={len(existing)} | newly_added={len(new_members)} | merged_total={len(merged)}"
    )
    for m in new_members:
        label = f"{m.get('username') or 'user'} ({m['userId']})"
        print(f"  + {label} — group {m.get('groupId')} [{m.get('role')} rank {m.get('rank')}]")

    if new_members and not args.dry_run:
        write_developer_ids_file(ids_path, merged, group_counts=by_group)
        print(f"[sync] wrote {len(merged)} sorted IDs to {ids_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
