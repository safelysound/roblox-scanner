#!/usr/bin/env python3
"""Read-only audit of the Admin tracker and its six notifier shards.

Checks the live Roblox group roster against the notifier's cached roster, state branches,
follower routing, and a fresh presence sweep. Prints compact GitHub check annotations and
writes admin-audit.json for local inspection. Does not post to Discord or write state.
"""
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import join_notifier as jn  # noqa: E402
from developer_scanner import _get_all_cookies, _profile_shows_hunt, fetch_presences  # noqa: E402

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
from tracker_scanner import fetch_group_members, resolve_universe_id  # noqa: E402

GROUP_ID = 1200769
SHARDS = 6
NOTIFIER = "admin"


def state_branch(shard):
    return f"notifier-state-{NOTIFIER}-{shard}"


def load_state_branch(shard):
    branch = state_branch(shard)
    path = f"notifier_{NOTIFIER}-{shard}.json"
    subprocess.run(
        ["git", "fetch", "-q", "--depth=1", "origin",
         f"refs/heads/{branch}:refs/remotes/origin/{branch}"],
        check=True,
    )
    raw = subprocess.check_output(["git", "show", f"origin/{branch}:{path}"])
    return json.loads(raw)


def sample(values, limit=15):
    values = sorted(values)
    return values[:limit] + ([f"... {len(values) - limit} more"] if len(values) > limit else [])


