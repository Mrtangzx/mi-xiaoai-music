"""Durable ready-track reconciliation against pinned Songloft/MIoT APIs.

The catalogue is the outbox: a crash between saving a file and notifying this
worker cannot lose an import. Exact identity lookup plus the upstream unique constraint and idempotent
playlist insertion make retries safe in Songloft 2.13.8. Never register an incomplete/local-missing file.
"""
from __future__ import annotations
import json
import os
import threading
import time
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler, Request
from urllib.parse import urlsplit


class SyncError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SyncError("Songloft redirect refused")


class API:
    def __init__(self, url, username, password):
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username):
            raise SyncError("Invalid Songloft URL")
        self.url, self.username, self.password = url.rstrip("/"), username, password
        self.token = ""
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, payload=None):
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=True).encode("ascii")
            headers["Content-Type"] = "application/json"
        try:
            with self.opener.open(Request(self.url + path, data=data, headers=headers, method=method), timeout=5) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError()
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except Exception:
            # Response bodies and credentials must never enter public status/logs.
            raise SyncError("Songloft request failed") from None

    def login(self):
        self.token = ""
        token = self.request("POST", "/api/v1/auth/login", {"username": self.username, "password": self.password}).get("access_token")
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise SyncError("Songloft authentication failed")
        self.token = token


