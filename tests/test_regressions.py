"""Offline regression coverage for cache completeness, refresh, and large libraries."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import json
from pathlib import Path
import shutil
import time
import unittest
import urllib.error
from unittest.mock import patch

import test_service as fixtures
from test_service import Fixture, Response, TRACK
from provider.server import Server
from provider.service import METADATA_RETRY_SECONDS


class CacheIntegrityRegressions(Fixture):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_demo_mp3_full_decode_accepts_only_complete_recording(self):
        sample = Path(__file__).resolve().parents[1] / "demo-music" / "致爱丽丝.mp3"
        if not sample.exists():
            self.skipTest("Bundled demo recording unavailable")
        content = sample.read_bytes()
        # Truncation preserves the Xing header's declared full duration. Test
        # both missing length and a self-consistent (but shortened) HTTP length.
        for truncated, declared in [(True, False), (True, True), (False, False)]:
            with self.subTest(truncated=truncated, declared=declared):
                payload = content[:len(content) // 2] if truncated else content
                def response(*args, **kwargs):
                    result = Response(payload)
                    if not declared:
                        del result.headers["Content-Length"]
                    return result
                with self.service.db() as db:
                    db.execute("DELETE FROM tracks")
                with patch("provider.service.open_media", side_effect=response):
                    self.service.enqueue(TRACK)
                    # Wait for this job without shutting down its reusable pool.
                    deadline = time.monotonic() + 30
                    while self.service.pending and time.monotonic() < deadline:
                        time.sleep(.01)
                self.assertFalse(self.service.pending)
                record = self.service.get_track(TRACK["id"])
                self.assertEqual(record["status"], "failed" if truncated else "ready")
                self.assertEqual(list(self.service.audio_dir.glob(".*.part")), [])
                if truncated:
                    self.assertIsNone(self.service.local_path(record))
                    self.assertFalse((self.service.credits_dir / f"{TRACK['id']}.json").exists())
                else:
                    self.assertEqual(self.service.local_path(record).read_bytes(), content)
                    credits = json.loads((self.service.credits_dir / f"{TRACK['id']}.json").read_text())
                    self.assertAlmostEqual(credits["saved_duration"], TRACK["duration"], delta=1)
                    self.assertEqual(credits["integrity_check"], "full_ffmpeg_decode_and_duration")


class MetadataRefreshRegressions(Fixture):
    def failed(self):
        with self.service.db() as db:
            db.execute("INSERT INTO tracks VALUES(?,?,'failed',NULL,'old error',?)",
                       (TRACK["id"], json.dumps(TRACK), time.time()))
        return self.service.get_track(TRACK["id"])

    def test_failed_download_retry_refreshes_before_fetching_bytes(self):
        track = self.failed()
        fresh = dict(TRACK, download_url=TRACK["download_url"] + "?revision=2")
        # Empty payload is deliberately rejected after proving retry fetched
        # the newly discovered URL, never the stale saved metadata URL.
        with patch.object(self.service.client, "get", return_value=fresh) as get, \
             patch("provider.service.open_media", side_effect=lambda *a, **kw: Response(b"")) as fetch:
            self.service.enqueue(track)
            self.service.close()
            get.assert_called_once_with(TRACK["page_id"])
            fetch.assert_called_once_with(fresh["download_url"])
        self.assertEqual(self.service.get_track(TRACK["id"])["status"], "failed")
        self.assertEqual(self.service.reserved_bytes, 0)
        self.assertFalse(self.service.pending)

    def test_refresh_rejects_different_id_or_page_identity(self):
        track = self.failed()
        for updates in [{"id": "commons-99"}, {"page_id": 99}]:
            self.service.refresh_attempts.clear()
            with self.subTest(updates=updates), patch.object(self.service.client, "get", return_value=dict(TRACK, **updates)):
                with self.assertRaises(ValueError):
                    self.service.refresh_track(track)
            self.assertEqual(self.service.get_track(TRACK["id"])["download_url"], TRACK["download_url"])
            self.assertIsNone(self.service.get_track("commons-99"))

    def test_refresh_rejects_non_allowlisted_url_without_persisting_it(self):
        track = self.failed()
        with patch.object(self.service.client, "get", return_value=dict(TRACK, download_url="http://127.0.0.1/private")):
            with self.assertRaises(ValueError):
                self.service.refresh_track(track)
        self.assertEqual(self.service.get_track(TRACK["id"])["download_url"], TRACK["download_url"])

    def test_concurrent_refreshes_coalesce_and_persist_updated_url(self):
        track = self.failed()
        fresh = dict(TRACK, download_url=TRACK["download_url"] + "?revision=2")
        def get(page_id):
            time.sleep(.03)
            return fresh
        with patch.object(self.service.client, "get", side_effect=get) as get:
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(lambda _: self.service.refresh_track(track), range(8)))
            self.assertEqual(get.call_count, 1)
        self.assertTrue(all(r["download_url"] == fresh["download_url"] for r in results))
        self.assertEqual(self.service.get_track(TRACK["id"])["download_url"], fresh["download_url"])

    def test_failed_refresh_is_throttled_then_retried_after_interval(self):
        track = self.failed()
        with patch.object(self.service.client, "get", side_effect=TimeoutError) as get:
            with self.assertRaises(TimeoutError):
                self.service.refresh_track(track)
            self.service.refresh_track(track)
            self.assertEqual(get.call_count, 1)
            self.service.refresh_attempts[TRACK["id"]] -= METADATA_RETRY_SECONDS + 1
            with self.assertRaises(TimeoutError):
                self.service.refresh_track(track)
            self.assertEqual(get.call_count, 2)

    def test_local_import_refresh_and_topone_remain_offline(self):
        self.seed()
        with patch.object(self.service.client, "get", side_effect=AssertionError("Origin must not be used")), \
             patch.object(self.service.client, "search", side_effect=AssertionError("Origin must not be used")), \
             patch.object(self.service.executor, "submit") as submit:
            self.assertEqual(self.service.import_page(TRACK["page_id"])["status"], "ready")
            self.assertEqual(self.service.refresh_track(TRACK)["status"], "ready")
            self.assertEqual(self.service.topone("致爱丽丝")["code"], 0)
            submit.assert_not_called()


class LibraryPaginationRegressions(Fixture):
    def test_exact_title_lookup_and_pagination_cover_more_than_500_tracks(self):
        with self.service.db() as db:
            for index in range(605):
                track = dict(TRACK, id=f"commons-{index + 1}", page_id=index + 1, title=f"Unique recording {index}")
                db.execute("INSERT INTO tracks VALUES(?,?,'failed',NULL,NULL,?)", (track["id"], json.dumps(track), index))
        self.assertEqual(self.service.lookup("Unique recording 0")["id"], "commons-1")
        first = self.service.list_tracks(limit=500, offset=0)
        second = self.service.list_tracks(limit=500, offset=500)
        self.assertEqual(len(first), 500)
        self.assertEqual(len(second), 105)
        self.assertEqual(len({t["id"] for t in first + second}), 605)
        self.assertEqual(second[-1]["id"], "commons-1")
        self.assertEqual(self.service.list_tracks(offset=605), [])


# Reuse HTTP fixture setup without inheriting its original tests.
class MediaRouteRegressions(Fixture):
    def setUp(self):
        super().setUp()
        self.content = self.seed()
        self.http = Server(("127.0.0.1", 0), self.service)
        self.base_url = f"http://127.0.0.1:{self.http.server_port}"
        self.service.config = replace(self.config, public_url=self.base_url)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    request = fixtures.HTTPTests.request

    def test_unauthorized_library_does_not_query_or_send_second_response(self):
        with patch.object(self.service, "list_tracks") as listing, patch("provider.server.logging.error") as error:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.request("/api/library")
            self.assertEqual(raised.exception.code, 401)
            raised.exception.read()
            listing.assert_not_called()
            error.assert_not_called()


    def test_failed_media_route_refreshes_stale_url_before_relay(self):
        self.service._state(TRACK["id"], "failed")
        fresh = dict(TRACK, download_url=TRACK["download_url"] + "?revision=2")
        path = self.service.media_url(TRACK["id"]).removeprefix(self.base_url)
        with patch.object(self.service.client, "get", return_value=fresh) as get, \
             patch.object(self.service.executor, "submit"), \
             patch("provider.server.open_media", side_effect=lambda *a, **kw: Response(b"audio")) as relay:
            with self.request(path) as response:
                self.assertEqual(response.read(), b"audio")
            get.assert_called_once_with(TRACK["page_id"])
            self.assertEqual(relay.call_args.args[0], fresh["download_url"])

    def test_expired_media_url_refreshes_once_after_404(self):
        self.service._state(TRACK["id"], "queued")
        fresh = dict(TRACK, download_url=TRACK["download_url"] + "?revision=2")
        path = self.service.media_url(TRACK["id"]).removeprefix(self.base_url)
        stale_error = urllib.error.HTTPError(TRACK["download_url"], 404, "Gone", {}, None)
        with patch.object(self.service.client, "get", return_value=fresh) as get, \
             patch.object(self.service.executor, "submit"), \
             patch("provider.server.open_media", side_effect=[stale_error, Response(b"audio")]) as relay:
            with self.request(path) as response:
                self.assertEqual(response.read(), b"audio")
            get.assert_called_once_with(TRACK["page_id"])
            self.assertEqual([call.args[0] for call in relay.call_args_list],
                             [TRACK["download_url"], fresh["download_url"]])

    def test_saved_local_media_route_never_uses_origin(self):
        path = self.service.media_url(TRACK["id"]).removeprefix(self.base_url)
        with patch.object(self.service.client, "get", side_effect=AssertionError("Offline")), \
             patch("provider.server.open_media", side_effect=AssertionError("Offline")):
            with self.request(path) as response:
                self.assertEqual(response.read(), self.content)

    def test_library_http_pagination_validates_bounds(self):
        headers = {"Authorization": "Bearer " + self.config.token}
        with self.request("/api/library?limit=1&offset=1", headers=headers) as response:
            self.assertEqual(json.load(response)["data"], [])
        for query in ["limit=0", "limit=501", "offset=-1", "limit=abc", "offset=abc"]:
            with self.subTest(query=query), self.assertRaises(urllib.error.HTTPError) as raised:
                self.request("/api/library?" + query, headers=headers)
            self.assertEqual(raised.exception.code, 400)
