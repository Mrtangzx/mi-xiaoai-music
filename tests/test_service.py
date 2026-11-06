from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from email.message import Message
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from provider.server import Server
from provider.service import Config, MusicService, RestrictedRedirect, allowed_media_url, parse_range


TRACK = {
    "id": "commons-73127893", "page_id": 73127893,
    "title": "For Elise (Für Elise) Beethoven JMC Han", "artist": "JMC Han",
    "album": "Wikimedia Commons", "duration": 160.45,
    "source_url": "https://commons.wikimedia.org/w/index.php?curid=73127893",
    "download_url": "https://upload.wikimedia.org/wikipedia/commons/transcoded/test/test.ogg/test.ogg.mp3",
    "license_name": "CC BY-SA 4.0", "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
    "attribution": "JMC Han; CC BY-SA 4.0", "metadata": {},
}


class OfflineClient:
    def get(self, page_id):
        assert page_id == TRACK["page_id"]
        return TRACK.copy()

    def search(self, keyword, limit=6):
        raise AssertionError("Saved-track playback must not contact the source")


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = Message()
        self.headers["Content-Length"] = str(len(data))
        self.headers["Content-Type"] = "audio/mpeg"
        self.status = 200

    def geturl(self):
        return TRACK["download_url"]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.config = Config(self.base / "audio", self.base / "data", "http://127.0.0.1:58092", "test-token-" * 5)
        self.service = MusicService(self.config, OfflineClient())

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def seed(self, content=b"0123456789" * 100):
        filename = "safe.mp3"
        (self.service.audio_dir / filename).write_bytes(content)
        with self.service.db() as db:
            db.execute("INSERT INTO tracks VALUES(?,?,'ready',?,NULL,?)",
                       (TRACK["id"], json.dumps(TRACK), filename, time.time()))
        self.service.set_alias("致爱丽丝", TRACK["id"])
        return content


class ServiceTests(Fixture):
    def test_saved_alias_plays_offline_after_restart(self):
        self.seed()
        expected_url = self.service.media_url(TRACK["id"])
        self.service.close()
        self.service = MusicService(self.config, OfflineClient())
        response = self.service.topone("致爱丽丝")
        self.assertEqual(response["code"], 0)
        self.assertEqual(response["data"]["url"], expected_url)
        self.assertEqual(self.service.list_tracks()[0]["status"], "ready")

    def test_duplicate_play_does_not_queue_download(self):
        self.seed()
        with patch.object(self.service.executor, "submit") as submit:
            for _ in range(3):
                self.service.topone("致爱丽丝")
            submit.assert_not_called()

    def test_unrelated_search_result_is_not_autoplayed(self):
        with patch.object(self.service, "search", return_value=[TRACK]):
            self.assertEqual(self.service.topone("晴天")["code"], 404)
            self.assertEqual(self.service.list_tracks(), [])
        self.assertTrue(self.service.matches("致爱丽丝", TRACK))
        self.assertFalse(self.service.matches("致爱丽丝", TRACK, {"artist": "Other Person"}))

    def test_local_alias_respects_artist_title_duration_hints(self):
        self.seed()
        with patch.object(self.service, "search", return_value=[]):
            for hint in [{"artist": "周杰伦"}, {"title": "稻香"}, {"duration": 999}]:
                self.assertEqual(self.service.topone("致爱丽丝", hint)["code"], 404)
        self.service.set_alias("晚安音乐", TRACK["id"])
        self.assertEqual(self.service.topone("晚安音乐", {"title": "晚安音乐"})["code"], 0)

    def test_numbered_works_do_not_match_other_numbers(self):
        self.assertFalse(self.service.matches("Symphony No. 5", {"title": "Symphony No. 15", "artist": "Test"}))
        self.assertTrue(self.service.matches("Symphony No. 5", {"title": "Symphony No. 5 - first movement", "artist": "Test"}))

    def test_failed_partial_download_is_not_reported_as_saved(self):
        with patch("provider.service.open_media", return_value=Response(b"")):
            self.service.enqueue(TRACK)
            self.service.close()
        self.assertEqual(self.service.get_track(TRACK["id"])["status"], "failed")
        self.assertEqual(list(self.service.audio_dir.glob("*.mp3")), [])
        self.assertEqual(list(self.service.audio_dir.glob("*.part")), [])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_real_mp3_is_validated_atomically_saved_with_credits(self):
        sample = self.base / "generated-test.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                        "-codec:a", "libmp3lame", str(sample)], check=True, capture_output=True, timeout=15)
        content = sample.read_bytes()
        with patch("provider.service.open_media", side_effect=lambda *a, **k: Response(content)):
            with patch.object(self.service.client, "get", return_value=dict(TRACK, duration=2.0)):
                self.service.import_page(TRACK["page_id"], "测试旋律")
            self.service.close()
        record = self.service.get_track(TRACK["id"])
        self.assertEqual(record["status"], "ready")
        self.assertEqual(self.service.local_path(record).read_bytes(), content)
        credits = json.loads((self.service.credits_dir / f"{TRACK['id']}.json").read_text())
        self.assertEqual(credits["license_url"], TRACK["license_url"])
        self.assertGreater(credits["saved_duration"], 1.9)

    def test_restarted_interrupted_job_is_visible_and_not_ready(self):
        with self.service.db() as db:
            db.execute("INSERT INTO tracks VALUES(?,?,'downloading',NULL,NULL,?)", (TRACK["id"], json.dumps(TRACK), time.time()))
        partial = self.service.audio_dir / f".{TRACK['id']}.part"
        partial.write_bytes(b"partial")
        self.service.close()
        self.service = MusicService(self.config, OfflineClient())
        self.assertEqual(self.service.get_track(TRACK["id"])["status"], "failed")
        self.assertFalse(partial.exists())

    def test_moved_local_file_is_visible_as_missing(self):
        self.seed()
        (self.service.audio_dir / "safe.mp3").unlink()
        self.assertEqual(self.service.list_tracks()[0]["status"], "missing")


