# Roblox Group Scanner — Usage Guide

## Quick Start
```bash
pip install requests
# optional but recommended for progress bars
pip install tqdm

python roblox_group_scanner.py
```

This will:
1. Fetch all 2,771 members of https://www.roblox.com/communities/1200769/Official-Group-of-Roblox (28 pages, 100/page)
2. Resolve PlaceId 74205509034203 → UniverseId 10766456501 (The Hunt: Roblox 20)
3. Check presence in batches of 50 (56 batches) — the most efficient allowed
4. Print prioritized results + save `roblox_scan_results.json`

**Expected time on fresh IP:** ~80–90 seconds (rate-limit safe)
- Group phase: 28 × 0.6s = ~17s
- Presence phase: 56 × 1.1s = ~62s
- Overhead: ~5s

## Rate Limits — How This Stays Efficient Yet Safe

Roblox headers observed live:
```
x-ratelimit-limit: 60, 60;w=60
x-ratelimit-remaining: 59
x-ratelimit-reset: 51
```
→ **60 requests per 60 seconds per domain (sliding window).**

### Naive approach = 2,771 requests (one per user) → guaranteed 429
### This script = ~84 requests total (28 group + 56 presence) → 33× fewer

**Tuning after live testing:**
- `4 workers + 0.25s` → 429 storm after ~6 batches (tested, failed)
- `2 workers + 0.4s` → 429 after ~6 batches (tested, failed)
- `1 worker + 1.1s` → **no 429 for 200 users (tested, success)**
- Remaining 49 with 1.1s still hit 429 when IP window was contaminated from previous bursts

**Final defaults (safe for full 2,771):**
```python
GROUP_PAGE_LIMIT = 100      # max per page
PRESENCE_BATCH_SIZE = 50    # max per POST (100 fails with 400)
DEFAULT_GROUP_DELAY = 0.6   # sec between group pages
DEFAULT_PRESENCE_DELAY = 1.1 # sec between *any* presence batches (global throttle)
DEFAULT_WORKERS = 1         # sequential is safest; 2 with global throttle also safe
```

If you run scans back-to-back, you **must wait 60–70s** between runs or you'll hit the sliding window. The script handles 429 automatically:
- Reads `x-ratelimit-reset` and `Retry-After`
- Exponential backoff: 1s, 2s, 4s, 8s, 16s (max 60s)
- Precautionary sleep if `remaining < 5`

## Common Commands

```bash
# Full scan (default) — unauthenticated, ~80-90s
python roblox_group_scanner.py

# Test on first 200 members only (fast, ~5 sec)
python roblox_group_scanner.py --max-members 200

# Faster but riskier (may hit 429 on large groups)
python roblox_group_scanner.py --workers 2 --presence-delay 0.8

# Safer but slower (for huge groups or contaminated IP)
python roblox_group_scanner.py --workers 1 --throttle 1.5

# Custom group / game
python roblox_group_scanner.py --group-id 123456 --place-id 987654321

# Export CSV as well
python roblox_group_scanner.py --csv results.csv

# No progress bars (for cron / CI)
python roblox_group_scanner.py --json results.json 2>&1 | cat

# Verbose debug (shows rate limit headers)
python roblox_group_scanner.py --verbose --max-members 50
```

## 6x Speed Boost — ROBLOSECURITY Cookie Pool

Unauthenticated = 60 req/min per IP. With **6 cookies you get ~360 req/min** (each cookie has its own bucket). For 2771 members:

- **1 cookie (default):** ~80-90s (28×0.6s + 56×1.1s)
- **2 cookies:** ~45s (auto: 0.65s, 2 workers)
- **6 cookies:** ~15-20s (auto: 0.35s, 6 workers)

### Three ways to provide cookies (choose one):

**Option A — Hardcoded (quickest, least secure):**
Edit top of `roblox_group_scanner.py`:
```python
ROBLOSECURITY_COOKIES = [
    "_|WARNING:-DO-NOT-SHARE-THIS...|cookie1",
    "_|WARNING:-DO-NOT-SHARE-THIS...|cookie2",
    "", # up to 6
]
```

