import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from provider.service import MusicService, Config
from provider.songloft_sync import SongloftSync, API, SyncError


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.songs = {}
        self.members = set()
        self.fail_playlist = False
        self.playlist = "11"
        self.version = "2.13.8"
        self.bad_import = False
        self.index_generation = 1
        self.index_fails = False

    def login(self):
        self.calls.append(("LOGIN", "", None))

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == "/api/v1/version":
            return {"version": self.version}
        if path == "/api/v1/jsplugins/":
            return {"plugins": [{"entry_path": "miot", "version": "2026.11.1.3"}]}
        if path.endswith("/indexing/status"):
            return {"success": True, "data": {"ready": True, "is_refreshing": False, "last_refresh_time": f"2026-11-05T00:00:{self.index_generation:02d}Z"}}
        if path.endswith("/indexing/refresh"):
            if self.index_fails:
                return {"success": False}
            self.index_generation += 1
            return {"success": True}
        if path.endswith("/config"):
            return {"success": True, "data": {"external_search_playlist_id": self.playlist}}
        if path.startswith("/api/v1/songs?"):
            query = parse_qs(urlsplit(path).query)
            assert query["type"] == ["remote"] and query["exclude_playlist_labels"] == ["none"]
            offset = int(query["offset"][0])
            return {"songs": list(self.songs.values())[offset:offset + 100], "total": len(self.songs)}
        if path == "/api/v1/songs/remote":
            item = payload[0]
            if self.bad_import:
                return {"songs": []}
            if item["dedup_key"] in self.songs:
                raise SyncError("Upstream UNIQUE constraint")
            song = {"id": len(self.songs) + 1, **item}
            self.songs[item["dedup_key"]] = song
            return {"songs": [song]}
        if path == f"/api/v1/playlists/{self.playlist}/songs":
            if self.fail_playlist:
                raise SyncError("sensitive raw upstream content")
            member = (self.playlist, payload["song_ids"][0])
            existed = member in self.members
            self.members.add(member)
            return {"added": 0 if existed else 1, "skipped": 1 if existed else 0}
        raise AssertionError(path)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"HOME_MUSIC_SONGLOFT_URL": ""})
        self.env.start()
        self.service = MusicService(Config(base / "audio", base, "http://192.168.1.10:58092", "x" * 40))
        self.api = FakeAPI()
        self.sync = SongloftSync(self.service, self.api, start=False)
        self.track = {"id": "commons-123", "title": "Test song", "artist": "Artist", "duration": 20}
        with self.service.db() as db:
            db.execute("INSERT INTO tracks VALUES(?,?,?,?,?,?)", (self.track["id"], json.dumps(self.track), "ready", "test.mp3", None, 1))
        (self.service.audio_dir / "test.mp3").write_bytes(b"validated-test-placeholder")

    def tearDown(self):
        self.sync.close()
        self.service.close()
        self.env.stop()
        self.temp.cleanup()

    def test_durable_import_and_playlist_are_idempotent(self):
        self.sync.run_once()
        self.assertEqual(self.sync.status("commons-123")["state"], "synced")
        self.assertEqual(len(self.api.songs), 1)
        self.assertEqual(self.api.members, {("11", 1)})
        self.api.calls.clear()
        restarted = SongloftSync(self.service, self.api, start=False)
        restarted.run_once()
        self.assertFalse(any(c[0] == "POST" for c in self.api.calls))

    def test_partial_commit_retry_after_restart_no_duplicate(self):
        self.api.fail_playlist = True
        self.sync.run_once()
        self.assertEqual(self.sync.status("commons-123")["state"], "pending")
        self.assertNotIn("sensitive", str(self.sync.status("commons-123")))
        self.api.fail_playlist = False
        SongloftSync(self.service, self.api, start=False).run_once()
        self.assertEqual(len(self.api.songs), 1)
        self.assertEqual(self.sync.status("commons-123")["state"], "synced")

    def test_missing_and_incomplete_audio_never_imported(self):
        (self.service.audio_dir / "test.mp3").unlink()
        self.sync.run_once()
        self.assertEqual(self.api.calls, [])
        (self.service.audio_dir / "test.mp3").write_bytes(b"placeholder")
        with self.service.db() as db:
            db.execute("UPDATE tracks SET status='downloading'")
        self.sync.run_once()
        self.assertEqual(self.api.calls, [])

    def test_playlist_change_reconciles_without_new_song(self):
        self.sync.run_once()
        self.api.playlist = "55"
        self.sync.run_once()
        self.assertEqual(len(self.api.songs), 1)
        self.assertEqual(self.api.members, {("11", 1), ("55", 1)})
        self.assertEqual(self.sync.status("commons-123")["playlist_id"], "55")

    def test_unknown_version_and_empty_import_fail_closed(self):
        self.api.version = "9.0.0"
        self.sync.run_once()
        self.assertEqual(self.api.songs, {})
        self.api.version = "2.13.8"
        self.api.bad_import = True
        self.sync.run_once()
        self.assertEqual(self.api.members, set())
        self.assertEqual(self.sync.status("commons-123")["state"], "pending")

    def test_paginated_exact_identity_lookup(self):
        for i in range(101):
            self.api.songs[f"other-{i}"] = {"id": i + 1, "dedup_key": f"other-{i}", "plugin_entry_path": "", "url": "http://other.test"}
        self.api.songs["commons-123"] = {"id": 102, "dedup_key": "commons-123", "plugin_entry_path": "", "url": self.service.media_url("commons-123")}
        self.sync.run_once()
        self.assertEqual(self.sync.status("commons-123")["song_id"], 102)
        self.assertFalse(any(c[1] == "/api/v1/songs/remote" for c in self.api.calls))
        self.assertTrue(any("offset=100" in c[1] for c in self.api.calls))

    def test_same_identity_wrong_url_is_not_overwritten(self):
        self.api.songs["commons-123"] = {"id": 1, "dedup_key": "commons-123", "plugin_entry_path": "", "url": "http://another.test/audio"}
        self.sync.run_once()
        self.assertEqual(self.sync.status("commons-123")["state"], "pending")
        self.assertFalse(any(c[0] == "POST" for c in self.api.calls))

    def test_import_committed_but_response_lost_is_reconciled(self):
        original = self.api.request
        def lost(method, path, payload=None):
            result = original(method, path, payload)
            if path == "/api/v1/songs/remote":
                raise SyncError("Connection lost after server commit")
            return result
        self.api.request = lost
        self.sync.run_once()
        self.assertEqual(self.sync.status("commons-123")["state"], "synced")
        self.assertEqual(len(self.api.songs), 1)

    def test_index_failure_keeps_pending_then_recovers(self):
        self.api.index_fails = True
        self.sync.run_once()
        status = self.sync.status("commons-123")
        self.assertEqual(status["state"], "pending")
        self.assertEqual(status["song_id"], 1)
        self.assertIn("索引", status["error"])
        self.api.index_fails = False
        self.sync.run_once()
        self.assertEqual(self.sync.status("commons-123")["state"], "synced")
        self.assertEqual(len(self.api.songs), 1)

    def test_standalone_service_does_not_enable_network_sync(self):
        disabled = SongloftSync(self.service, start=False)
        disabled.run_once()
        self.assertEqual(disabled.status("commons-123")["state"], "disabled")


if __name__ == "__main__":
    unittest.main()
