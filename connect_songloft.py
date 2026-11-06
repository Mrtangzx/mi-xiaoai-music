#!/usr/bin/env python3
"""Connect the local Songloft 2.13.8 and MIoT 2026.11.1.3 installation.

Reads .env as data via setup.py.  Does not deploy servers, log in to Xiaomi,
replace an installed MIoT version, or print credentials/API response bodies.
All backups and manual fallback settings are private files on this machine.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from setup import SetupError, read_env, validate_config


BASE = Path(__file__).resolve().parent
SONGLOFT_VERSION = "2.13.8"
MIOT_VERSION = "2026.11.1.3"
MIOT_HOMEPAGE = "https://github.com/songloft-org/songloft-plugin-miot"
MIOT_DOWNLOAD = MIOT_HOMEPAGE + f"/releases/download/v{MIOT_VERSION}/miot.jsplugin.zip"
SOURCE_ID = "src_home_open_music"
SOURCE_URL = "http://free-music:8080/api/search/topone"
CONFIG_PATH = "/api/v1/jsplugin/miot/config"
PLAYLIST_NAME = "免费音乐"
_MAX_RESPONSE = 2 * 1024 * 1024


class ConnectError(Exception):
    """Only deliberately constructed, credential-free messages are allowed."""


class APIError(ConnectError):
    def __init__(self, status: int | None = None):
        self.status = status
        if status == 401:
            message = "Songloft 登录或访问授权失败；请核对本机 .env 与管理员账户。"
        elif status in (404, 405):
            message = "当前 Songloft/MIoT 接口与已核实的版本不一致，已停止自动配置。"
        elif status is not None:
            message = f"Songloft 请求返回 HTTP {status}；自动配置未完成。"
        else:
            message = "无法连接本机 Songloft，或请求超时；请先确认服务已启动。"
        super().__init__(message)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # In particular, never forward the admin bearer token to a redirected host.
        raise APIError(code)


class SongloftAPI:
    def __init__(self, values: dict[str, str]):
        self.base_url = f"http://{values['LAN_IP']}:{values['SONGLOFT_PORT']}"
        self.access_token = ""
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(self, method: str, path: str, payload: dict | None = None, *, timeout: int = 20) -> dict:
        if not path.startswith("/api/v1/") or any(ch in path for ch in "\r\n"):
            raise ConnectError("拒绝访问未核实的 API 路径。")
        headers = {"Accept": "application/json", "User-Agent": "HomeMusicConnector/1.0"}
        if self.access_token:
            headers["Authorization"] = "Bearer " + self.access_token
        data = None
        if payload is not None:
            # ASCII JSON also works with the plugin's Uint8Array-to-string parser.
            data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                content = response.read(_MAX_RESPONSE + 1)
        except APIError:
            raise
        except HTTPError as exc:
            # Never include exc.read(), the raw URL, or server-provided error text.
            raise APIError(exc.code) from None
        except (URLError, OSError, TimeoutError):
            raise APIError() from None
        if len(content) > _MAX_RESPONSE:
            raise ConnectError("Songloft 返回的数据超出预期，已停止自动配置。")
        try:
            result = json.loads(content.decode("utf-8"))
        except (UnicodeError, ValueError):
            raise ConnectError("Songloft 没有返回预期的 JSON 数据，已停止自动配置。") from None
        if not isinstance(result, dict):
            raise ConnectError("Songloft 返回的接口结构未知，已停止自动配置。")
        return result

    def login(self, username: str, password: str) -> None:
        result = self.request("POST", "/api/v1/auth/login", {"username": username, "password": password})
        token = result.get("access_token")
        if not isinstance(token, str) or not token or len(token) > 16_384 or any(ch.isspace() for ch in token):
            raise ConnectError("登录响应没有有效的访问令牌，已停止自动配置。")
        self.access_token = token


def private_directory(base: Path) -> Path:
    path = base / "private"
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ConnectError("private 必须是本机普通目录；未写入任何私有设置。")
    path.mkdir(mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def write_private_json(base: Path, prefix: str, data: dict) -> Path:
    directory = private_directory(base)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"{prefix}-{timestamp}-{secrets.token_hex(3)}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


@contextmanager
def connection_lock(base: Path):
    path = private_directory(base) / "connect.lock"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ConnectError("另一个连接任务可能正在运行；如上次异常退出，请确认没有任务运行后删除 private/connect.lock。") from None
    os.close(descriptor)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def desired_source(values: dict[str, str]) -> dict:
    return {
        "id": SOURCE_ID,
        "name": "家里的开放音乐",
        "url": SOURCE_URL,
        "token": "Bearer " + values["HOME_MUSIC_TOKEN"],
        "enabled": True,
    }


def desired_settings(values: dict[str, str]) -> dict:
    return {
        "server_host": f"http://{values['LAN_IP']}:{values['SONGLOFT_PORT']}",
        "conversation_monitor_enabled": True,
        "voice_command_enabled": True,
        "external_search_enabled": True,
        "search_priority": "local_first",
        "external_search_timeout": 30,
        "external_search_no_import": False,
    }


def export_manual_settings(values: dict[str, str], base: Path) -> Path:
    return write_private_json(base, "miot-manual-settings", {
        "notice": "此文件包含曲库访问令牌，仅在本机查看。它是手工设置说明，不是完整 MIoT 配置，不能整体覆盖现有配置。",
        "verified_versions": {"songloft": SONGLOFT_VERSION, "miot": MIOT_VERSION},
        "official_plugin_download_url": MIOT_DOWNLOAD,
        "settings_to_set": desired_settings(values),
        "external_search_source_to_add_or_update": desired_source(values),
        "playlist": "如果未指定导入歌单，可创建普通歌单“免费音乐”，在 MIoT 外部搜索配置中选择它。已有导入歌单可保留。",
        "instructions": [
            "打开本机 Songloft；管理员密码在 .env 中。",
            "检查已有 MIoT 版本；不要仅为运行本脚本而覆盖已有插件、账号或设备。",
            "在现有外部搜索源列表中新增或更新本文件所列来源，保留其他来源。",
            "只设置 settings_to_set 中的字段，其他字段保持原状。",
            "在 MIoT 插件本地完成小米账号扫码登录，勾选需要接管的音箱并验证播放。",
        ],
    })


def _positive_id(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConnectError(f"{label} 的标识无效，已停止自动配置。")
    return value


def _find_miot(api) -> dict | None:
    response = api.request("GET", "/api/v1/jsplugins/")
    plugins = response.get("plugins")
    if not isinstance(plugins, list) or any(not isinstance(plugin, dict) for plugin in plugins):
        raise ConnectError("插件列表结构未知，未安装或覆盖任何插件。")
    matches = [plugin for plugin in plugins if plugin.get("entry_path") == "miot"]
    if len(matches) > 1:
        raise ConnectError("发现多个 MIoT 插件记录，未自动修改它们。")
    return matches[0] if matches else None


def _verify_miot(plugin: dict) -> None:
    _positive_id(plugin.get("id"), "MIoT 插件")
    version = plugin.get("version")
    if not isinstance(version, str) or version.removeprefix("v") != MIOT_VERSION:
        raise ConnectError("已安装的 MIoT 不是本脚本核实过的版本；已保留它并停止自动配置。")
    if plugin.get("author") != "Songloft Team" or str(plugin.get("homepage", "")).rstrip("/") != MIOT_HOMEPAGE:
        raise ConnectError("MIoT 插件来源无法确认为已核实的官方插件，已停止自动配置。")
    if plugin.get("status") not in {"active", "inactive", "error"}:
        raise ConnectError("MIoT 插件状态未知，已停止自动配置。")
    # The manifest's zipHash is not used as a downloaded ZIP's SHA-256.  The
    # official host installer owns package validation; do not conflate digests.


def _get_config(api) -> dict:
    response = api.request("GET", CONFIG_PATH)
    if response.get("success") is not True or not isinstance(response.get("data"), dict):
        raise ConnectError("MIoT 配置接口返回未知结构，未尝试覆盖现有配置。")
    config = response["data"]
    required = {
        "server_host": str,
        "conversation_monitor_enabled": bool,
        "voice_command_enabled": bool,
        "external_search_enabled": bool,
        "search_priority": str,
        "external_search_timeout": (int, float),
        "external_search_no_import": bool,
        "external_search_sources": list,
        "external_search_playlist_id": str,
    }
    for key, expected_type in required.items():
        if key not in config or not isinstance(config[key], expected_type):
            raise ConnectError("MIoT 配置字段与已核实版本不一致，未覆盖现有配置。")
    return deepcopy(config)


def merge_sources(existing: list, values: dict[str, str]) -> list:
    merged = deepcopy(existing)
    target = desired_source(values)
    seen_ids: set[str] = set()
    replaced = False
    for index, source in enumerate(merged):
        if not isinstance(source, dict):
            raise ConnectError("已有搜索来源结构异常，未覆盖它们。")
        if any(not isinstance(source.get(key), str) for key in ("id", "name", "url", "token")) or not isinstance(source.get("enabled"), bool):
            raise ConnectError("已有搜索来源缺少必要字段，未覆盖它们。")
        if not source["id"] or source["id"] in seen_ids or not source["url"].strip():
            raise ConnectError("已有搜索来源标识重复或地址无效，未覆盖它们。")
        if set(source) - {"id", "name", "url", "token", "enabled"}:
            raise ConnectError("已有搜索来源包含未知字段，自动写入可能丢失信息，已停止。")
        seen_ids.add(source["id"])
        if source["id"] == SOURCE_ID:
            if source["url"] != SOURCE_URL:
                raise ConnectError("家庭曲库来源标识已被不同地址占用，未自动覆盖。")
            merged[index] = target
            replaced = True
    if not replaced:
        merged.append(target)
    return merged


def _ensure_playlist(api) -> tuple[str, bool]:
    matches: list[dict] = []
    offset = 0
    for _ in range(10):
        query = urlencode({"type": "normal", "keyword": PLAYLIST_NAME, "exclude_labels": "none", "limit": 100, "offset": offset})
        response = api.request("GET", "/api/v1/playlists?" + query)
        playlists = response.get("playlists")
        total = response.get("total")
        if playlists is None and total == 0:
            playlists = []
        if not isinstance(playlists, list) or not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise ConnectError("歌单列表结构未知，未自动新建歌单。")
        for playlist in playlists:
            if not isinstance(playlist, dict):
                raise ConnectError("歌单列表含未知记录，未自动新建歌单。")
            if playlist.get("name") == PLAYLIST_NAME and playlist.get("type") == "normal":
                matches.append(playlist)
        offset += len(playlists)
        if offset >= total:
            break
        if not playlists:
            raise ConnectError("歌单分页结果不完整，未自动新建歌单。")
    else:
        raise ConnectError("同名相关歌单过多，请在 MIoT 中手工选择导入歌单。")
    if len(matches) > 1:
        raise ConnectError("发现多个同名“免费音乐”歌单，请在 MIoT 中手工选择。")
    if matches:
        return str(_positive_id(matches[0].get("id"), "歌单")), False
    playlist = api.request("POST", "/api/v1/playlists", {
        "name": PLAYLIST_NAME, "type": "normal", "description": "家中保存的开放授权音乐",
    })
    if playlist.get("name") != PLAYLIST_NAME or playlist.get("type") != "normal":
        raise ConnectError("新建歌单的返回结构未知，已停止后续配置。")
    return str(_positive_id(playlist.get("id"), "新建歌单")), True


def connect(values: dict[str, str], *, base: Path = BASE, api=None, progress=print) -> dict:
    """Apply the verified local API contract; return only non-secret status data."""
    validate_config(values)
    api = api or SongloftAPI(values)
    version = api.request("GET", "/api/v1/version").get("version")
    if not isinstance(version, str) or version.removeprefix("v") != SONGLOFT_VERSION:
        raise ConnectError("实际 Songloft 版本与已核实的 2.13.8 不同，未登录或自动修改。")
    api.login(values["SONGLOFT_ADMIN_USERNAME"], values["SONGLOFT_ADMIN_PASSWORD"])
    progress("已登录本机 Songloft，正在核实 MIoT 插件。")
    plugin = _find_miot(api)
    installed = False
    if plugin is None:
        # Recheck immediately before installing: never intentionally update an
        # MIoT that appeared after the first list request.
        plugin = _find_miot(api)
        if plugin is None:
            response = api.request("POST", "/api/v1/jsplugins/registry/install", {
                "download_url": MIOT_DOWNLOAD, "github_proxy": "", "overwrite": False,
            }, timeout=180)
            results = response.get("results")
            if (
                type(response.get("success")) is not int or response["success"] != 1
                or type(response.get("failed")) is not int or response["failed"] != 0
                or not isinstance(results, list) or len(results) != 1
                or not isinstance(results[0], dict) or results[0].get("success") is not True
            ):
                raise ConnectError("官方插件安装未完整成功；已停止后续配置，请在本机插件页查看状态。")
            installed = True
            progress("官方固定版本 MIoT 的安装请求已成功。")
            plugin = _find_miot(api)
            if plugin is None:
                raise ConnectError("安装后未找到 MIoT 插件，已停止后续配置。")
    _verify_miot(plugin)
    if plugin["status"] != "active":
        write_private_json(base, "miot-state-before-enable", {"plugin": plugin})
        api.request("POST", f"/api/v1/jsplugins/{plugin['id']}/enable", {})
        plugin = _find_miot(api)
        if plugin is None:
            raise ConnectError("启用后无法读取 MIoT 插件，已停止后续配置。")
        _verify_miot(plugin)
        if plugin["status"] != "active":
            raise ConnectError("MIoT 尚未处于 active 状态，请先在本机检查插件。")
        progress("已启用核实过的 MIoT 插件。")

    current = _get_config(api)
    # Validate the array before making even the optional playlist mutation.
    merge_sources(current["external_search_sources"], values)
    playlist_id = current["external_search_playlist_id"].strip()
    playlist_created = False
    if not playlist_id:
        playlist_id, playlist_created = _ensure_playlist(api)
        if playlist_created:
            progress("已创建普通歌单“免费音乐”。")
        # Preserve settings changed in the UI during a potentially slow lookup.
        current = _get_config(api)
    desired = desired_settings(values)
    desired["external_search_sources"] = merge_sources(current["external_search_sources"], values)
    if not current["external_search_playlist_id"].strip():
        desired["external_search_playlist_id"] = playlist_id
    else:
        playlist_id = current["external_search_playlist_id"]
    patch = {key: value for key, value in desired.items() if current.get(key) != value}
    if not patch:
        progress("MIoT 已具备所需配置，无需重复写入。")
        return {"installed": installed, "changed": False, "playlist_created": playlist_created, "playlist_id": playlist_id}

    backup = write_private_json(base, "miot-config-before-connect", {
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "songloft_version": SONGLOFT_VERSION, "plugin": plugin, "config": current,
    })
    progress("已将现有 MIoT 配置备份到本机 private 目录（权限 0600）。")
    response = api.request("POST", CONFIG_PATH, patch, timeout=60)
    if response.get("success") is not True:
        raise ConnectError("MIoT 没有确认配置保存成功；现有备份已保留，请在本机检查。")
    saved = _get_config(api)
    if any(saved.get(key) != value for key, value in patch.items()):
        raise ConnectError("配置保存后的回读结果不一致；备份已保留，未自动回滚或覆盖其他变更。")
    progress("已连接开放音乐来源，并核实本地优先、自动入库和语音设置。")
    return {
        "installed": installed, "changed": True, "playlist_created": playlist_created,
        "playlist_id": playlist_id, "backup_path": str(backup),
    }


def refresh_local_library(api, *, seconds: float = 25, progress=print) -> bool:
    """Best-effort incremental scan, then MIoT index refresh; never undo setup."""
    deadline = time.monotonic() + max(0.1, seconds)
    try:
        try:
            api.request("POST", "/api/v1/scan", {"reimport": False}, timeout=5)
        except APIError as exc:
            if exc.status != 409:
                raise
        progress("正在增量扫描本地曲库，已有歌曲不会强制重新导入。")
        while time.monotonic() < deadline:
            scan = api.request("GET", "/api/v1/scan/progress", timeout=5)
            status = scan.get("status")
            if status == "completed":
                break
            if status in {"failed", "cancelled", "cancelling"}:
                raise ConnectError("本地扫描未完成。")
            if status not in {"idle", "scanning", "importing", "splitting_cue", "creating_playlists"}:
                raise ConnectError("扫描进度结构未知。")
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        else:
            raise ConnectError("本地扫描仍在后台执行。")
        before = api.request("GET", "/api/v1/jsplugin/miot/indexing/status", timeout=3)
        previous = before.get("data", {}).get("last_refresh_time")
        if before.get("success") is not True or not isinstance(previous, str):
            raise ConnectError("MIoT 索引刷新时间不可验证。")
        response = api.request("POST", "/api/v1/jsplugin/miot/indexing/refresh", {}, timeout=5)
        if response.get("success") is not True:
            raise ConnectError("MIoT 未确认索引刷新。")
        index_deadline = time.monotonic() + 6
        while time.monotonic() < index_deadline:
            response = api.request("GET", "/api/v1/jsplugin/miot/indexing/status", timeout=3)
            state = response.get("data")
            if response.get("success") is not True or not isinstance(state, dict):
                raise ConnectError("MIoT 索引状态结构未知。")
            if (state.get("ready") is True and state.get("is_refreshing") is False
                    and isinstance(state.get("last_refresh_time"), str)
                    and state["last_refresh_time"] > previous):
                progress("本地曲库扫描及 MIoT 索引刷新已完成。")
                return True
            time.sleep(0.5)
        raise ConnectError("MIoT 索引仍在后台刷新。")
    except (ConnectError, OSError):
        progress("MIoT 连接设置已保留；曲库扫描或索引尚未确认完成。请在 Songloft 点击“扫描曲库”，完成后在 MIoT 点击“刷新索引”。")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="连接本机 Songloft 与 MIoT；不会登录小米账号或覆盖已有插件版本。")
    parser.add_argument("--export-only", action="store_true", help="仅生成私有手工设置，不发出网络请求")
    args = parser.parse_args(argv)
    try:
        values = read_env(BASE / ".env")
        validate_config(values)
    except (SetupError, OSError, UnicodeError):
        print("无法读取有效的 .env；请先运行 setup.py，在本机检查配置。", file=sys.stderr)
        return 1
    if args.export_only:
        path = export_manual_settings(values, BASE)
        print(f"已生成本机私有手工设置：private/{path.name}（权限 0600）。")
        return 0
    try:
        # The stack binds only the LAN address; do not send admin credentials to
        # an arbitrary RFC1918 host supplied in a copied .env file.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((values["LAN_IP"], 0))
        with connection_lock(BASE):
            api = SongloftAPI(values)
            connect(values, base=BASE, api=api)
            refreshed = refresh_local_library(api)
    except (ConnectError, SetupError) as exc:
        print(f"自动连接未完成：{exc}", file=sys.stderr)
        path = export_manual_settings(values, BASE)
        print(f"已生成私有手工设置：private/{path.name}。已完成的步骤和备份保留。")
        return 1
    except OSError:
        print("本机地址、文件权限或网络检查未通过，自动连接未完成。", file=sys.stderr)
        path = export_manual_settings(values, BASE)
        print(f"已生成私有手工设置：private/{path.name}。已完成的步骤和备份保留。")
        return 1
    print("提示：修改同名中文点歌映射后，MIoT 旧语音记忆或本地优先搜索可能仍命中旧歌；请先在 MIoT 删除该口令的旧记忆并实机验证。本脚本不会清空记忆或改动其他来源优先级。")
    print("最后在 MIoT 插件中完成小米账号扫码登录，并勾选要接管的小爱音箱。")
    print("可先在曲库网页试听，再对音箱说“小爱同学，播放歌曲致爱丽丝”进行真机验证。")
    return 0 if refreshed else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n连接任务已取消，已有配置和备份保留。", file=sys.stderr)
        raise SystemExit(130)
    except Exception:
        # Avoid tracebacks containing request objects, credentials or API bodies.
        print("连接任务未完成；请检查本机文件权限及服务状态，凭据未输出。", file=sys.stderr)
        raise SystemExit(1)
