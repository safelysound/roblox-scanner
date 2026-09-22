# Roblox Tracker — `Official Group` / `Video Stars` / `Developers` → `The Hunt`

Single config, single scanner for all 3 trackers. Detects who is playing [The Hunt: Roblox 20 (74205509034203)](https://www.roblox.com/games/74205509034203/The-Hunt-Roblox-20) and edits the same Discord embed every 5 min.

**Public repo / GitHub Actions ready - 0$ (cloud) + optional home run for follows**

## Quick Start (Local, 30s)

```bash
pip install requests pyyaml
python tracker_scanner.py --tracker developers --max-members 10 --verbose
python tracker_scanner.py --tracker video-stars --max-members 10 --verbose
python tracker_scanner.py --tracker admin --max-members 10 --verbose
# -> JSON: target_game_players (Hunt) + other_game_players + offline_or_website
```

## Config — Add a Tracker = 1 Line (no workflow edit)

Edit `config/trackers.yaml`:

```yaml
trackers:
  - id: admin
    name: Admin Tracker
    groupId: 1200769
    color: "#305778"
    emoji: "<:roblox:1551329066337050754>"
    webhookSecret: DISCORD_WEBHOOK

  - id: video-stars
    name: Video Star Tracker
    groupId: 4199740
    extraIds: [1377033267]  # extra user even if not in group
    color: "#F9E999"
    emoji: "<:verified:1551329029783691334>"
    webhookSecret: DISCORD_WEBHOOK_VIDEO_STARS

  - id: developers
    name: Developer Tracker
    idsFile: config/developer_ids.txt  # one ID per line, # comments
    color: "#000000"
    emoji: "<:developer:1551388816324169838>"
    webhookSecret: DISCORD_WEBHOOK_DEVELOPERS
```

Edit `config/developer_ids.txt` to add/remove IDs — next 5-min run picks it up automatically.

## GitHub Actions (Unified, 1 Workflow)

`Actions → Trackers → Run workflow`:

- `tracker: all | admin | video-stars | developers` (default `all`)
- `place_id: 74205509034203`
- `max_members: 10` for test (empty = all)

Runs 9 shards in parallel (3 trackers × 3 shards), merges, edits same Discord message per tracker. Free for public repos.

**Secrets (Settings → Secrets → Actions):**

- `ROBLOX_COOKIE` / `ROBLOSECURITY` / `ROBLOSECURITY_1..5` — alt cookies (6-pool, 6× faster, reveals hidden Hunt via Friends-only)
- `DISCORD_WEBHOOK` (Admin) / `DISCORD_WEBHOOK_VIDEO_STARS` / `DISCORD_WEBHOOK_DEVELOPERS`

## Follow (Home IP Only - 0$)

Cloud (GitHub) gets `403 Challenge` when following — by design (datacenter IP). For hidden `InGame` with `placeId:null` to show Hunt, follow once from home:

```bash
pip install requests pyyaml
export ROBLOX_COOKIE="_|WARNING...|"
python follow.py --tracker developers --dry-run        # counts
python follow.py --tracker developers                  # 103 ~20 min (12s + backoff, 6-pool)
python follow.py --tracker video-stars --pool 6        # 614 ~20 min
python follow.py --tracker all --pool 6                # 3491 ~2h (shards across 6 alts, bypasses 1k follow cap)
# Or cloud dry-run: Actions → Follow → tracker=developers, dry_run=true
```

After one home bulk follow, cloud 5-min scans see Hunt without needing residential runner.

## Files

| File | Purpose |
|------|---------|
| `tracker_scanner.py` | **Universal scanner** — group + ID list + extraIds union, sharded `--shard 0/3`, 6-cookie pool, pooled `isFollowing` + profile scrape |
| `follow.py` | Unified follow (`--tracker all|admin|video-stars|developers --pool 6 --dry-run`) — thin wrapper over `follow_all_trackers.py` logic |
| `config/trackers.yaml` | Single source of truth — add tracker without touching workflow |
| `config/developer_ids.txt` | Developer list (103) — live reload each 5 min |
| `.github/workflows/trackers.yml` | **Single workflow** — 9 shards + 3 merges + 3 Discord edits every 5 min |
| `.github/workflows/follow-devs.yml` | Manual follow (residential) |
| `merge_shards.py` | Merges `shard_*.json` → `roblox_scan_results*.json/csv` |
| `discord_updater.py` | Edits same Discord embed (Hunt + Unknown only, spec_keep) |

## Why Hunt shows / hides

Presence `placeId:null` for `InGame` = Roblox privacy (Friends only). If alt follows that user, presence returns `74205509034203` and Discord shows `[The Hunt: Roblox 20](link) (Must Follow to Join)`. `Unknown` = `InGame` hidden not following.

## License

MIT
