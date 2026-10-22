import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import discord_updater as du


def utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


class StrictDiscordWebhookState:
    """In-memory Discord channel + webhook enforcing Discord's exact 4096/6000/10 limits."""

    def __init__(self):
        self.next_id = 2000000000000000001
        self.channel_order = []     # chronological message IDs currently visible in channel
        self.messages = {}          # msg_id -> payload dict
        self.fail_next_delete = set()
        self.calls = []             # (method, path, status)

    def seed_existing(self, msg_id: str, payload: dict):
        self.channel_order.append(msg_id)
        self.messages[msg_id] = payload

    def validate_payload(self, body: dict):
        embeds = body.get("embeds")
        if not isinstance(embeds, list) or not (1 <= len(embeds) <= 10):
            return 400, f"Invalid embeds count: {len(embeds) if isinstance(embeds, list) else type(embeds)}"
        total = 0
        for idx, e in enumerate(embeds):
            t = e.get("title", "")
            d = e.get("description", "")
            if utf16_len(t) > 256 or len(t) > 256:
                return 400, f"Embed {idx} title > 256"
            if utf16_len(d) > 4096 or len(d) > 4096:
                return 400, f"Embed {idx} description exceeds 4096 ({utf16_len(d)} utf16 / {len(d)} cp)"
            total += max(len(t), utf16_len(t)) + max(len(d), utf16_len(d))
        if total > 6000:
            return 400, f"Embed size exceeds maximum size of 6000 ({total})"
        return 200, "ok"


def make_players(count: int, fancy_unicode: bool = False):
    players = []
    for i in range(1, count + 1):
        prefix = "𝕯𝖊𝖛🔥 " if fancy_unicode else "StarUser_"
        players.append({
            "userId": 100000 + i,
            "username": f"PlayerName_{i:03d}",
            "displayName": f"{prefix}{i:03d}",
            "presenceType": 2,
            "placeId": 116463962153167,
            "rootPlaceId": 116463962153167,
            "universeId": 10770728771,
            "lastLocation": "The Hollow",
            "gameId": f"00000000-1111-2222-3333-{i:012d}",
            "isFollowing": (i % 2 == 0),
        })
    return {
        "meta": {
            "targetPlaceId": 116463962153167,
            "targetUniverseId": 10770728771,
            "targetGameName": "The Hollow",
            "anyGame": False,
        },
        "target_game_players": players,
        "other_game_players": [],
        "offline_or_website": [],
    }