def main():
    cfg = yaml.safe_load((ROOT / "config/trackers.yaml").read_text(encoding="utf-8")) or {}
    settings = cfg.get("settings") or {}
    tracker = next((t for t in cfg.get("trackers") or [] if t.get("id") == NOTIFIER), {})
    notifier = (cfg.get("notifiers") or {}).get(NOTIFIER) or {}

    place_id = int(settings.get("targetPlaceId"))
    universe_id = int(settings.get("targetUniverseId"))
    target_ids = {place_id, universe_id}
    target_sig = f"{place_id}:{universe_id}"

    result = {
        "config": {
            "tracker": tracker,
            "notifier": notifier,
            "targetPlaceId": place_id,
            "targetUniverseId": universe_id,
            "targetGameName": settings.get("targetGameName"),
        },
        "checks": {},
        "issues": [],
    }

    def check(name, ok, detail=""):
        result["checks"][name] = {"ok": bool(ok), "detail": detail}
        if not ok:
            result["issues"].append(f"{name}: {detail}")

    # ---------------------------------------------------------------- live roster
    try:
        meta = requests.get(
            f"https://groups.roblox.com/v1/groups/{GROUP_ID}",
            timeout=15,
            headers={"User-Agent": UA},
        )
        meta.raise_for_status()
        member_count = int(meta.json().get("memberCount") or 0)
    except Exception as exc:  # noqa: BLE001
        member_count = 0
        check("group_metadata", False, f"{type(exc).__name__}: {exc}")

    roster = fetch_group_members(GROUP_ID, strict=True)
    roster_ids = sorted({int(m["userId"]) for m in roster if m.get("userId")})
    roster_names = {int(m["userId"]): m.get("username") or m.get("name") for m in roster if m.get("userId")}
    role_counts = Counter(m.get("role") or "?" for m in roster)
    result["group"] = {
        "id": GROUP_ID,
        "memberCount": member_count,
        "fetched": len(roster),
        "unique": len(roster_ids),
        "roles": dict(role_counts.most_common(12)),
    }
    check("roster_fetched", len(roster_ids) >= max(1, member_count) * 0.9,
          f"fetched {len(roster_ids)} of {member_count}")
    check("roster_unique", len(roster_ids) == len(roster), f"{len(roster_ids)} unique of {len(roster)} rows")

    # ---------------------------------------------------------------- notifier state
    states = {}
    state_errors = []
    for shard in range(SHARDS):
        try:
            states[shard] = load_state_branch(shard)
        except Exception as exc:  # noqa: BLE001
            state_errors.append(f"{shard}: {type(exc).__name__}")
    check("state_branches", len(states) == SHARDS, f"{len(states)}/{SHARDS} loaded {state_errors}")

    state_members = set()
    hunt = set()
    last_job = {}
    last_place = {}
    followers = {}
    followers_accts = set()
    shard_rows = []
    for shard, st in sorted(states.items()):
        shard_members = {int(u) for u in st.get("members") or []}
        shard_hunt = {int(u) for u in (st.get("hunt") or {})}
        state_members |= shard_members
        hunt |= shard_hunt
        for uid, job in (st.get("last_job") or {}).items():
            last_job[int(uid)] = job
        for uid, place in (st.get("last_place") or {}).items():
            last_place[int(uid)] = place
        for uid, key in (st.get("followers") or {}).items():
            followers[int(uid)] = key
        followers_accts |= set(st.get("followers_accts") or [])
        shard_rows.append({
            "shard": shard,
            "shards_key": st.get("shards"),
            "target": st.get("target"),
            "members": len(shard_members),
            "hunt": len(shard_hunt),
            "updated_age_s": round(time.time() - float(st.get("updated") or 0), 1),
        })

    result["notifier"] = {
        "shards": shard_rows,
        "state_members": len(state_members),
        "hunt_total": len(hunt),
        "hunt_in_roster": len(hunt & set(roster_ids)),
        "hunt_not_in_roster": sample(hunt - set(roster_ids)),
        "follower_map": len(followers),
        "follower_accounts": sorted(followers_accts),
        "hunt_unfollowed": len(hunt - set(followers)),
        "hunt_unfollowed_ids": sample(hunt - set(followers)),
        "hunt_unfollowed_names": {str(u): roster_names.get(u) for u in sorted(hunt - set(followers))[:15]},
        "last_place_wrong": sample({u for u, p in last_place.items() if p not in target_ids}),
        "last_job_missing": sample(hunt - set(last_job)),
        "missing_from_state": sample(set(roster_ids) - state_members),
        "extra_in_state": sample(state_members - set(roster_ids)),
    }
    check("state_members_cover_roster", not (set(roster_ids) - state_members),
          f"{len(set(roster_ids) - state_members)} roster members missing from state")
    check("state_has_no_extra_members", not (state_members - set(roster_ids)),
          f"{len(state_members - set(roster_ids))} stale/extra IDs in state")
    check("hunt_in_roster", hunt <= set(roster_ids),
          f"{len(hunt - set(roster_ids))} hunt IDs no longer in the group")
    check("last_place", all(p in target_ids for p in last_place.values()),
          f"{len([p for p in last_place.values() if p not in target_ids])} wrong last_place values")
    check("state_target", all(st.get("target") == target_sig for st in states.values()),
          f"expected {target_sig}")
    check("state_shards", all(int(st.get("shards") or 0) == SHARDS for st in states.values()),
          "each state file must record shards=6")

    # ---------------------------------------------------------------- presence sweep
    pool = _get_all_cookies()
    # fetch_presences paces one 50-user batch at a time (0.35s), rotates through the cookie pool,
    # and does follower/hidden passes. jn.poll is built for ~475-ID shards and rate-limits hard
    # when handed the whole 2,852-member roster in one runner.
    best = fetch_presences(roster_ids)
    ok = bool(best)
    complete = len(best) >= len(roster_ids) * 0.95

    live_target = set()
    hidden = set()
    other_game = set()
    offline = set()
    website = set()
    studio = set()
    for uid, presence in best.items():
        ptype = presence.get("userPresenceType")
        if ptype == 2 and jn.is_hunt(presence, place_id, universe_id):
            live_target.add(int(uid))
        elif ptype == 2 and not (presence.get("placeId") or presence.get("rootPlaceId") or presence.get("universeId")):
            hidden.add(int(uid))
        elif ptype == 2:
            other_game.add(int(uid))
        elif ptype == 0:
            offline.add(int(uid))
        elif ptype == 1:
            website.add(int(uid))
        elif ptype == 3:
            studio.add(int(uid))

    # Fully-hidden InGame presence has no place/universe, so is_hunt() cannot classify it.
    # The tracker falls back to a profile-page scrape for followed users; mirror that here to
    # determine whether those hidden admins are in the target game or somewhere else.
    cookie_by_key = dict(pool)
    profile_target = set()
    for uid in sorted(hidden):
        key = followers.get(uid)
        raw = cookie_by_key.get(key) if key else None
        if not raw:
            continue
        if _profile_shows_hunt(
            uid,
            {},
            target_place_id=place_id,
            target_universe_id=universe_id,
            prefer_cookie=f".ROBLOSECURITY={raw}",
        ):
            profile_target.add(uid)

    coverage = len(best) / max(1, len(roster_ids))
    result["presence"] = {
        "accounts": len(pool),
        "queried": len(roster_ids),
        "responses": len(best),
        "coverage": round(coverage, 4),
        "poll_ok": bool(ok),
        "complete": bool(complete),
        "in_target": len(live_target),
        "in_target_ids": sample(live_target),
        "in_target_names": {str(u): roster_names.get(u) for u in sorted(live_target)[:15]},
        "hidden": len(hidden),
        "hidden_ids": sample(hidden),
        "hidden_names": {str(u): roster_names.get(u) for u in sorted(hidden)[:15]},
        "hidden_in_hunt": len(hidden & hunt),
        "hidden_unfollowed": len(hidden - set(followers)),
        "hidden_unfollowed_ids": sample(hidden - set(followers)),
        "hidden_unfollowed_names": {str(u): roster_names.get(u) for u in sorted(hidden - set(followers))[:15]},
        "profile_target": len(profile_target),
        "profile_target_ids": sample(profile_target),
        "profile_target_names": {str(u): roster_names.get(u) for u in sorted(profile_target)[:15]},
        "profile_target_not_in_hunt": len(profile_target - hunt),
        "other_game": len(other_game),
        "offline": len(offline),
        "website": len(website),
        "studio": len(studio),
    }
    check("presence_coverage", coverage >= 0.95, f"answered for {len(best)}/{len(roster_ids)} ({coverage:.1%})")
    check("live_target_in_state", live_target <= hunt,
          f"{len(live_target - hunt)} live target players missing from notifier hunt")
    check("hidden_profile_target_in_state", profile_target <= hunt,
          f"{len(profile_target - hunt)} profile-confirmed target players missing from notifier hunt")
    result["live_vs_hunt"] = {
        "live_not_in_hunt": sample(live_target - hunt),
        "hunt_not_currently_visible": len(hunt - live_target),
    }

    # ---------------------------------------------------------------- config / game
    try:
        resolved_universe = resolve_universe_id(place_id)
    except Exception as exc:  # noqa: BLE001
        resolved_universe = None
        check("place_resolution", False, f"{type(exc).__name__}: {exc}")
    else:
        check("place_resolution", resolved_universe == universe_id,
              f"place {place_id} -> universe {resolved_universe}, configured {universe_id}")
    try:
        game_name = jn.resolve_game_name(universe_id)
    except Exception as exc:  # noqa: BLE001
        game_name = None
        check("game_name", False, f"{type(exc).__name__}: {exc}")
    else:
        check("game_name", game_name == settings.get("targetGameName"),
              f"resolved {game_name!r}, configured {settings.get('targetGameName')!r}")
    result["resolved_game_name"] = game_name
    check("notifier_links_tracker", notifier.get("tracker") == NOTIFIER,
          f"notifier.tracker={notifier.get('tracker')!r}")
    check("notifier_shards", int(notifier.get("shards") or 0) == SHARDS,
          f"notifier.shards={notifier.get('shards')!r}")
    check("tracker_group", int(tracker.get("groupId") or 0) == GROUP_ID,
          f"tracker.groupId={tracker.get('groupId')!r}")

    # ---------------------------------------------------------------- real tracker scan output
    tracker_scan_path = ROOT / "admin-tracker-scan.json"
    if tracker_scan_path.exists():
        try:
            scan = json.loads(tracker_scan_path.read_text(encoding="utf-8"))
            target_players = scan.get("target_game_players") or []
            tracker_target = {int(p["userId"]) for p in target_players if p.get("userId")}
            meta = scan.get("meta") or {}
            result["tracker_scan"] = {
                "summary": scan.get("summary"),
                "meta": {k: meta.get(k) for k in (
                    "groupId", "targetPlaceId", "targetUniverseId", "targetGameName",
                    "total_members", "full_group_size", "timestamp", "shard_info",
                )},
                "target_count": len(tracker_target),
                "target_ids": sample(tracker_target),
                "target_names": {str(u): roster_names.get(u) for u in sorted(tracker_target)[:15]},
                "target_not_in_hunt": len(tracker_target - hunt),
                "target_not_in_hunt_ids": sample(tracker_target - hunt),
                "target_not_in_hunt_names": {str(u): roster_names.get(u) for u in sorted(tracker_target - hunt)[:15]},
                "audit_target_not_in_tracker": sample((live_target | profile_target) - tracker_target),
                "tracker_not_in_audit": sample(tracker_target - (live_target | profile_target)),
            }
            check("tracker_scan_roster", int(meta.get("total_members") or 0) == len(roster_ids),
                  f"tracker meta total_members={meta.get('total_members')} roster={len(roster_ids)}")
            check("tracker_scan_target", bool(meta.get("targetPlaceId") == place_id
                                              and meta.get("targetUniverseId") == universe_id),
                  f"tracker target {meta.get('targetPlaceId')}:{meta.get('targetUniverseId')} "
                  f"expected {place_id}:{universe_id}")
            check("tracker_scan_target_in_state", tracker_target <= hunt,
                  f"{len(tracker_target - hunt)} tracker target players missing from notifier hunt")
        except Exception as exc:  # noqa: BLE001
            check("tracker_scan_parse", False, f"{type(exc).__name__}: {exc}")
    else:
        check("tracker_scan_present", False, "admin-tracker-scan.json was not produced")

    result["summary"] = {
        "roster": len(roster_ids),
        "state_members": len(state_members),
        "hunt": len(hunt),
        "currently_in_target": len(live_target),
        "unfollowed_hunt": len(hunt - set(followers)),
        "issues": len(result["issues"]),
        "tracker_target": (result.get("tracker_scan") or {}).get("target_count", 0),
    }

    (ROOT / "admin-audit.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    print("::notice title=Admin audit summary::" + json.dumps(result["summary"], separators=(",", ":")))
    print("::notice title=Admin audit group/state::" + json.dumps({
        "group": result["group"],
        "notifier": {k: result["notifier"][k] for k in (
            "state_members", "hunt_total", "hunt_in_roster", "hunt_not_in_roster",
            "follower_map", "follower_accounts", "hunt_unfollowed", "hunt_unfollowed_ids",
            "hunt_unfollowed_names", "last_place_wrong", "last_job_missing",
            "missing_from_state", "extra_in_state",
        )},
        "shards": shard_rows,
    }, separators=(",", ":")))
    print("::notice title=Admin audit presence::" + json.dumps(result["presence"], separators=(",", ":")))
    for issue in result["issues"]:
        print(f"::warning title=Admin audit issue::{issue}")
    print(json.dumps(result, indent=2))
    return 0 if not result["issues"] else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print("::error title=Admin audit crash::" + tb.replace("\n", " | ")[-3500:])
        raise
