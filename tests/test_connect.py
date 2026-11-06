"""Verified Songloft/MIoT HTTP contracts, preservation and secret handling."""

from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import connect_songloft as connector
import setup


def settings():
    return setup.make_config("192.168.1.100", 1000, 1000)


def plugin(status="active", version=connector.MIOT_VERSION):
    return {
        "id": 7, "entry_path": "miot", "version": version,
        "author": "Songloft Team", "homepage": connector.MIOT_HOMEPAGE,
        "status": status,
    }


def config():
    return {
        "server_host": "", "conversation_monitor_enabled": False,
        "voice_command_enabled": False, "external_search_enabled": False,
        "search_priority": "parallel", "external_search_timeout": 6,
        "external_search_no_import": False, "external_search_sources": [],
        "external_search_playlist_id": "",
        "ai_config": {"api_key": "old-private-ai-key", "enabled": False},
        "untouched_setting": {"example": "preserve"},
    }


class FakeAPI:
    def __init__(self, *, installed=True, current=None):
        self.plugins = [plugin()] if installed else []
        self.config = deepcopy(current) if current is not None else config()
        self.calls = []
        self.playlists = []
        self.installer_result = None
        self.host_version = connector.SONGLOFT_VERSION
        self.save_success = True
        self.readback_wrong = False

    def login(self, username, password):
        self.calls.append(("LOGIN", "/api/v1/auth/login", {"username": username, "password": password}))

    def request(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path, deepcopy(payload)))
        route = urlsplit(path).path
        if route == "/api/v1/version":
            return {"version": self.host_version}
        if route == "/api/v1/jsplugins/":
            return {"plugins": deepcopy(self.plugins)}
        if route == "/api/v1/jsplugins/registry/install":
            if self.installer_result is not None:
                return deepcopy(self.installer_result)
            self.plugins = [plugin()]
            return {"total": 1, "success": 1, "failed": 0, "results": [{"success": True, "plugin": plugin()}]}
        if route == "/api/v1/jsplugins/7/enable":
            self.plugins[0]["status"] = "active"
            return {"plugin": deepcopy(self.plugins[0])}
        if route == connector.CONFIG_PATH:
            if method == "GET":
                return {"success": True, "data": deepcopy(self.config)}
            if self.save_success:
                self.config.update(deepcopy(payload))
                if self.readback_wrong:
                    self.config["search_priority"] = "parallel"
            return {"success": self.save_success, "error": "private-raw-response-must-not-be-printed"}
        if route == "/api/v1/playlists":
            if method == "GET":
                self.assert_playlist_query(path)
                return {"playlists": deepcopy(self.playlists), "total": len(self.playlists)}
            record = {"id": 11, **payload}
            self.playlists.append(record)
            return deepcopy(record)
        raise AssertionError("Unexpected API route")

    @staticmethod
    def assert_playlist_query(path):
        query = parse_qs(urlsplit(path).query)
        assert query["keyword"] == ["免费音乐"]
        assert query["type"] == ["normal"]
        assert query["exclude_labels"] == ["none"]


class ConnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.values = settings()
        self.messages = []

    def tearDown(self):
        self.temp.cleanup()

    def run_connect(self, api):
        return connector.connect(self.values, base=self.base, api=api, progress=self.messages.append)

    def test_fresh_install_contract_and_config(self):
        api = FakeAPI(installed=False)
        result = self.run_connect(api)
        self.assertTrue(result["installed"])
        self.assertTrue(result["changed"])
        install_calls = [c for c in api.calls if c[1].endswith("registry/install")]
        self.assertEqual(len(install_calls), 1)
        self.assertEqual(install_calls[0][2], {
            "download_url": connector.MIOT_DOWNLOAD, "github_proxy": "", "overwrite": False,
        })
        self.assertEqual(api.config["external_search_timeout"], 30)
        self.assertEqual(api.config["external_search_sources"], [connector.desired_source(self.values)])
        self.assertEqual(api.config["external_search_playlist_id"], "11")
        self.assertEqual(api.config["server_host"], "http://192.168.1.100:58091")
        self.assertEqual(api.config["ai_config"], config()["ai_config"])
        self.assertEqual(api.config["untouched_setting"], config()["untouched_setting"])
        posts = [c[2] for c in api.calls if c[:2] == ("POST", connector.CONFIG_PATH)]
        self.assertNotIn("ai_config", posts[0])
        self.assertNotIn("untouched_setting", posts[0])

    def test_backup_precedes_config_write_and_is_private(self):
        api = FakeAPI()
        original_request = api.request
        def checking_request(method, path, payload=None, **kwargs):
            if (method, path) == ("POST", connector.CONFIG_PATH):
                backups = list((self.base / "private").glob("miot-config-before-connect-*.json"))
                self.assertEqual(len(backups), 1)
                backup = json.loads(backups[0].read_text())
                self.assertEqual(backup["config"], config())
                if os.name == "posix":
                    self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(backups[0].parent.stat().st_mode), 0o700)
            return original_request(method, path, payload, **kwargs)
        api.request = checking_request
        self.run_connect(api)

    def test_existing_sources_and_selected_playlist_are_preserved(self):
        current = config()
        other = {"id": "other", "name": "现有来源", "url": "https://example.test/search", "token": "old-private-token", "enabled": False}
        current["external_search_sources"] = [other]
        current["external_search_playlist_id"] = "55"
        api = FakeAPI(current=current)
        self.run_connect(api)
        self.assertEqual(api.config["external_search_sources"][0], other)
        self.assertEqual(api.config["external_search_playlist_id"], "55")
        self.assertFalse(any(urlsplit(c[1]).path == "/api/v1/playlists" for c in api.calls))

    def test_repeated_run_is_idempotent(self):
        api = FakeAPI()
        self.run_connect(api)
        api.calls.clear()
        second = self.run_connect(api)
        self.assertFalse(second["changed"])
        self.assertFalse(any(c[0] == "POST" for c in api.calls))
        self.assertEqual(len(api.config["external_search_sources"]), 1)

    def test_existing_plugin_is_never_reinstalled(self):
        api = FakeAPI()
        api.plugins[0]["status"] = "inactive"
        self.run_connect(api)
        self.assertFalse(any(c[1].endswith("registry/install") for c in api.calls))
        self.assertTrue(any(c[1].endswith("/7/enable") for c in api.calls))
        self.assertTrue(list((self.base / "private").glob("miot-state-before-enable-*.json")))

    def test_unknown_version_or_source_stops_before_mutation(self):
        for field, value in [("version", "2025.1.1"), ("author", "Other Author"), ("homepage", "https://evil.test")]:
            with self.subTest(field=field):
                api = FakeAPI()
                api.plugins[0][field] = value
                with self.assertRaises(connector.ConnectError):
                    self.run_connect(api)
                self.assertFalse(any(c[0] == "POST" for c in api.calls))
        api = FakeAPI()
        api.host_version = "9.0.0"
        with self.assertRaises(connector.ConnectError):
            self.run_connect(api)
        self.assertFalse(any(c[0] == "LOGIN" for c in api.calls))

    def test_http_200_partial_install_failure_is_not_success(self):
        api = FakeAPI(installed=False)
        api.installer_result = {"success": 0, "failed": 1, "results": [{"success": False, "error": "private-password"}]}
        with self.assertRaises(connector.ConnectError) as error:
            self.run_connect(api)
        self.assertNotIn("private-password", str(error.exception))
        self.assertFalse(any(c[1] == connector.CONFIG_PATH for c in api.calls))

    def test_id_collision_and_unknown_source_fields_fail_closed(self):
        source = connector.desired_source(self.values)
        source["url"] = "https://someone-else.test/search"
        with self.assertRaises(connector.ConnectError):
            connector.merge_sources([source], self.values)
        source = connector.desired_source(self.values)
        source["unknown_extension"] = "do not discard"
        with self.assertRaises(connector.ConnectError):
            connector.merge_sources([source], self.values)

    def test_existing_free_playlist_is_reused(self):
        api = FakeAPI()
        api.playlists = [{"id": 99, "name": "免费音乐", "type": "normal"}]
        self.run_connect(api)
        self.assertEqual(api.config["external_search_playlist_id"], "99")
        self.assertFalse(any(c[:2] == ("POST", "/api/v1/playlists") for c in api.calls))

    def test_save_failure_and_mismatch_leave_backup(self):
        for wrong_readback in (False, True):
            with self.subTest(wrong_readback=wrong_readback):
                api = FakeAPI()
                api.save_success = wrong_readback
                api.readback_wrong = wrong_readback
                with self.assertRaises(connector.ConnectError) as error:
                    self.run_connect(api)
                self.assertNotIn("private-raw-response", str(error.exception))
        self.assertEqual(len(list((self.base / "private").glob("miot-config-before-connect-*.json"))), 2)

    def test_export_only_no_network_and_no_secret_output(self):
        setup.write_new_env(self.base / ".env", self.values)
        output = io.StringIO()
        with patch.object(connector, "BASE", self.base), patch.object(connector, "SongloftAPI") as api, patch("sys.stdout", output):
            self.assertEqual(connector.main(["--export-only"]), 0)
        api.assert_not_called()
        for key in setup.SECRET_KEYS:
            self.assertNotIn(self.values[key], output.getvalue())
        files = list((self.base / "private").glob("miot-manual-settings-*.json"))
        self.assertEqual(len(files), 1)
        document = json.loads(files[0].read_text())
        self.assertEqual(document["external_search_source_to_add_or_update"]["token"], "Bearer " + self.values["HOME_MUSIC_TOKEN"])
        self.assertNotIn(self.values["SONGLOFT_ADMIN_PASSWORD"], files[0].read_text())

    def test_api_uses_real_login_schema_and_bearer(self):
        api = connector.SongloftAPI(self.values)
        responses = [io.BytesIO(b'{"access_token":"local-access-token"}'), io.BytesIO(b'{"plugins":[]}')]
        with patch.object(api.opener, "open", side_effect=responses) as opened:
            api.login("admin", "do-not-print-this-password")
            api.request("GET", "/api/v1/jsplugins/")
        request = opened.call_args_list[0].args[0]
        self.assertEqual(json.loads(request.data), {"username": "admin", "password": "do-not-print-this-password"})
        self.assertEqual(opened.call_args_list[1].args[0].get_header("Authorization"), "Bearer local-access-token")

    def test_api_error_never_exposes_response_or_password(self):
        api = connector.SongloftAPI(self.values)
        error = HTTPError("http://secret-value.test", 401, "raw-secret-message", {}, io.BytesIO(b'{"password":"private"}'))
        with patch.object(api.opener, "open", side_effect=error):
            with self.assertRaises(connector.APIError) as caught:
                api.login("admin", "very-private-password")
        for secret in ("secret-value", "raw-secret-message", "private", "very-private-password"):
            self.assertNotIn(secret, str(caught.exception))

    def test_lock_and_symlink_protect_private_output(self):
        with connector.connection_lock(self.base):
            with self.assertRaises(connector.ConnectError):
                with connector.connection_lock(self.base):
                    pass
        self.assertFalse((self.base / "private" / "connect.lock").exists())
        if hasattr(os, "symlink"):
            (self.base / "private").rmdir()
            try:
                (self.base / "private").symlink_to(self.base / "elsewhere", target_is_directory=True)
            except OSError as exc:
                if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Windows symlink privilege unavailable")
                raise
            with self.assertRaises(connector.ConnectError):
                connector.export_manual_settings(self.values, self.base)

    def test_scan_and_index_use_verified_contracts(self):
        class ScanAPI:
            def __init__(self):
                self.calls = []
                self.refreshed = False
            def request(self, method, path, payload=None, **kwargs):
                self.calls.append((method, path, payload))
                if path == "/api/v1/scan":
                    return {"message": "started"}
                if path == "/api/v1/scan/progress":
                    return {"status": "completed", "imported_files": 2}
                if path == "/api/v1/jsplugin/miot/indexing/refresh":
                    self.refreshed = True
                    return {"success": True, "data": {"message": "index refresh started"}}
                if path == "/api/v1/jsplugin/miot/indexing/status":
                    return {"success": True, "data": {"ready": True, "is_refreshing": False, "last_refresh_time": "2026-11-05T00:00:02Z" if self.refreshed else "2026-11-05T00:00:01Z"}}
                raise AssertionError("Unexpected scan route")
        api = ScanAPI()
        self.assertTrue(connector.refresh_local_library(api, progress=self.messages.append))
        self.assertEqual(api.calls[0], ("POST", "/api/v1/scan", {"reimport": False}))
        self.assertEqual(api.calls[3][1], "/api/v1/jsplugin/miot/indexing/refresh")

    def test_old_ready_index_is_not_a_successful_refresh(self):
        class OldIndexAPI:
            def request(self, method, path, payload=None, **kwargs):
                if path == "/api/v1/scan":
                    return {}
                if path == "/api/v1/scan/progress":
                    return {"status": "completed"}
                if path.endswith("/refresh"):
                    return {"success": True}
                return {"success": True, "data": {"ready": True, "is_refreshing": False, "last_refresh_time": "2026-11-05T00:00:01Z"}}
        tick = [0]
        def advance(seconds):
            tick[0] += seconds
        with patch.object(connector.time, "monotonic", side_effect=lambda: tick[0]), patch.object(connector.time, "sleep", side_effect=advance):
            self.assertFalse(connector.refresh_local_library(OldIndexAPI(), progress=self.messages.append))
        self.assertFalse(any("已完成" in m for m in self.messages))

    @unittest.skipUnless(os.name == "posix", "POSIX permission bits")
    def test_existing_directory_mode_rejected_without_changing_it(self):
        values = dict(self.values, HOME_MUSIC_UID=str(os.getuid() or 1000), HOME_MUSIC_GID=str(os.getgid()))
        directory = self.base / "provider-data"
        directory.mkdir(mode=0o500)
        try:
            with patch.object(setup, "BASE", self.base):
                with self.assertRaises(setup.SetupError):
                    setup.ensure_directories(values)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o500)
        finally:
            directory.chmod(0o700)

    def test_scan_failure_is_nonblocking_and_does_not_leak_body(self):
        api = FakeAPI()
        api.request = lambda *args, **kwargs: (_ for _ in ()).throw(connector.APIError(500))
        self.assertFalse(connector.refresh_local_library(api, progress=self.messages.append))
        self.assertIn("连接设置已保留", self.messages[-1])
        self.assertNotIn(self.values["HOME_MUSIC_TOKEN"], " ".join(self.messages))


if __name__ == "__main__":
    unittest.main()