class SongloftSync:
    def __init__(self, service, api=None, *, start=True, interval=30):
        self.service = service
        url = os.environ.get("HOME_MUSIC_SONGLOFT_URL", "")
        self.enabled = api is not None or bool(url)
        self.api = api
        self.interval = interval
        self.stop = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        with service.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS songloft_sync (track_id TEXT PRIMARY KEY, state TEXT NOT NULL, song_id INTEGER, playlist_id TEXT, fingerprint TEXT, error TEXT, updated REAL NOT NULL)")
        if self.enabled and self.api is None:
            self.api = API(url, os.environ.get("HOME_MUSIC_SONGLOFT_USERNAME", "admin"), os.environ.get("HOME_MUSIC_SONGLOFT_PASSWORD", ""))
        if self.enabled and start:
            self.thread = threading.Thread(target=self._run, daemon=True, name="songloft-sync")
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=25)

    def status(self, track_id):
        if not self.enabled:
            return {"state": "disabled", "error": None}
        with self.service.db() as db:
            row = db.execute("SELECT state,song_id,playlist_id,error FROM songloft_sync WHERE track_id=?", (track_id,)).fetchone()
        return dict(row) if row else {"state": "pending", "error": None}

    def _record(self, track_id, state, song_id=None, playlist_id=None, fingerprint=None, error=None):
        with self.service.db() as db:
            db.execute("INSERT INTO songloft_sync VALUES(?,?,?,?,?,?,?) ON CONFLICT(track_id) DO UPDATE SET state=excluded.state,song_id=excluded.song_id,playlist_id=excluded.playlist_id,fingerprint=excluded.fingerprint,error=excluded.error,updated=excluded.updated", (track_id,state,song_id,playlist_id,fingerprint,error,time.time()))

    def _run(self):
        while not self.stop.is_set():
            try:
                self.run_once()
            except Exception:
                pass  # Durable ready rows are retried next cycle, never discarded.
            self.stop.wait(self.interval)

    def _find_song(self, item):
        # Blank plugin_entry_path is a direct URL, not an installed plugin.
        # Upstream does INSERT (not upsert) for this case, but enforces the
        # nonempty dedup_key unique index. Read exact identity before import and
        # after ambiguous failures; never fall back to a similarly named song.
        offset = 0
        while True:
            result = self.api.request("GET", f"/api/v1/songs?type=remote&limit=100&offset={offset}&exclude_playlist_labels=none")
            songs, total = result.get("songs"), result.get("total")
            if songs is None and total == 0:
                songs = []
            if not isinstance(songs, list) or type(total) is not int or total < 0:
                raise SyncError("Unknown song list")
            for song in songs:
                if song.get("dedup_key") == item["dedup_key"] and not song.get("plugin_entry_path"):
                    if song.get("url") != item["url"]:
                        raise SyncError("Existing song URL differs; manual review required")
                    return song
            offset += len(songs)
            if offset >= total:
                return None
            if not songs:
                raise SyncError("Incomplete song list")

    def run_once(self):
        if not self.enabled:
            return
        with self.lock:
            with self.service.db() as db:
                rows = db.execute("SELECT id,metadata,filename FROM tracks WHERE status='ready'").fetchall()
            tracks = []
            for row in rows:
                track = json.loads(row["metadata"])
                track.update(status="ready", filename=row["filename"])
                if self.service.local_path(track):
                    tracks.append(track)
            if not tracks:
                return
            try:
                if self.api.request("GET", "/api/v1/version").get("version") != "2.13.8":
                    raise SyncError("Unsupported version")
                self.api.login()
                plugins = self.api.request("GET", "/api/v1/jsplugins/").get("plugins", [])
                if not any(p.get("entry_path") == "miot" and p.get("version") == "2026.11.1.3" for p in plugins):
                    raise SyncError("Unsupported MIoT version")
                config = self.api.request("GET", "/api/v1/jsplugin/miot/config")
                if config.get("success") is not True:
                    raise SyncError("MIoT unavailable")
                playlist = str(config["data"].get("external_search_playlist_id", ""))
                if not playlist.isdigit() or int(playlist) <= 0:
                    raise SyncError("No configured playlist")
            except Exception:
                for track in tracks:
                    self._record(track["id"], "pending", error="主曲库暂未连接或歌单未配置；将自动重试")
                return
            imported = []
            for track in tracks:
                if self.stop.is_set():
                    return
                item = {"title": track["title"], "artist": track.get("artist", ""), "album": track.get("album", "开放音乐"), "duration": track.get("duration", 0), "url": self.service.media_url(track["id"]), "dedup_key": track["id"], "plugin_entry_path": ""}
                fingerprint = json.dumps(item, sort_keys=True, ensure_ascii=True)
                with self.service.db() as db:
                    old = db.execute("SELECT * FROM songloft_sync WHERE track_id=?", (track["id"],)).fetchone()
                if old and old["state"] == "synced" and old["playlist_id"] == playlist and old["fingerprint"] == fingerprint:
                    continue
                try:
                    song = self._find_song(item)
                    if song is None:
                        try:
                            songs = self.api.request("POST", "/api/v1/songs/remote", [item]).get("songs")
                            if not isinstance(songs, list) or len(songs) != 1:
                                raise SyncError("Import not confirmed")
                            song = songs[0]
                        except Exception:
                            song = self._find_song(item)
                            if song is None:
                                raise SyncError("Import not confirmed")
                    song_id = song.get("id")
                    if (type(song_id) is not int or song_id <= 0 or song.get("url") != item["url"] or
                            song.get("dedup_key") != item["dedup_key"] or song.get("plugin_entry_path")):
                        raise SyncError("Import mismatch")
                    result = self.api.request("POST", f"/api/v1/playlists/{playlist}/songs", {"song_ids": [song_id]})
                    if type(result.get("added")) is not int or type(result.get("skipped")) is not int or result["added"] + result["skipped"] != 1:
                        raise SyncError("Playlist not confirmed")
                    imported.append((track["id"], song_id, playlist, fingerprint))
                    self._record(track["id"], "pending", song_id, playlist, fingerprint,
                                 "主曲库及歌单已登记；MIoT 索引刷新待确认")
                except Exception:
                    self._record(track["id"], "pending", error="录音已保存；主曲库或歌单登记未确认，将自动重试")

            if imported:
                try:
                    before = self.api.request("GET", "/api/v1/jsplugin/miot/indexing/status")
                    previous = before.get("data", {}).get("last_refresh_time")
                    if before.get("success") is not True or not isinstance(previous, str):
                        raise SyncError("Index state unavailable")
                    result = self.api.request("POST", "/api/v1/jsplugin/miot/indexing/refresh", {})
                    if result.get("success") is not True:
                        raise SyncError("Index refresh rejected")
                    deadline = time.monotonic() + 6
                    while not self.stop.is_set() and time.monotonic() < deadline:
                        result = self.api.request("GET", "/api/v1/jsplugin/miot/indexing/status")
                        state = result.get("data", {})
                        stamp = state.get("last_refresh_time")
                        if (result.get("success") is True and state.get("ready") is True and
                                state.get("is_refreshing") is False and isinstance(stamp, str) and stamp > previous):
                            for track_id, song_id, playlist, fingerprint in imported:
                                self._record(track_id, "synced", song_id, playlist, fingerprint)
                            break
                        self.stop.wait(0.5)
                except Exception:
                    pass  # Keep pending state and retry the idempotent operations.
