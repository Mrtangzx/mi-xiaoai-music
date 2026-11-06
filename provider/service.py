"""Persistent catalogue, bounded downloads, and signed speaker URLs.

Only CommonsClient may introduce new remote URLs. The API never downloads an
arbitrary URL supplied by a caller. The media relay and downloader independently
enforce the Wikimedia HTTPS host allowlist, including every redirect.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .commons import CommonsClient, canonical_query
from .songloft_sync import SongloftSync


LOG = logging.getLogger("home_music")
TRACK_ID = re.compile(r"commons-[1-9][0-9]{0,12}\Z")
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 180
METADATA_RETRY_SECONDS = 30


def normalized(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).casefold()
    return "".join(c for c in text if c.isalnum())


def safe_filename(value: str) -> str:
    value = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", value)
    return value.strip(" .").encode("utf-8")[:180].decode("utf-8", errors="ignore") or "未命名音乐"


def contains_terms(expected: str, actual: str) -> bool:
    def terms(text):
        return [normalized(t) for t in re.findall(r"[^\W\d_]+|\d+", text, re.UNICODE)]
    wanted = [t for t in terms(expected) if t not in {"the", "of", "a", "and", "的"}]
    available = set(terms(actual))
    joined = normalized(actual)
    if not wanted:
        return False
    for term in wanted:
        if re.search(r"[\u3400-\u9fff]", term):
            if term not in joined:
                return False
        elif term not in available:
            return False
    return True


def allowed_media_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("无效媒体端口") from exc
    if (parsed.scheme != "https" or parsed.hostname != "upload.wikimedia.org"
            or parsed.username is not None or parsed.password is not None
            or port not in (None, 443) or not parsed.path.startswith("/wikipedia/commons/")
            or parsed.fragment or any(ord(c) < 32 for c in url)):
        raise ValueError("只允许 Wikimedia 官方 HTTPS 音频地址")
    return url


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed_media_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_media(url: str, *, range_header: str | None = None, method="GET"):
    headers = {"User-Agent": "HomeMusic/0.2 (personal self-hosted audio library)",
               "Accept-Encoding": "identity"}
    if range_header:
        headers["Range"] = range_header
    request = urllib.request.Request(allowed_media_url(url), headers=headers, method=method)
    response = urllib.request.build_opener(RestrictedRedirect()).open(request, timeout=12)
    allowed_media_url(response.geturl())
    return response


@dataclass(frozen=True)
class Config:
    root: Path
    data: Path
    public_url: str
    token: str
    max_library_bytes: int = 5 * 1024 * 1024 * 1024

    @classmethod
    def from_env(cls):
        token = os.environ.get("HOME_MUSIC_TOKEN", "")
        public = os.environ.get("HOME_MUSIC_PUBLIC_URL", "").rstrip("/")
        parsed = urllib.parse.urlsplit(public)
        if len(token) < 32:
            raise ValueError("HOME_MUSIC_TOKEN 至少需要 32 个字符，请先运行 setup.py")
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or
                parsed.path or parsed.query or parsed.fragment or parsed.username):
            raise ValueError("HOME_MUSIC_PUBLIC_URL 应为音箱能访问的局域网 HTTP(S) 地址")
        limit = int(os.environ.get("HOME_MUSIC_MAX_LIBRARY_MB", "5120")) * 1024 * 1024
        if limit < MAX_FILE_BYTES:
            raise ValueError("曲库容量上限至少需要 64 MB")
        return cls(Path(os.environ.get("HOME_MUSIC_ROOT", "./music")).resolve(),
                   Path(os.environ.get("HOME_MUSIC_DATA", "./provider-data")).resolve(),
                   public, token, limit)


class MusicService:
    def __init__(self, config: Config, client=None):
        self.config = config
        self.client = client or CommonsClient(timeout=12.0)
        self.audio_dir = config.root / "开放音乐"
        self.credits_dir = config.data / "credits"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.credits_dir.mkdir(parents=True, exist_ok=True)
        self.database = config.data / "catalogue.sqlite3"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tracks (
                  id TEXT PRIMARY KEY, metadata TEXT NOT NULL,
                  status TEXT NOT NULL, filename TEXT, error TEXT,
                  updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS aliases (
                  keyword TEXT PRIMARY KEY, track_id TEXT NOT NULL,
                  display_text TEXT NOT NULL);
            """)
            db.execute("UPDATE tracks SET status='failed', error=? WHERE status IN ('queued','downloading')",
                       ("服务曾中断，请在曲库页面重试保存",))
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="music-cache")
        self.lock = threading.RLock()
        self.pending = set()
        self.search_cache = {}
        self.search_lock = threading.Lock()
        self.reserved_bytes = 0
        self.refresh_locks = [threading.Lock() for _ in range(32)]
        self.refresh_attempts = {}
        for part in self.audio_dir.glob(".commons-*.part"):
            part.unlink(missing_ok=True)
        self.songloft_sync = SongloftSync(self)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self):
        self.songloft_sync.close()
        self.executor.shutdown(wait=True, cancel_futures=True)

    def signature(self, track_id: str) -> str:
        return hmac.new(self.config.token.encode(), track_id.encode(), hashlib.sha256).hexdigest()

    def media_url(self, track_id: str) -> str:
        return f"{self.config.public_url}/audio/{track_id}.mp3?s={self.signature(track_id)}"

    def valid_signature(self, track_id, signature):
        return bool(TRACK_ID.fullmatch(track_id) and re.fullmatch(r"[0-9a-f]{64}", signature) and
                    hmac.compare_digest(self.signature(track_id), signature))

    def get_track(self, track_id: str):
        if not TRACK_ID.fullmatch(track_id):
            return None
        with self.db() as db:
            row = db.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        if not row:
            return None
        result = json.loads(row["metadata"])
        result.update(status=row["status"], filename=row["filename"], error=row["error"])
        return result

    def local_path(self, track):
        if track.get("status") != "ready" or not track.get("filename"):
            return None
        path = (self.audio_dir / track["filename"]).resolve()
        if path.parent != self.audio_dir.resolve() or not path.is_file():
            return None
        return path

    def list_tracks(self, limit=100, offset=0):
        if not 1 <= limit <= 500 or offset < 0:
            raise ValueError("曲库分页参数无效")
        with self.db() as db:
            rows = db.execute("SELECT id FROM tracks ORDER BY updated DESC, id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            aliases = db.execute("SELECT keyword, track_id, display_text FROM aliases").fetchall()
        alias_map = {}
        for row in aliases:
            alias_map.setdefault(row["track_id"], []).append(row["display_text"])
        records = []
        for row in rows:
            track = self.get_track(row["id"])
            track["aliases"] = alias_map.get(row["id"], [])
            if track["status"] == "ready" and self.local_path(track) is None:
                track["status"] = "missing"
                track["error"] = "音频文件已被移动或删除，请重试保存"
            track["play_url"] = self.media_url(track["id"])
            track["songloft_sync"] = self.songloft_sync.status(track["id"])
            records.append(track)
        return records

    def set_alias(self, keyword: str, track_id: str):
        key = normalized(keyword)
        if not key or len(keyword) > 160:
            raise ValueError("点歌名称不能为空，且不得超过 160 字")
        if not self.get_track(track_id):
            raise ValueError("请先将曲目加入曲库")
        with self.db() as db:
            db.execute("INSERT INTO aliases(keyword,track_id,display_text) VALUES(?,?,?) "
                       "ON CONFLICT(keyword) DO UPDATE SET track_id=excluded.track_id,display_text=excluded.display_text",
                       (key, track_id, keyword.strip()))

    def lookup(self, keyword: str):
        key = normalized(keyword)
        with self.db() as db:
            row = db.execute("SELECT track_id FROM aliases WHERE keyword=?", (key,)).fetchone()
        if row:
            return self.get_track(row["track_id"])
        # Exact matching avoids silently replacing a requested performance.
        canonical = normalized(canonical_query(keyword))
        with self.db() as db:
            db.create_function("normalized_title", 1,
                               lambda metadata: normalized(json.loads(metadata).get("title", "")))
            row = db.execute("SELECT id FROM tracks WHERE normalized_title(metadata) IN (?,?) "
                             "ORDER BY updated DESC, id LIMIT 1", (key, canonical)).fetchone()
        return self.get_track(row["id"]) if row else None

    def search(self, keyword: str):
        if not isinstance(keyword, str) or not keyword.strip() or len(keyword) > 160:
            raise ValueError("请输入 1–160 字的歌名")
        keyword = keyword.strip()
        key = normalized(keyword)
        with self.search_lock:
            cached = self.search_cache.get(key)
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
        results = self.client.search(keyword, limit=6)
        with self.search_lock:
            if len(self.search_cache) >= 128:
                self.search_cache.clear()
            self.search_cache[key] = (time.monotonic(), results)
        return results

    def import_page(self, page_id: int, alias: str = ""):
        if isinstance(page_id, bool) or not isinstance(page_id, int) or not 1 <= page_id <= 10**13:
            raise ValueError("无效的 Commons 页面编号")
        existing = self.get_track(f"commons-{page_id}")
        # A locally saved recording remains available when the origin is offline.
        track = existing if existing and self.local_path(existing) else self.client.get(page_id)
        self.enqueue(track)
        if alias:
            self.set_alias(alias, track["id"])
        return self.get_track(track["id"])

    @staticmethod
    def matches_hint(track, hint, alias=None):
        if not isinstance(hint, dict):
            return True
        if hint.get("artist"):
            if not contains_terms(str(hint["artist"]), track.get("artist", "")):
                return False
        if hint.get("title"):
            requested_title = canonical_query(str(hint["title"]))
            is_explicit_alias = alias is not None and normalized(requested_title) == normalized(canonical_query(alias))
            if not is_explicit_alias and not contains_terms(requested_title, track.get("title", "")):
                return False
        if hint.get("duration"):
            try:
                expected = float(hint["duration"])
                actual = float(track.get("duration", 0))
            except (TypeError, ValueError):
                return False
            if not math.isfinite(expected) or (expected > 0 and abs(actual - expected) > max(5, expected * .1)):
                return False
        return True

    @classmethod
    def matches(cls, keyword, track, hint=None):
        # Exact numeric/Latin tokens prevent Symphony No. 5 matching No. 15.
        haystack = track.get("title", "") + " " + track.get("artist", "")
        return (contains_terms(canonical_query(keyword), haystack)
                and cls.matches_hint(track, hint))

    def topone(self, keyword: str, hint=None):
        if not isinstance(keyword, str) or not keyword.strip() or len(keyword) > 160:
            return {"code": 400, "msg": "请输入有效歌名", "data": None}
        track = self.lookup(keyword)
        if track and not self.matches_hint(track, hint, alias=keyword):
            track = None
        if not track:
            track = next((r for r in self.search(keyword) if self.matches(keyword, r, hint)), None)
        if not track:
            return {"code": 404, "msg": "没有找到明确匹配的开放授权录音，可在曲库助手先选曲并设置中文点歌名", "data": None}
        self.enqueue(track)
        self.set_alias(keyword, track["id"])
        saved = self.local_path(self.get_track(track["id"])) is not None
        return {"code": 0, "msg": "播放家中已保存的录音" if saved else "来自开放授权曲库；正在保存到家中服务器",
                "data": {"title": track["title"], "artist": track["artist"],
                         "album": track.get("album", "开放音乐"),
                         "duration": track.get("duration", 0),
                         "url": self.media_url(track["id"]),
                         "dedup_key": track["id"]}}

    def refresh_track(self, track):
        """Refresh one unchanged Commons identity; coalesce/rate-limit retries.

        A saved local copy never needs origin metadata. Failed refreshes are also
        throttled so concurrent speaker range requests cannot hammer Commons.
        """
        track_id = track["id"]
        with self.refresh_locks[int(track_id.split("-")[1]) % len(self.refresh_locks)]:
            existing = self.get_track(track_id)
            if existing and self.local_path(existing):
                return existing
            now = time.monotonic()
            with self.lock:
                last = self.refresh_attempts.get(track_id)
                if last is not None and now - last < METADATA_RETRY_SECONDS:
                    return existing or track
                if len(self.refresh_attempts) >= 512:
                    self.refresh_attempts = {k: v for k, v in self.refresh_attempts.items()
                                             if now - v < METADATA_RETRY_SECONDS}
                self.refresh_attempts[track_id] = now
            fresh = self.client.get(int(track_id.split("-")[1]))
            if fresh.get("id") != track_id or fresh.get("page_id") != int(track_id.split("-")[1]):
                raise ValueError("音源刷新返回了不同录音，已停止保存")
            allowed_media_url(fresh["download_url"])
            with self.db() as db:
                db.execute("UPDATE tracks SET metadata=? WHERE id=?",
                           (json.dumps(fresh, ensure_ascii=False), track_id))
            return fresh

    def enqueue(self, track):
        track_id = track.get("id", "")
        if not TRACK_ID.fullmatch(track_id):
            raise ValueError("无效曲目编号")
        allowed_media_url(track["download_url"])
        with self.lock:
            existing = self.get_track(track_id)
            if existing and self.local_path(existing):
                return
            if track_id in self.pending:
                return
            if len(self.pending) >= 16:
                raise ValueError("保存队列已满，请稍后再试")
            if self.library_bytes() + self.reserved_bytes + MAX_FILE_BYTES > self.config.max_library_bytes:
                raise ValueError("开放曲库容量达到上限，请清理文件或调整 HOME_MUSIC_MAX_LIBRARY_MB")
            if shutil.disk_usage(self.audio_dir).free < MAX_FILE_BYTES * 2:
                raise ValueError("磁盘剩余空间不足 128 MB，暂停自动保存")
            clean = {k: v for k, v in track.items() if k not in {"status", "filename", "error", "play_url", "aliases", "songloft_sync"}}
            with self.db() as db:
                db.execute("INSERT INTO tracks VALUES(?,?,'queued',NULL,NULL,?) "
                           "ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,status='queued',error=NULL,updated=excluded.updated",
                           (track_id, json.dumps(clean, ensure_ascii=False), time.time()))
            self.pending.add(track_id)
            self.reserved_bytes += MAX_FILE_BYTES
            refresh_needed = bool(existing and (existing["status"] == "failed" or
                                                 existing["status"] == "ready"))
            self.executor.submit(self._download, clean, refresh_needed)

    def library_bytes(self):
        return sum(path.stat().st_size for path in self.audio_dir.glob("*.mp3") if path.is_file())

    def _state(self, track_id, status, filename=None, error=None):
        with self.db() as db:
            db.execute("UPDATE tracks SET status=?,filename=?,error=?,updated=? WHERE id=?",
                       (status, filename, error, time.time(), track_id))

    def _download(self, track, refresh_needed=False):
        track_id = track["id"]
        part = self.audio_dir / f".{track_id}.part"
        final = self.audio_dir / f"{safe_filename(track['title'])}__{track_id}.mp3"
        try:
            self._state(track_id, "downloading")
            if refresh_needed:
                track = self.refresh_track(track)
                final = self.audio_dir / f"{safe_filename(track['title'])}__{track_id}.mp3"
            start = time.monotonic()
            with open_media(track["download_url"]) as response, part.open("wb") as output:
                declared = int(response.headers.get("Content-Length", "0"))
                if response.status != 200 or declared > MAX_FILE_BYTES:
                    raise ValueError("音源响应异常或文件超过 64 MB")
                total = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_FILE_BYTES or time.monotonic() - start > MAX_DOWNLOAD_SECONDS:
                        raise ValueError("音频超过文件大小或下载时间限制")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
                if not total or (declared and declared != total):
                    raise ValueError("音频下载不完整")
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-f", "mp3",
                 "-show_entries", "stream=codec_name,codec_type:format=duration",
                 "-of", "json", str(part)], check=True, capture_output=True, text=True, timeout=20)
            info = json.loads(probe.stdout)
            audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
            duration = float(info.get("format", {}).get("duration", 0))
            if not audio or any(s.get("codec_name") != "mp3" for s in audio) or not 1 <= duration <= 1900:
                raise ValueError("下载内容不是可验证的 MP3 音频")
            # Container/Xing duration can survive a truncated response. Decode
            # every frame and compare the actual timeline with both source and
            # header durations before declaring a file complete.
            decoded = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
                 "-f", "mp3", "-i", str(part), "-map", "0:a:0", "-vn", "-sn", "-dn",
                 "-progress", "pipe:1", "-f", "null", "-"],
                check=True, capture_output=True, text=True, timeout=90)
            times = [int(line.partition("=")[2]) / 1_000_000
                     for line in decoded.stdout.splitlines() if line.startswith("out_time_us=")
                     and line.partition("=")[2].isdigit()]
            actual_duration = max(times, default=0)
            if not 1 <= actual_duration <= 1900:
                raise ValueError("音频实际解码时长无效")
            for reference in (duration, float(track.get("duration", 0))):
                if (not math.isfinite(reference) or reference <= 0 or
                        abs(actual_duration - reference) > max(2, reference * .02)):
                    raise ValueError("音频下载不完整：实际解码时长与录音信息不符")
            credits = dict(track, saved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                           saved_duration=actual_duration, header_duration=duration, integrity_check="full_ffmpeg_decode_and_duration", transformations="原样保存官方 MP3 衍生音频，不改编码或标签；来源和授权另存 JSON")
            credit_part = self.credits_dir / f".{track_id}.json.part"
            credit_part.write_text(json.dumps(credits, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(credit_part, self.credits_dir / f"{track_id}.json")
            # Keep byte identity with the live relay: a player may issue Range
            # requests before and after the atomic cache transition.
            os.replace(part, final)
            self._state(track_id, "ready", final.name)
            LOG.info("Saved %s", track_id)
        except Exception as exc:
            # Do not log URLs or credentials. Keep actionable, non-secret reasons.
            if isinstance(exc, ValueError):
                reason = str(exc)
            elif isinstance(exc, urllib.error.HTTPError):
                reason = f"音源 HTTP {exc.code}，可稍后重试"
            elif isinstance(exc, FileNotFoundError):
                reason = "缺少 ffmpeg/ffprobe，请使用部署包的 Docker 镜像"
            elif isinstance(exc, subprocess.SubprocessError):
                reason = "MP3 音频校验失败"
            else:
                reason = "音源连接失败、超时或磁盘写入失败，请检查网络和磁盘后重试"
            self._state(track_id, "failed", error=reason)
            LOG.warning("Save failed for %s: %s", track_id, type(exc).__name__)
        finally:
            part.unlink(missing_ok=True)
            with self.lock:
                self.pending.discard(track_id)
                self.reserved_bytes -= MAX_FILE_BYTES


def parse_range(header: str | None, size: int):
    """Return inclusive byte endpoints, rejecting malformed/multiple ranges."""
    if not header:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError("invalid range")
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        if start >= size or end < start:
            raise ValueError("unsatisfiable range")
    else:
        count = int(last)
        if count <= 0:
            raise ValueError("invalid suffix")
        start, end = max(0, size - count), size - 1
    return start, end
