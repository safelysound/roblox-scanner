# Usage — Unified Tracker

See README.md for quick start. Details:

## Local

```bash
pip install requests pyyaml
# All 3
python tracker_scanner.py --tracker admin --json out_admin.json --verbose
python tracker_scanner.py --tracker video-stars --json out_vs.json --verbose
python tracker_scanner.py --tracker developers --json out_dev.json --verbose
# Sharded (like CI)
python tracker_scanner.py --tracker developers --shard 0 --shards 3 --json shard_0.json
python tracker_scanner.py --tracker developers --shard 1 --shards 3 --json shard_1.json
python tracker_scanner.py --tracker developers --shard 2 --shards 3 --json shard_2.json
python merge_shards.py shard_*.json --json roblox_scan_results_developers.json --csv out.csv
```

## GitHub Actions

Single workflow `.github/workflows/trackers.yml` — no need to edit for new trackers, just `config/trackers.yaml`.

## Follow

See `follow.py --help`. Home IP required for actual follow.

## Old files (removed)

`roblox_group_scanner.py` / `developer_scanner.py` / `follow_all.py` / `follow_all_trackers.py` / `config/video_star_extra_ids.txt` → replaced by `tracker_scanner.py` + `follow.py` + `config/trackers.yaml` (extraIds inline).
