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
        # Live verification against real Roblox profile page containing data-purple=".../116463962153167/The-Hollow"
        self.assertFalse(ds._profile_shows_hunt(1, {}, target_place_id=116463962153167, target_universe_id=10770728771))

        # Verify embedded JSON presence still matches cleanly
        html_with_json = (
            '<meta data-purple="https://www.roblox.com/games/116463962153167/The-Hollow" />'
            '<script>window.__DATA__ = {"userPresences":[{"placeId":116463962153167,"gameId":"abc"}]};</script>'
        )
        with patch("requests.get", return_value=FakeResponse(200, html_with_json)):
            fake = FakeResponse(200)
            fake.text = html_with_json
            with patch("requests.get", return_value=fake):
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