class SecurityTests(unittest.TestCase):
    def test_media_url_allowlist_blocks_ssrf_and_unsafe_redirects(self):
        allowed_media_url(TRACK["download_url"])
        for bad in ["http://upload.wikimedia.org/a.mp3", "https://127.0.0.1/music.mp3",
                    "https://upload.wikimedia.org.evil.test/wikipedia/commons/a.mp3",
                    "https://upload.wikimedia.org@127.0.0.1/wikipedia/commons/a.mp3",
                    "https://upload.wikimedia.org:444/wikipedia/commons/a.mp3"]:
            with self.subTest(url=bad), self.assertRaises(ValueError):
                allowed_media_url(bad)
        request = urllib.request.Request(TRACK["download_url"])
        with self.assertRaises(ValueError):
            RestrictedRedirect().redirect_request(request, None, 302, "Found", {}, "http://169.254.169.254/latest")

    def test_range_semantics_cover_seek_suffix_and_invalid_inputs(self):
        self.assertEqual(parse_range("bytes=2-5", 10), (2, 5))
        self.assertEqual(parse_range("bytes=-3", 10), (7, 9))
        self.assertEqual(parse_range("bytes=8-", 10), (8, 9))
        self.assertEqual(parse_range("bytes=3-99", 10), (3, 9))
        for bad in ["bytes=11-", "bytes=5-2", "bytes=-", "bytes=-0", "bytes=0-1,3-4", "items=1-3"]:
            with self.subTest(value=bad), self.assertRaises(ValueError):
                parse_range(bad, 10)


class HTTPTests(Fixture):
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

    def request(self, path, data=None, headers=None, method=None):
        return urllib.request.urlopen(urllib.request.Request(
            self.base_url + path, data=None if data is None else json.dumps(data).encode(),
            headers=headers or {}, method=method), timeout=3)

    def test_authenticated_topone_contract_and_media_range(self):
        headers = {"Authorization": "Bearer " + self.config.token, "Content-Type": "application/json"}
        with self.request("/api/search/topone", {"keyword": "致爱丽丝"}, headers) as response:
            payload = json.load(response)
        self.assertEqual(payload["code"], 0)
        url = payload["data"]["url"]
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=10-19"})) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.headers["Content-Range"], f"bytes 10-19/{len(self.content)}")
            self.assertEqual(response.read(), self.content[10:20])
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD")) as response:
            self.assertEqual(int(response.headers["Content-Length"]), len(self.content))
            self.assertEqual(response.read(), b"")

    def test_unauthorized_catalogue_and_forged_media_are_rejected(self):
        for path, expected in [("/api/library", 401), ("/audio/commons-73127893.mp3?s=wrong", 403),
                               ("/audio/commons-73127893.mp3?s=%E4%BD%A0", 403), ("/.env", 404)]:
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path)
            self.assertEqual(error.exception.code, expected)

    def test_invalid_range_returns_416_with_length(self):
        url = self.service.media_url(TRACK["id"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=999999-"}))
        self.assertEqual(error.exception.code, 416)
        self.assertEqual(error.exception.headers["Content-Range"], f"bytes */{len(self.content)}")

    def test_html_security_and_oversize_json(self):
        with self.request("/") as response:
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertNotIn(self.config.token.encode(), response.read())
        headers = {"Authorization": "Bearer " + self.config.token, "Content-Type": "application/json"}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/search", {"keyword": "a" * 9000}, headers)
        self.assertEqual(error.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
