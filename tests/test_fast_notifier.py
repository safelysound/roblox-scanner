import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import developer_scanner as ds
import join_notifier as jn
import tracker_scanner as ts


class FakeResponse:
    def __init__(self, status_code=200, data=None, headers=None):
        self.status_code = status_code
        self._data = data or {}
        self.headers = headers or {}
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


class TestFastNotifier(unittest.TestCase):
    def test_profile_scrape_ignores_global_data_purple_banner(self):
        # Hermetic: a sitewide promo tag (present on every profile page) must not count as presence.
        banner_html = '<meta data-purple="https://www.roblox.com/games/116463962153167/The-Hollow" />'
        banner = FakeResponse(200)
        banner.text = banner_html
        with patch("requests.get", return_value=banner):
            self.assertFalse(ds._profile_shows_hunt(1, {}, target_place_id=116463962153167, target_universe_id=10770728771))

        # Embedded JSON presence still matches cleanly
        html_with_json = (
            '<meta data-purple="https://www.roblox.com/games/116463962153167/The-Hollow" />'
            '<script>window.__DATA__ = {"userPresences":[{"placeId":116463962153167,"gameId":"abc"}]};</script>'
        )
        page = FakeResponse(200)
        page.text = html_with_json
        with patch("requests.get", return_value=page):
            self.assertTrue(ds._profile_shows_hunt(999, {}, target_place_id=116463962153167, target_universe_id=10770728771))

    def test_sharding_is_lossless_disjoint_and_migrates_legacy_state(self):
        with tempfile.TemporaryDirectory() as td:
            ids_file = Path(td) / "ids.txt"
            full_ids = [1000 + i for i in range(250)]
            ids_file.write_text("\n".join(str(x) for x in full_ids), encoding="utf-8")

            legacy_state_path = Path(td) / "legacy.json"
            now = time.time()
            legacy_state_path.write_text(json.dumps({
                "hunt": {str(1000 + i): now - 10 for i in range(20)},
                "pending": {},
                "last_job": {str(1000 + i): f"job-{i}" for i in range(20)},
                "last_place": {str(1000 + i): 10770728771 for i in range(20)},
                "target": "116463962153167:10770728771",
                "updated": now - 10,
                "members": full_ids,
                "members_ts": now - 10,
            }), encoding="utf-8")

            tracker = {"idsFile": str(ids_file)}
            seen_ids = []
            seen_hunt = []
            for s in range(4):
                st, seeded = jn.load_state(str(legacy_state_path), target_sig="116463962153167:10770728771", shard=s, shards=4)
                self.assertTrue(seeded)
                part = jn.resolve_ids(tracker, st, shard=s, shards=4)
                self.assertEqual(set(part) & set(seen_ids), set())
                seen_ids.extend(part)
                self.assertEqual(set(st["hunt"].keys()) & set(seen_hunt), set())
                seen_hunt.extend(st["hunt"].keys())

            self.assertEqual(sorted(seen_ids), full_ids)
            self.assertEqual(sorted(seen_hunt), [1000 + i for i in range(20)])

    def test_load_state_keeps_last_seen_and_detects_rejoins(self):
        # Regression: load_state() used to stamp every loaded `hunt` entry with `now`, which made
        # returning admins look continuously present and suppressed their join alert forever.
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            now = time.time()
            old = now - 600
            state_path.write_text(json.dumps({
                "hunt": {"501": old, "502": old},
                "pending": {},
                "last_job": {"501": "job-501"},
                "last_place": {"501": 10770728771, "502": 10770728771},
                "target": "116463962153167:10770728771",
                "updated": now - 60,
                "shards": 1,
                "members": [501, 502],
                "members_ts": old,
            }), encoding="utf-8")

            st, seeded = jn.load_state(
                str(state_path),
                target_sig="116463962153167:10770728771",
                target_ids={116463962153167, 10770728771},
                shard=0,
                shards=1,
            )
            self.assertTrue(seeded)
            self.assertAlmostEqual(st["hunt"][501], old, delta=0.01)
            self.assertAlmostEqual(st["hunt"][502], old, delta=0.01)

            ready = jn.detect(
                st,
                {
                    501: {"userId": 501, "userPresenceType": 2, "universeId": 10770728771,
                          "placeId": 116463962153167, "gameId": "job-501"},
                    502: {"userId": 502, "userPresenceType": 2, "universeId": 10770728771,
                          "placeId": 116463962153167, "gameId": None},
                },
                now,
                seeded=True,
                grace=135,
                wait_for_job=10,
                immediate_no_job={502},
                prune=True,
            )
            self.assertEqual(set(ready), {501, 502})

    def test_load_state_clamps_only_after_a_missed_tick(self):
        # A normal run loads state seconds old, but a skipped tick must not turn every continuous
        # player into a "returning" join and ping the whole roster.
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            now = time.time()
            state_path.write_text(json.dumps({
                "hunt": {"601": now - 600},
                "pending": {},
                "last_job": {},
                "last_place": {"601": 10770728771},
                "target": "116463962153167:10770728771",
                "updated": now - 600,
                "shards": 1,
            }), encoding="utf-8")

            st, seeded = jn.load_state(
                str(state_path),
                target_sig="116463962153167:10770728771",
                shard=0,
                shards=1,
                grace=180,
            )
            self.assertTrue(seeded)
            self.assertGreater(st["hunt"][601], now - 100)
            ready = jn.detect(
                st,
                {601: {"userId": 601, "userPresenceType": 2, "universeId": 10770728771,
                       "placeId": 116463962153167, "gameId": None}},
                time.time(),
                seeded=True,
                grace=180,
                wait_for_job=10,
                immediate_no_job={601},
                prune=False,
            )
            self.assertEqual(ready, [])

    def test_presence_rank_prefers_revealed_universe_over_hidden(self):
        hidden = {"userPresenceType": 2, "placeId": None, "rootPlaceId": None, "universeId": None, "gameId": None}
        universe_only = {"userPresenceType": 2, "placeId": None, "rootPlaceId": None, "universeId": 10770728771, "gameId": None}
        self.assertGreater(jn._rank(universe_only), jn._rank(hidden))

    def test_strict_group_fetch_rejects_partial_roster(self):
        class Resp:
            def __init__(self, code, data):
                self.status_code = code
                self._data = data

            def json(self):
                return self._data

        page = [
            {"user": {"userId": i, "username": f"u{i}"}, "role": {"name": "Member", "rank": 1, "id": 1}}
            for i in range(100)
        ]

        def fake_request(sess, method, url, **kwargs):
            if url.endswith("/v1/groups/1"):
                return Resp(200, {"memberCount": 200})
            if not (kwargs.get("params") or {}).get("cursor"):
                return Resp(200, {"data": page, "nextPageCursor": "next-page"})
            return None  # second page fails -> roster is truncated

        with patch.object(ts, "_get_all_cookies", return_value=[]), \
             patch.object(ts, "_request_with_backoff", side_effect=fake_request):
            with self.assertRaises(RuntimeError):
                ts.fetch_group_members(1, strict=True)
            self.assertEqual(len(ts.fetch_group_members(1, strict=False)), 100)

    def test_follow_routes_merge_after_state_load(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            st, seeded = jn.load_state(str(state_path), target_sig="116463962153167:10770728771", shard=0, shards=2)
            self.assertFalse(seeded)
            st["followers"] = {501: "acct_1"}
            st["followers_accts"] = ["acct_1"]

            added = jn.apply_follow_routes(
                st,
                {"502": "acct_1", "503": "retired_acct", "not-a-user-id": "acct_2", "504": "acct_2"},
                {"acct_1", "acct_2"},
            )
            self.assertEqual(added, 2)
            self.assertEqual(st["followers"][501], "acct_1")
            self.assertEqual(st["followers"][504], "acct_2")
            self.assertNotIn(503, st["followers"])
            self.assertEqual(st["followers_accts"], ["acct_1", "acct_2"])

    def test_parallel_streaming_poll_routes_followers_and_streams_mid_sweep(self):
        accts = [jn.Account(f"acct_{i}", f"ck_{i}") for i in range(4)]

        # acct_0 is the sole follower account; acct_1..3 are zero-follow sweepers
        def make_presence_fn(acct_key):
            def _fn(batch):
                out = []
                for uid in batch:
                    if uid == 501:
                        if acct_key == "acct_0":
                            out.append({"userId": 501, "userPresenceType": 2, "placeId": 116463962153167, "universeId": 10770728771, "gameId": "vip-job-501"})
                        else:
                            out.append({"userId": 501, "userPresenceType": 2, "placeId": None, "universeId": None, "gameId": None})
                    elif uid == 502:
                        out.append({"userId": 502, "userPresenceType": 2, "placeId": 116463962153167, "universeId": 10770728771, "gameId": "pub-job-502"})
                    else:
                        out.append({"userId": uid, "userPresenceType": 0})
                return out
            return _fn

        for a in accts:
            a.presence = make_presence_fn(a.key)

        streamed_batches = []
        cache = {"viewer": {501: "acct_0"}, "unseeable_until": {}, "unseeable_job": set()}
        orig_stagger = jn.BATCH_STAGGER
        jn.BATCH_STAGGER = 0.0
        try:
            best, ok, complete = jn.poll(
                accts,
                list(range(500, 620)),
                poll_no=1,
                cache=cache,
                on_batch=lambda b: streamed_batches.append(set(b.keys())),
                follower_capable_keys={"acct_0"},
            )
        finally:
            jn.BATCH_STAGGER = orig_stagger

        self.assertTrue(ok)
        self.assertTrue(complete)
        self.assertEqual(best[501]["gameId"], "vip-job-501")
        self.assertEqual(best[502]["gameId"], "pub-job-502")
        self.assertTrue(any(501 in b for b in streamed_batches))
        self.assertTrue(any(502 in b for b in streamed_batches))


if __name__ == "__main__":
    unittest.main()
