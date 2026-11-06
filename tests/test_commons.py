"""Offline contract, provenance, URL validation and deadline tests."""

from copy import deepcopy
import io
import json
import threading
import time
import unittest
from unittest.mock import patch

from provider.commons import CommonsClient, canonical_query


def fixture(page_id=73127893, index=1, title="File:For Elise (Für Elise) Beethoven JMC Han.ogg"):
    return {
        "pageid": page_id,
        "ns": 6,
        "title": title,
        "index": index,
        "videoinfo": [{
            "user": "Jason M. C., Han",
            "duration": 160.44625850340137,
            "mime": "application/ogg",
            "mediatype": "AUDIO",
            "url": "https://upload.wikimedia.org/wikipedia/commons/1/15/example.ogg",
            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Example.ogg",
            "extmetadata": {
                "Artist": {"value": "<bdi><a href='https://example.org'>Ludwig van Beethoven</a></bdi>"},
                "Credit": {"value": "<span>Own work</span>"},
                "LicenseShortName": {"value": "CC BY-SA 4.0"},
                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
                "UsageTerms": {"value": "Creative Commons Attribution-Share Alike 4.0"},
                "AttributionRequired": {"value": "true"},
                "Copyrighted": {"value": "True"},
                "Restrictions": {"value": ""},
            },
            "derivatives": [{
                "src": "https://upload.wikimedia.org/wikipedia/commons/transcoded/1/15/example.ogg/example.ogg.mp3",
                "type": "audio/mpeg",
                "transcodekey": "mp3",
            }],
        }],
    }