class TestDiscordMultiMessage(unittest.TestCase):
    def setUp(self):
        self.state = StrictDiscordWebhookState()
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                pass

            def _read_json(self):
                n = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

            def do_POST(self):
                parsed = urlparse(self.path)
                body = self._read_json()
                code, msg = state.validate_payload(body)
                if code != 200:
                    state.calls.append(("POST", parsed.path, code))
                    self.send_response(code)
                    self.end_headers()
                    self.wfile.write(msg.encode("utf-8"))
                    return
                mid = str(state.next_id)
                state.next_id += 1
                state.channel_order.append(mid)
                state.messages[mid] = body
                qs = parse_qs(parsed.query)
                status = 200 if qs.get("wait") == ["true"] else 204
                state.calls.append(("POST", parsed.path, status))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if status == 200:
                    self.wfile.write(json.dumps({"id": mid}).encode("utf-8"))

            def do_PATCH(self):
                parsed = urlparse(self.path)
                mid = parsed.path.rsplit("/", 1)[-1]
                body = self._read_json()
                if mid not in state.messages:
                    state.calls.append(("PATCH", mid, 404))
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"message": "Unknown Message", "code": 10008}')
                    return
                code, msg = state.validate_payload(body)
                state.calls.append(("PATCH", mid, code))
                if code != 200:
                    self.send_response(code)
                    self.end_headers()
                    self.wfile.write(msg.encode("utf-8"))
                    return
                state.messages[mid] = body
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"id": mid}).encode("utf-8"))

            def do_DELETE(self):
                parsed = urlparse(self.path)
                mid = parsed.path.rsplit("/", 1)[-1]
                if mid in state.fail_next_delete:
                    state.calls.append(("DELETE", mid, 503))
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"Service Unavailable")
                    return
                if mid not in state.messages:
                    state.calls.append(("DELETE", mid, 404))
                    self.send_response(404)
                    self.end_headers()
                    return
                del state.messages[mid]
                state.channel_order.remove(mid)
                state.calls.append(("DELETE", mid, 204))
                self.send_response(204)
                self.end_headers()

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.webhook_url = f"http://127.0.0.1:{self.port}/api/webhooks/123456/tok_test"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _channel_text(self) -> str:
        parts = []
        for mid in self.state.channel_order:
            for e in self.state.messages[mid]["embeds"]:
                if e.get("title"):
                    parts.append(e["title"])
                parts.append(e["description"])
        return "\n".join(parts)

    def test_full_scale_up_and_cleanup_lifecycle(self):
        orig_id = "1551363936174219275"
        self.state.seed_existing(orig_id, {"embeds": [{"title": "Admin Tracker", "description": "seed"}]})

        with tempfile.TemporaryDirectory() as td:
            id_file = Path(td) / "discord_message_id.txt"
            id_file.write_text(f"{orig_id}\n", encoding="utf-8")

            # 1) 12 players -> fits comfortably in 1 message (1 embed), keeps original ID
            batches = du.build_message_batches(make_players(12), title="Admin Tracker")
            self.assertEqual(len(batches), 1)
            du.sync_messages(self.webhook_url, batches, str(id_file))
            self.assertEqual(self.state.channel_order, [orig_id])
            self.assertEqual(du.load_message_ids(str(id_file)), [orig_id])

            # 2) 22 players -> 4k..5.9k chars: still 1 message (2 seamless embeds), no extra message
            batches = du.build_message_batches(make_players(22), title="Admin Tracker")
            self.assertEqual(len(batches), 1)
            self.assertEqual(len(batches[0]), 2)
            du.sync_messages(self.webhook_url, batches, str(id_file))
            self.assertEqual(self.state.channel_order, [orig_id])

            # 3) Surge to 48 players (>6,000 chars) -> expands to 2 messages in chronological order
            batches = du.build_message_batches(make_players(48), title="Admin Tracker")
            self.assertEqual(len(batches), 2)
            du.sync_messages(self.webhook_url, batches, str(id_file))
            self.assertEqual(len(self.state.channel_order), 2)
            self.assertEqual(self.state.channel_order[0], orig_id)
            txt48 = self._channel_text()
            self.assertNotIn("more not shown", txt48)
            for i in range(1, 49):
                self.assertIn(f"@PlayerName\\_{i:03d}" if "_" in "" else f"PlayerName_{i:03d}", txt48)

            # 4) Huge surge to 115 astral-emoji/math-bold players (~26k UTF-16 chars -> 5 messages)
            batches = du.build_message_batches(make_players(115, fancy_unicode=True), title="Video Star Tracker")
            self.assertGreaterEqual(len(batches), 4)
            du.sync_messages(self.webhook_url, batches, str(id_file))
            self.assertEqual(len(self.state.channel_order), len(batches))
            txt115 = self._channel_text()
            for i in range(1, 116):
                self.assertIn(f"PlayerName_{i:03d}", txt115)

            # 5) Drop from 115 -> 36 players -> shrinks cleanly to 2 messages, deleting #3..#N
            batches = du.build_message_batches(make_players(36), title="Video Star Tracker")
            self.assertEqual(len(batches), 2)
            du.sync_messages(self.webhook_url, batches, str(id_file))
            self.assertEqual(len(self.state.channel_order), 2)
            self.assertEqual(self.state.channel_order[0], orig_id)
            self.assertEqual(du.load_message_ids(str(id_file)), self.state.channel_order)

            # 6) Drop from 36 -> 0 players -> deletes message #2, keeps ONLY original message #1
            batches = du.build_message_batches(make_players(0), title="Video Star Tracker")
            self.assertEqual(len(batches), 1)
            du.sync_messages(self.webhook_url, batches, str(id_file))
            self.assertEqual(self.state.channel_order, [orig_id])
            self.assertEqual(id_file.read_text(encoding="utf-8"), f"{orig_id}\n")
            self.assertIn("No video stars are currently playing.", self._channel_text())

    def test_manual_delete_of_first_message_preserves_top_to_bottom_order(self):
        with tempfile.TemporaryDirectory() as td:
            id_file = Path(td) / "ids.txt"
            batches = du.build_message_batches(make_players(45), title="Developer Tracker")
            self.assertEqual(len(batches), 2)
            ids1 = du.sync_messages(self.webhook_url, batches, str(id_file))
            self.assertEqual(len(ids1), 2)

            # Simulate someone manually deleting message #1 in Discord
            del self.state.messages[ids1[0]]
            self.state.channel_order.remove(ids1[0])

            ids2 = du.sync_messages(self.webhook_url, batches, str(id_file))
            self.assertEqual(len(ids2), 2)
            self.assertEqual(self.state.channel_order, ids2)
            self.assertEqual(self.state.messages[ids2[0]]["embeds"][0]["title"], "Developer Tracker (1/2)")
            self.assertEqual(self.state.messages[ids2[1]]["embeds"][0]["title"], "Developer Tracker (2/2)")

    def test_transient_delete_failure_retains_overflow_id_for_next_tick_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            id_file = Path(td) / "ids.txt"
            # Mock _send sleep fast by monkeypatching time.sleep during 503 test
            orig_sleep = du.time.sleep
            du.time.sleep = lambda _s: None
            try:
                ids2 = du.sync_messages(self.webhook_url, du.build_message_batches(make_players(45)), str(id_file))
                self.assertEqual(len(ids2), 2)
                self.state.fail_next_delete.add(ids2[1])

                # Shrink to 5 players while DELETE on ids2[1] returns 503 -> retained in id_file!
                retained = du.sync_messages(self.webhook_url, du.build_message_batches(make_players(5)), str(id_file))
                self.assertEqual(retained, ids2)
                self.assertIn(ids2[1], self.state.channel_order)

                # Next tick: Discord recovers -> ids2[1] is automatically deleted and removed from id_file!
                self.state.fail_next_delete.clear()
                cleaned = du.sync_messages(self.webhook_url, du.build_message_batches(make_players(5)), str(id_file))
                self.assertEqual(cleaned, [ids2[0]])
                self.assertEqual(self.state.channel_order, [ids2[0]])
            finally:
                du.time.sleep = orig_sleep

    def test_cli_dry_run_and_workflow_yaml(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            json.dump(make_players(55), tf)
            jpath = tf.name
        try:
            cp = subprocess.run(
                [sys.executable, "discord_updater.py", jpath, "--title", "Admin Tracker", "--dry-run"],
                capture_output=True,
                text=True,
                check=True,
            )
            parsed = json.loads(cp.stdout)
            self.assertEqual(len(parsed), 2)
            self.assertIn("# Messages: 2", cp.stderr)
        finally:
            Path(jpath).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
