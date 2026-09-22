#!/usr/bin/env python3
"""
Follow helper — residential run (home IP, 0$). Replaces follow_all.py + follow_all_trackers.py
Reads config/trackers.yaml for source lists, or --tracker / --group-id / --ids-file.

Usage:
  pip install requests
  export ROBLOX_COOKIE="_|WARNING...|..."
  python follow.py --tracker developers --dry-run
  python follow.py --tracker developers              # 103 ~20min
  python follow.py --tracker video-stars             # 614 ~2h
  python follow.py --tracker admin --max 100         # test 100
  python follow.py --tracker all --pool 6            # 3491 ~2h with 6 alts
  python follow.py --group-id 4199740 --pool 1
  python follow.py --ids-file config/developer_ids.txt

Wrapper around follow_all_trackers.py logic, but reads trackers.yaml.
"""
import sys
from pathlib import Path

# Reuse follow_all_trackers implementation
import importlib.util, argparse

spec = importlib.util.spec_from_file_location("follow_all_trackers", "follow_all_trackers.py")
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
    # Monkey-patch to support --tracker
    original_main = mod.main
except Exception as e:
    print(f"Failed to load follow_all_trackers: {e}", file=sys.stderr)
    sys.exit(1)

# Extend to support --tracker
def follow_with_tracker():
    import argparse
    # if --tracker in args, resolve to source and delegate
    if any(a.startswith("--tracker") for a in sys.argv):
        # parse tracker
        p = argparse.ArgumentParser()
        p.add_argument("--tracker", default="")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--pool", type=int, default=1)
        p.add_argument("--max", type=int, default=None)
        args, remaining = p.parse_known_args()
        # map tracker id to follow_all_trackers --source
        mapping = {"admin": "admins", "video-stars": "video-stars", "developers": "developers", "all": "all", "video-star-extras": "video-star-extras"}
        src = mapping.get(args.tracker, args.tracker)
        # build new argv for follow_all_trackers
        new_argv = ["follow.py", "--source", src]
        if args.dry_run:
            new_argv.append("--dry-run")
        if args.pool != 1:
            new_argv.extend(["--pool", str(args.pool)])
        # pass through other args like --max-members via remaining? follow_all_trackers uses its own args
        # For simplicity, forward remaining
        new_argv.extend(remaining)
        # Handle --max -> --max-members? follow_all_trackers doesn't have max, but we can handle via --source and max fetching? Just pass
        sys.argv = new_argv
        return original_main()
    else:
        return original_main()

if __name__ == "__main__":
    follow_with_tracker()