class CommonsTests(unittest.TestCase):
    def setUp(self):
        self.client = CommonsClient()

    def get_fixture(self, page):
        with patch.object(self.client, "_request", return_value={"query": {"pages": [page]}}):
            return self.client.get(page["pageid"])

    def test_real_shape_audio_ogg_and_provenance(self):
        page = fixture()
        result = self.get_fixture(page)
        self.assertEqual(result["id"], "commons-73127893")
        self.assertEqual(result["artist"], "Ludwig van Beethoven")
        self.assertEqual(result["title"], "For Elise (Für Elise) Beethoven JMC Han")
        self.assertEqual(result["license_url"], "https://creativecommons.org/licenses/by-sa/4.0/")
        self.assertIn("Jason M. C., Han", result["attribution"])
        self.assertEqual(result["source_url"], "https://commons.wikimedia.org/w/index.php?curid=73127893")
        self.assertEqual(result["metadata"], page["videoinfo"][0]["extmetadata"])

    def test_real_cc0_url_deed_and_copyrighted_true(self):
        page = fixture(18832269, title="File:Twinkle Twinkle Little Star - sung with full lyrics.ogg")
        metadata = page["videoinfo"][0]["extmetadata"]
        metadata["LicenseShortName"]["value"] = "CC0"
        metadata["LicenseUrl"]["value"] = "http://creativecommons.org/publicdomain/zero/1.0/deed.en"
        metadata["UsageTerms"]["value"] = "Creative Commons Zero, Public Domain Dedication"
        metadata["AttributionRequired"]["value"] = "false"
        result = self.get_fixture(page)
        self.assertEqual(result["license_name"], "CC0 1.0")
        self.assertEqual(result["license_url"], "https://creativecommons.org/publicdomain/zero/1.0/")

    def test_rejects_unknown_noncommercial_and_conflicting_licenses(self):
        edits = [
            ("LicenseShortName", "CC BY-NC 4.0"),
            ("LicenseShortName", "Public domain"),
            ("LicenseUrl", "https://creativecommons.org/licenses/by/4.0/"),
            ("LicenseUrl", "https://creativecommons.org.evil.example/licenses/by-sa/4.0/"),
            ("LicenseUrl", "https://evil@creativecommons.org/licenses/by-sa/4.0/"),
            ("LicenseUrl", "https://creativecommons.org/licenses/by-sa/4.0/?redirect=evil"),
            ("UsageTerms", "Non-commercial use only"),
            ("Restrictions", "personality rights"),
            ("DeletionReason", "Copyright violation"),
            ("NonFree", "true"),
            ("Copyrighted", "False"),
            ("AttributionRequired", "false"),
        ]
        for key, value in edits:
            with self.subTest(key=key, value=value):
                page = fixture()
                page["videoinfo"][0]["extmetadata"][key] = {"value": value}
                with self.assertRaises(ValueError):
                    self.get_fixture(page)

    def test_allowed_by_and_older_by_sa_versions(self):
        for family in ("BY", "BY-SA"):
            for version in ("2.0", "2.5", "3.0", "4.0"):
                with self.subTest(family=family, version=version):
                    page = fixture()
                    metadata = page["videoinfo"][0]["extmetadata"]
                    metadata["LicenseShortName"]["value"] = f"CC {family} {version}"
                    metadata["LicenseUrl"]["value"] = f"https://creativecommons.org/licenses/{family.lower()}/{version}/"
                    metadata["UsageTerms"]["value"] = f"Creative Commons Attribution{'-Share Alike' if family == 'BY-SA' else ''} {version}"
                    self.assertEqual(self.get_fixture(page)["license_name"], f"CC {family} {version}")

    def test_download_urls_reject_foreign_hosts_credentials_and_http(self):
        invalid = [
            "http://upload.wikimedia.org/wikipedia/commons/transcoded/x/a.mp3",
            "https://upload.wikimedia.org.evil.example/wikipedia/commons/transcoded/x/a.mp3",
            "https://user@upload.wikimedia.org/wikipedia/commons/transcoded/x/a.mp3",
            "https://upload.wikimedia.org:8080/wikipedia/commons/transcoded/x/a.mp3",
            "https://127.0.0.1/wikipedia/commons/transcoded/x/a.mp3",
            "https://upload.wikimedia.org/wikipedia/commons/transcoded/x/a.mp3#ignored",
            "https://upload.wikimedia.org/other/a.mp3",
            "https://upload.wikimedia.org/wikipedia/commons/transcoded/x/a.mp3\n",
            "file:///etc/passwd",
        ]
        for url in invalid:
            with self.subTest(url=url):
                page = fixture()
                page["videoinfo"][0]["derivatives"][0]["src"] = url
                with self.assertRaises(ValueError):
                    self.get_fixture(page)

    def test_unsafe_source_is_rejected_and_unsafe_original_omitted(self):
        page = fixture()
        page["videoinfo"][0]["descriptionurl"] = "https://evil.example/page"
        with self.assertRaises(ValueError):
            self.get_fixture(page)
        page = fixture()
        page["videoinfo"][0]["url"] = "http://127.0.0.1/private"
        self.assertNotIn("url", self.get_fixture(page))

    def test_strips_markup_scripts_styles_entities_and_controls(self):
        page = fixture()
        page["videoinfo"][0]["extmetadata"]["Artist"]["value"] = (
            "<style>hidden-css</style><script>bad()</script><b>Good &amp; Safe</b>"
            "\u202e<img src=x onerror=evil()>"
        )
        result = self.get_fixture(page)
        self.assertEqual(result["artist"], "Good & Safe")
        self.assertNotIn("bad()", result["attribution"])

    def test_media_duration_and_missing_mp3_validation(self):
        for key, value in [
            ("mediatype", "VIDEO"), ("mime", "text/html"),
            ("duration", 19.99), ("duration", 1800.01),
            ("duration", float("nan")), ("duration", True), ("derivatives", []), ("derivatives", None),
        ]:
            with self.subTest(key=key, value=value):
                page = fixture()
                page["videoinfo"][0][key] = value
                with self.assertRaises(ValueError):
                    self.get_fixture(page)
        for title in ("File:Fur Elise sample.ogg", "File:茉莉花-演示.opus", "File:Audio Fragment.ogg"):
            with self.subTest(title=title), self.assertRaises(ValueError):
                self.get_fixture(fixture(title=title))

    def test_aliases_do_not_guess_unseen_songs(self):
        self.assertEqual(canonical_query("致爱丽丝"), "Fur Elise")
        self.assertEqual(canonical_query("请播放《小星星》"), "Twinkle Twinkle Little Star")
        self.assertEqual(canonical_query("小爱同学，请帮我播放致爱丽丝"), "Fur Elise")
        self.assertEqual(canonical_query("周杰伦的晴天"), "周杰伦的晴天")
        self.assertEqual(canonical_query("周杰伦的小星星"), "周杰伦的小星星")
        self.assertEqual(canonical_query("请听我说"), "请听我说")

    def test_search_sorts_index_filters_invalid_and_preserves_limit(self):
        first = fixture(100, 1)
        second = fixture(200, 2)
        invalid = fixture(50, 0)
        invalid["videoinfo"][0]["extmetadata"]["NonFree"] = {"value": "true"}
        with patch.object(self.client, "_request", return_value={"query": {"pages": [second, invalid, first, deepcopy(first)]}}) as request:
            tracks = self.client.search("致爱丽丝", limit=2)
        self.assertEqual([t["page_id"] for t in tracks], [100, 200])
        self.assertEqual(tracks[0]["normalized_query"], "Fur Elise")
        self.assertEqual(request.call_args.args[0]["gsrsearch"], "Fur Elise filetype:audio")
        self.assertEqual(request.call_args.args[0]["gsrnamespace"], 6)

    def test_invalid_get_id_no_network(self):
        with patch.object(self.client, "_request") as request:
            for value in (-1, 0, True, 1.2, "1|2", "commons-1"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    self.client.get(value)
        request.assert_not_called()

    def test_api_errors_and_missing_result(self):
        with patch.object(self.client, "_request", return_value={}):
            self.assertEqual(self.client.search("没有结果"), [])
            with self.assertRaises(ValueError):
                self.client.get(123)
        response = io.BytesIO(json.dumps({"error": {"code": "maxlag"}}).encode())
        with patch.object(self.client._opener, "open", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "maxlag"):
                self.client.search("致爱丽丝")

    def test_deadline_includes_slow_connection(self):
        client = CommonsClient(timeout=0.05)
        release = threading.Event()
        finished = threading.Event()
        def slow_open(*args, **kwargs):
            try:
                release.wait(1)
                return io.BytesIO(b"{}")
            finally:
                finished.set()
        with patch.object(client._opener, "open", side_effect=slow_open):
            start = time.monotonic()
            try:
                with self.assertRaises(TimeoutError):
                    client.search("致爱丽丝")
                self.assertLess(time.monotonic() - start, 0.3)
            finally:
                release.set()
                finished.wait(1)

    def test_timeout_cap_and_header_validation(self):
        self.assertEqual(CommonsClient().timeout, 12.0)
        self.assertEqual(CommonsClient(timeout=4).timeout, 4.0)
        self.assertEqual(CommonsClient(timeout=300).timeout, 30.0)
        with self.assertRaises(ValueError):
            CommonsClient(user_agent="HomeMusic\r\nX-Injected: yes")
        with self.assertRaises(ValueError):
            CommonsClient(timeout=float("inf"))


if __name__ == "__main__":
    unittest.main()
