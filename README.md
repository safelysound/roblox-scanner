# Roblox Tracker — `Official Group` / `Video Stars` / `Developers` → `The Hunt`

One config, one scanner, three trackers. Detects who is playing [The Hunt: Roblox 20 (74205509034203)](https://www.roblox.com/games/74205509034203/The-Hunt-Roblox-20) and keeps one Discord embed per tracker up to date, editing the same message every ~5 minutes. Runs entirely on GitHub Actions (free for public repos).

## Quick start (local)

```bash
pip install -r requirements.txt
python tracker_scanner.py --tracker developers --max-members 10 --verbose
python tracker_scanner.py --tracker video-stars --max-members 10 --verbose
python tracker_scanner.py --tracker admin --max-members 10 --verbose --json out_admin.json
# -> JSON: target_game_players (Hunt) + other_game_players + offline_or_website
```

Sharded, like CI does it:

```bash
for i in 0 1 2; do
  python tracker_scanner.py --tracker admin --shard $i --shards 3 --json shard_admin_$i.json
done
python merge_shards.py shard_admin_*.json --json roblox_scan_results.json
python discord_updater.py roblox_scan_results.json --title "Admin Tracker" --dry-run   # preview the embed
```

Cookies are read from environment variables (see `cookies.example.txt`); without any, the scan still works but hidden presence stays hidden.

## Developer join notifier

`developer_notifier.py` posts to Discord within about a minute of a Developer joining The Hunt:

> **A Developer joined The Hunt!** — `<emoji> [Display (username)](profile) has joined **The Hunt**!` plus the server link / job ID in a code block, with a role ping above it.

- Runs as the `notify-developers` job in the workflow. Each cron dispatch polls presence every ~20s for ~4.5 minutes, right up to the next tick; who was already in The Hunt is carried between runs in a small state artifact.
- Setup: add the Actions secret `DISCORD_WEBHOOK_DEVELOPER_JOINS` (use a different channel/webhook than the tracker embed). Role, emoji and code-block format (`link` / `id` / `both`) are under `notifiers:` in `config/trackers.yaml`.
- Try it: *Run workflow* with `notifier_mode = test` (posts one `[TEST]` sample, no role ping) or `dry-run` (polls 60s, posts nothing).
- Behavior: a Developer counts as *joined* if they weren't in The Hunt in the last 3 minutes (short flickers and server hops don't re-announce). The first run after downtime records who's already there without announcing. It waits up to 30s for the server ID to appear before posting.
- Limits: the server ID is only visible to a pool account that can see that player's game (followed + join privacy allows it); otherwise the message says the server isn't visible. Developers whose game is hidden from every pool account can't be detected. Roblox's own presence lags by a few seconds to tens of seconds.
- Run it continuously on your own machine instead: `python developer_notifier.py --duration 0`.

## Config — add a tracker without touching the workflow

Everything lives in [`config/trackers.yaml`](config/trackers.yaml): group ID and/or ID file, extra IDs, shard count, embed title/colour/emoji, webhook secret name, results file and message-id file. The workflow builds its job matrix from that file, so a new tracker is one YAML block plus its webhook secret. (For *manual* runs, also add the id to the `tracker` choice list in the workflow.)

Edit `config/developer_ids.txt` (one ID per line, `#` comments) to change the developer list; the next run picks it up.

## GitHub Actions

`.github/workflows/trackers.yml` runs on `workflow_dispatch` (an external cron hits it every ~5 minutes) and on pushes to `main`:

1. **plan** — reads `config/trackers.yaml`, builds the tracker/shard matrix.
2. **scan** — one job per tracker shard (`tracker_scanner.py`).
3. **merge** — merges shards, edits the Discord embed, commits a new message id if one was created. If a shard is missing the merge is skipped and the previous embed stays (a partial merge would silently drop people).
4. **follow** — best-effort `follow_unknown.py`, time-boxed and serialized per tracker.

Manual run: *Actions → Trackers → Run workflow* (`tracker`, `place_id`, `max_members` for quick tests).

**Secrets** (Settings → Secrets and variables → Actions):

- `ROBLOX_COOKIE`, `ROBLOSECURITY`, `ROBLOSECURITY_1` … `ROBLOSECURITY_5` — cookie pool (more cookies = more presence requests per minute)
- `DISCORD_WEBHOOK` (Admin), `DISCORD_WEBHOOK_VIDEO_STARS`, `DISCORD_WEBHOOK_DEVELOPERS`, `DISCORD_WEBHOOK_DEVELOPER_JOINS` (join notifier)

## Files

| File | Purpose |
|------|---------|
| `tracker_scanner.py` | Scanner: group + ID list + extra IDs, sharded, cookie pool, follow detection |
| `developer_scanner.py` | Presence / cookie / profile helpers used by `tracker_scanner.py` (must stay; can also run standalone) |
| `merge_shards.py` | Merges `shard_*.json` into one results file |
| `discord_updater.py` | Builds and edits the Discord embed (Hunt + Unknown only) |
| `developer_notifier.py` | Real-time "a Developer joined The Hunt" Discord notifier |
| `follow_unknown.py` | Follows accounts whose status shows as "Unknown" so the game becomes visible |
| `config/trackers.yaml` | Tracker definitions (single source of truth) |
| `config/developer_ids.txt` | Developer ID list |
| `.github/workflows/trackers.yml` | The workflow |
| `.github/discord_message_id*.txt` | Which Discord message each tracker edits (committed by the workflow) |

## Why Hunt shows / hides

Presence `placeId: null` while `InGame` means the user restricts who can see the game (Roblox privacy). If a pool account follows that user, presence returns `74205509034203` and the embed shows `[The Hunt: Roblox 20](link) (Must Follow to Join)`. `Unknown` = in a game but hidden, and not followed yet.

Note: GitHub's datacenter IPs often get a `403 Challenge` when following, so the cloud follow step is best-effort.

## Security notes

- Never commit cookies; use Actions secrets or env vars. Logs of public repos are public, so the scripts never print cookie fragments or account info.
- Rotate a cookie/webhook immediately if it is ever exposed.

## License

MIT
