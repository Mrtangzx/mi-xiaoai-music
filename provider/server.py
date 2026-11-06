"""Small LAN service; Songloft's existing UI remains the primary music player."""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .service import Config, MAX_FILE_BYTES, MusicService, open_media, parse_range


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, address, service):
        self.service = service
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeMusic/0.2"
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    @property
    def service(self):
        return self.server.service

    def log_message(self, format, *args):
        # Standard request logging would persist signed media URLs. Suppress it.
        pass

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        super().end_headers()

    def json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def authenticated(self):
        provided = self.headers.get("Authorization", "")
        expected = "Bearer " + self.service.config.token
        if hmac.compare_digest(provided.encode(), expected.encode()):
            return True
        self.json(401, {"code": 401, "msg": "请使用曲库助手的访问密钥登录", "data": None})
        return False

    def body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ValueError("不支持分块请求正文")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("请求必须使用 application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("无效请求长度") from exc
        if not 1 <= length <= 8192:
            raise ValueError("请求正文须为 1–8192 字节")
        content = json.loads(self.rfile.read(length))
        if not isinstance(content, dict):
            raise ValueError("请求正文须为 JSON 对象")
        return content

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            parts = urllib.parse.urlsplit(self.path)
            if parts.path == "/healthz":
                self.json(200, {"status": "ok", "version": "0.2.0"})
            elif parts.path in {"/", "/app.js", "/style.css"}:
                filename, content_type = {
                    "/": ("index.html", "text/html; charset=utf-8"),
                    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "/style.css": ("style.css", "text/css; charset=utf-8"),
                }[parts.path]
                data = (Path(__file__).parent / "static" / filename).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'none'; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
            elif parts.path == "/api/library":
                if not self.authenticated():
                    return
                query = urllib.parse.parse_qs(parts.query)
                try:
                    limit = int(query.get("limit", ["100"])[0])
                    offset = int(query.get("offset", ["0"])[0])
                    tracks = self.service.list_tracks(limit, offset)
                except ValueError:
                    self.json(400, {"code": 400, "msg": "曲库分页参数无效"})
                    return
                self.json(200, {"code": 0, "data": tracks})
            elif parts.path.startswith("/audio/"):
                match = re.fullmatch(r"/audio/(commons-[1-9][0-9]{0,12})\.mp3", parts.path)
                signature = urllib.parse.parse_qs(parts.query).get("s", [""])[0]
                if not match or not self.service.valid_signature(match[1], signature):
                    self.json(403, {"code": 403, "msg": "无效播放地址"})
                    return
                self.media(match[1])
            else:
                self.json(404, {"code": 404, "msg": "未找到页面"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            logging.error("GET request failed; request URL omitted")
            self.json(500, {"code": 500, "msg": "请求失败，请检查服务状态"})

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path not in {"/api/search", "/api/search/topone", "/api/import", "/api/alias"}:
            self.json(404, {"code": 404, "msg": "未知接口", "data": None})
            return
        if not self.authenticated():
            return
        try:
            content = self.body()
            if path == "/api/search":
                self.json(200, {"code": 0, "data": self.service.search(content.get("keyword", ""))})
            elif path == "/api/search/topone":
                self.json(200, self.service.topone(content.get("keyword", ""), content.get("hint")))
            elif path == "/api/import":
                alias = content.get("alias", "")
                if not isinstance(alias, str) or len(alias) > 160:
                    raise ValueError("中文点歌名长度不能超过 160 字")
                self.json(202, {"code": 0, "data": self.service.import_page(content.get("page_id"), alias)})
            elif path == "/api/alias":
                if not isinstance(content.get("alias"), str) or not isinstance(content.get("id"), str):
                    raise ValueError("需要有效曲目编号和点歌名")
                self.service.set_alias(content["alias"], content["id"])
                self.json(200, {"code": 0, "msg": "点歌名称已保存"})
        except (ValueError, json.JSONDecodeError) as exc:
            self.json(400, {"code": 400, "msg": str(exc), "data": None})
        except (TimeoutError, urllib.error.URLError, OSError, RuntimeError):
            self.json(200 if path.endswith("topone") else 502,
                      {"code": 502, "msg": "免费音源暂时连接失败；已保存的歌曲仍可播放。请稍后重试。", "data": None})
        except Exception:
            # Exception type only; unexpected third-party errors may contain URLs.
            logging.error("API failed on fixed route %s", path)
            self.json(500, {"code": 500, "msg": "服务处理失败，请查看服务状态", "data": None})

    def media(self, track_id):
        track = self.service.get_track(track_id)
        if not track:
            self.json(404, {"code": 404, "msg": "曲目不存在"})
            return
        local = self.service.local_path(track)
        if local:
            self.local_media(local)
            return
        range_header = self.headers.get("Range")
        if range_header and not re.fullmatch(r"bytes=(\d*)-(\d*)", range_header):
            self.json(416, {"code": 416, "msg": "仅支持一个字节范围"})
            return
        try:
            if track["status"] in {"failed", "ready"}:
                track = self.service.refresh_track(track)
            self.service.enqueue(track)
            # A retrying cache job may already have refreshed the same identity.
            track = self.service.get_track(track_id) or track
            try:
                response = open_media(track["download_url"], range_header=range_header, method=self.command)
            except urllib.error.HTTPError as exc:
                if exc.code not in {403, 404, 410}:
                    raise
                track = self.service.refresh_track(track)
                response = open_media(track["download_url"], range_header=range_header, method=self.command)
        except urllib.error.HTTPError as exc:
            self.json(416 if exc.code == 416 else 502, {"code": exc.code, "msg": "音源当前不可播放"})
            return
        except Exception:
            self.json(502, {"code": 502, "msg": "音频仍未保存，且原音源当前不可连接"})
            return
        with response:
            if response.status not in (200, 206):
                self.json(502, {"code": 502, "msg": "音源响应异常"})
                return
            declared = int(response.headers.get("Content-Length", "0"))
            mime = response.headers.get_content_type()
            if declared > MAX_FILE_BYTES or mime not in {"audio/mpeg", "audio/mp3", "application/octet-stream"}:
                self.json(502, {"code": 502, "msg": "音源不是受支持的 MP3 文件"})
                return
            self.send_response(response.status)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=3600")
            for header in ("Content-Length", "Content-Range"):
                if response.headers.get(header):
                    self.send_header(header, response.headers[header])
            self.end_headers()
            if self.command == "HEAD":
                return
            started = time.monotonic()
            sent = 0
            while sent <= MAX_FILE_BYTES and time.monotonic() - started < 180:
                chunk = response.read(min(64 * 1024, MAX_FILE_BYTES - sent + 1))
                if not chunk:
                    break
                sent += len(chunk)
                if sent > MAX_FILE_BYTES:
                    break
                self.wfile.write(chunk)

    def local_media(self, path):
        size = path.stat().st_size
        try:
            byte_range = parse_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = byte_range or (0, size - 1)
        self.send_response(206 if byte_range else 200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "private, max-age=3600")
        if byte_range:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as source:
            source.seek(start)
            remaining = end - start + 1
            while remaining:
                data = source.read(min(64 * 1024, remaining))
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = MusicService(Config.from_env())
    server = Server((os.environ.get("HOME_MUSIC_BIND_HOST", "127.0.0.1"),
                     int(os.environ.get("HOME_MUSIC_PORT", "8080"))), service)
    logging.info("Home Music provider started; API authentication enabled")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
