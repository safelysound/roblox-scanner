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

## Join notifiers (Developers / Admins / Video Stars)

`join_notifier.py` posts to Discord within about a minute of someone in a tracker joining The Hunt: an embed (title, colour, role ping and emoji per tracker, avatar thumbnail, relative timestamp, server ID in inline code) with a **Join ↗** button that opens the Roblox client on that exact server.

- Runs as the `notify` job in the workflow, one per tracker in `notifiers:` of `config/trackers.yaml`. Each cron dispatch polls presence for ~4.5 minutes, right up to the next tick; who was already in The Hunt is carried between runs in a small state artifact.
- Setup: the Actions secret named by `webhookSecret` (currently `DISCORD_WEBHOOK_DEVELOPER_JOINS` for all three; point any notifier at its own secret to use a different channel).
- Try it: *Run workflow* with `notifier_mode = test` (posts a `[TEST]` sample per tracker, no role ping) or `dry-run` (polls 60s, posts nothing). The run's annotations show the result, e.g. whether Discord accepted the button.
- **Join button:** Discord only opens `http(s)` links, so it is a link button to `https://www.roblox.com/games/start?placeId=…&gameInstanceId=…`, which launches Roblox (a `roblox://` link can't be clicked in Discord). Webhooks can't send interactive buttons (Discohook-style `custom_id` / `flow` buttons need a bot). If Discord ever drops the button, the message gets the same link as text.
- **Detection:** *joined* = not in The Hunt in the last 3 minutes. The first run after downtime records who's already there without announcing. It waits up to 30s for the server ID to appear before posting.
- **Limits:** the server ID (and the button) only exist when a pool account can see that player's game (followed + their join privacy allows it); otherwise the message says `server not visible`. People hidden from every pool account can't be detected. Roblox's own presence lags a few seconds to tens of seconds.
- **Big groups (Admin, Video Stars):** one account, rotating each poll, sweeps the whole group; only users who are "in-game, location hidden" are re-checked through the other accounts. Someone who appears offline to everyone except their followers is found more slowly than in the Developer list, which queries every account. Group member lists are cached for an hour.
- Run it continuously on your own machine instead: `python join_notifier.py --notifier developers --duration 0`.

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
- `DISCORD_WEBHOOK` (Admin), `DISCORD_WEBHOOK_VIDEO_STARS`, `DISCORD_WEBHOOK_DEVELOPERS`, `DISCORD_WEBHOOK_DEVELOPER_JOINS` (join notifiers)

## Files

| File | Purpose |
|------|---------|
| `tracker_scanner.py` | Scanner: group + ID list + extra IDs, sharded, cookie pool, follow detection |
| `developer_scanner.py` | Presence / cookie / profile helpers used by `tracker_scanner.py` (must stay; can also run standalone) |
| `merge_shards.py` | Merges `shard_*.json` into one results file |
| `discord_updater.py` | Builds and edits the Discord embed (Hunt + Unknown only) |
| `join_notifier.py` | Real-time "joined The Hunt" Discord notifier (Developers / Admins / Video Stars) |
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