**Option B — Environment variables (recommended):**
```bash
export ROBLOSECURITY_1="_|WARNING...|cookie1"
export ROBLOSECURITY_2="_|WARNING...|cookie2"
export ROBLOSECURITY_3="_|WARNING...|cookie3"
# ... up to ROBLOSECURITY_6
python roblox_group_scanner.py
# single: export ROBLOSECURITY="..."
```

**Option C — File (most secure, git-ignorable):**
```bash
# Create cookies.txt (one per line, up to 6)
cp cookies.example.txt cookies.txt
# edit cookies.txt with real cookies
python roblox_group_scanner.py --cookie-file cookies.txt
```

**Other cookie flags:**
```bash
# Comma-separated via CLI
python roblox_group_scanner.py --cookies "cookie1,cookie2,cookie3"

# Mix: file + env + hardcoded are auto-merged (deduplicated, max 6)
python roblox_group_scanner.py --cookie-file cookies.txt --cookies "extra_cookie"
```

**How auto-tuning works:**
- 1 cookie: `presence-delay 1.1s, workers 2` (safe)
- 2 cookies: `presence-delay 0.65s, workers 2` → ~2× faster
- 4 cookies: `presence-delay 0.4s, workers 4` → ~3× faster
- 6 cookies: `presence-delay 0.35s, workers 6` → ~4-5× faster
- You can override: `--presence-delay 0.3 --workers 6 --group-delay 0.35`

> **Security:** `.ROBLOSECURITY` grants full account access. Never commit, share, or paste it publicly. Use `cookies.txt` (git-ignored) or env vars. The script never logs cookie values.

**Verify cookies:**
```bash
python roblox_group_scanner.py --cookie-file cookies.txt --max-members 10 --verbose
# Look for: "Cookie 1: authenticated as user 12345 (Username)" or "invalid/expired"
```

## Output

### Terminal (prioritized)
```
================================================================================
 ROBLOX GROUP PRESENCE SCAN — RESULTS (Prioritized)
================================================================================
Group members scanned: 500
Target game: PlaceId 74205509034203 | UniverseId 10766456501 | "The Hunt: Roblox 20"
Playing TARGET game: 1
Playing OTHER games: 5

🎯  [PRIORITY 1] PLAYING TARGET GAME
  1. GalaxyForceMan (GalaxyForceMan)  [UID 623795340]
     Role: Team Member (Rank 20) | Location: The Hunt: Roblox 20

🎮  [PRIORITY 2] PLAYING OTHER GAMES
  1. EncodedAlex ... — Place unknown (privacy hidden but InGame)
```

### JSON (`roblox_scan_results.json`)
```json
{
  "meta": { "groupId": 1200769, "targetPlaceId": 74205509034203, ... },
  "summary": { "total_members": 500, "target_game_count": 1, ... },
  "target_game_players": [ ... ],
  "other_game_players": [ ... ],
  "offline_or_website": [ ... ]
}
```

### CSV (`--csv out.csv`)
`priority,username,displayName,userId,...,profileUrl` — target first, then others

## Privacy Note
Some InGame users show `placeId: null` and `lastLocation: ""` even though `userPresenceType: 2`. This means their privacy is set to hide game details (Friends only, etc.). The script correctly counts them as "playing a game" but cannot determine if it's the target game — they appear under **Priority 2**. This is a Roblox API limitation, not a bug. After April 2025 Roblox fixed a bug that leaked hidden presence, so hidden = null is expected.

## Troubleshooting

**429 Too Many Requests**
- You ran scans too quickly. Wait 60–70 seconds and retry.
- Or increase throttle: `--throttle 1.5 --workers 1`

**No members fetched**
- Check group ID is correct and group is public. Private groups need authentication (not supported without cookie).

**0 target players but you expect some**
- Members may be offline, or privacy hidden, or not actually in that exact PlaceId. Try rescanning in 2–5 minutes.

## Files
- `roblox_group_scanner.py` — main script (only dependency: `requests`)
- `USAGE.md` — this guide
