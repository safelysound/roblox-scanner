# Roblox Group Scanner — `Official Group of Roblox` → `The Hunt`

Scans **all 2,771 members** of [Official Group of Roblox (1200769)](https://www.roblox.com/communities/1200769/Official-Group-of-Roblox) and prioritizes who is playing [The Hunt: Roblox 20 (74205509034203)](https://www.roblox.com/games/74205509034203/The-Hunt-Roblox-20). Also lists everyone else currently in *any* game.

**Public repo / GitHub Actions ready - 0$**

## Quick Start (Local, 85s)

```bash
pip install requests tqdm
python roblox_group_scanner.py
# → terminal: 🎯 TARGET + 🎮 OTHER + roblox_scan_results.json
```

## GitHub Actions (Public, Free, ~30s via 3 shards)

This repo is ready for public GitHub Actions. Free for public repos (unlimited minutes).

1. **Create a new public GitHub repo** and push these files:
   ```
   roblox_group_scanner.py
   merge_shards.py
   .github/workflows/scan.yml
   ```

2. **Go to Actions → "Roblox Group Scan" → Run workflow**
   - Inputs: `group_id` (1200769), `place_id` (74205509034203), `max_members` (empty = all)
   - It runs 3 shards in parallel (each on a different runner IP = 3 * 60/min = 180/min) -> **~30s** for full 2,771
   - Artifacts: `shard-0/1/2` + `final-results` (JSON + CSV)

3. **(Optional) Add Discord webhook later:**
   - GitHub → Settings → Secrets and variables → Actions → New secret → `DISCORD_WEBHOOK` = `https://discord.com/api/webhooks/...`
   - Re-run workflow - the `merge` job will auto-post embeds to Discord. No code change. Leave it empty for now and it gracefully skips.

**How many runs?** `~1.5 min per full scan`. Public = unlimited. Private free = 2,000 min/mo = ~1,333 scans.

## How it stays fast *and* safe (no 429)

* `Groups: 100/page` = 28 pages
* `Presence: 50/batch` = 56 batches (100 fails with 400)
* **84 req total vs 2,771 naive = 33x fewer**
* Live-tuned: `1.1s` delay = stays under `60 req/60s` header (`x-ratelimit-limit: 60, 60;w=60`). Auto-backoff on 429.
* **3 shards = 3 IPs** in Actions, so 84 req in 30s is safe (each runner ~28 req)

## Files

| File | Purpose |
|------|---------|
| `roblox_group_scanner.py` | Main scanner - single or sharded (`--shard 0 --shards 3`) + Discord (`--webhook` / `DISCORD_WEBHOOK` env) |
| `merge_shards.py` | Merges `shard_*.json` -> `roblox_scan_results.json/csv` |
| `.github/workflows/scan.yml` | 3-shard matrix + merge + Discord |
| `cookies.example.txt` | Template if you *want* to use ROBLOSECURITY for even faster (optional) |
| `USAGE.md` | Deep dive on rate limits, local usage |
| `.gitignore` | Ignores secrets |

## Local Sharding Test

```bash
python roblox_group_scanner.py --max-members 90 --shard 0 --shards 3 --json shard_0.json
python roblox_group_scanner.py --max-members 90 --shard 1 --shards 3 --json shard_1.json
python roblox_group_scanner.py --max-members 90 --shard 2 --shards 3 --json shard_2.json
python merge_shards.py shard_*.json --json roblox_scan_results.json --csv out.csv
```

## Discord Webhook (Future)

Already wired. Two ways:

```bash
# CLI
python roblox_group_scanner.py --webhook https://discord.com/api/webhooks/xxx

# Env / Secret
export DISCORD_WEBHOOK=https://discord.com/api/webhooks/xxx
python roblox_group_scanner.py

# GitHub: add secret DISCORD_WEBHOOK - merge job will post combined results
```

In Actions it only sends *once* from the merge job (not 3x per shard).

## Security

* Public repo is fine - code has no secrets. Don't hardcode `.ROBLOSECURITY` - use `Settings > Secrets > DISCORD_WEBHOOK` if you add it. `cookies.txt` is git-ignored.
* Presence `placeId:null` for some InGame users = Roblox privacy (Friends only) - they are counted as "Other games" but can't be mapped to target. Expected after Apr 2025 fix.

## License

MIT - do what you want, respect Roblox ToS and rate limits.
