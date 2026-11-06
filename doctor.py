#!/usr/bin/env python3
"""Read-only, shareable diagnostics. Never print passwords, tokens, or raw logs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import socket
import stat
import subprocess
import sys
import urllib.error
import urllib.request

from setup import BASE, ENV_KEYS, SetupError, candidate_addresses, read_env, validate_config


def command(argv: list[str], timeout: int = 8) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, cwd=BASE,
        )
        if result.returncode != 0:
            return False, "不可用（未输出原始错误，避免带出凭据）"
        return True, result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False, "未安装、权限不足或超时"


def version_text(text: str) -> str:
    # Docker version commands are not expected to include credentials; constrain
    # their output anyway. Host names, environment variables and logs are omitted.
    return " ".join(text.split())[:120]


def tcp_reachable(address: str, port: int) -> bool:
    try:
        with socket.create_connection((address, port), timeout=2):
            return True
    except OSError:
        return False


def http_status(url: str) -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=3) as response:
            return f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except (OSError, urllib.error.URLError):
        return "无响应/连接失败"


def main() -> int:
    print("家庭音乐服务诊断（只读，无账号凭据）")
    print(f"系统：{platform.system()}；内核：{platform.release()}；CPU：{platform.machine()}")
    print(f"Python：{platform.python_version()}")
    if hasattr(os, "getuid"):
        print(f"当前进程 UID/GID：{os.getuid()}/{os.getgid()}")
    print("局域网地址（只读取本机网卡，不扫描网络）：")
    candidates = candidate_addresses()
    if candidates:
        for name, address in candidates:
            print(f"  {name}: {address}")
    else:
        print("  未检测到（可在服务器本机运行 hostname -I 核对）")

    docker_exists = shutil.which("docker") is not None
    docker_ready = False
    if docker_exists:
        for label, argv in (
            ("Docker CLI", ["docker", "--version"]),
            ("Docker Compose", ["docker", "compose", "version", "--short"]),
            ("Docker Engine", ["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"]),
        ):
            ok, output = command(argv)
            print(f"{label}：{version_text(output)}")
            if label == "Docker Engine":
                docker_ready = ok
    else:
        print("Docker：未安装或不在 PATH 中。")

    env_path = BASE / ".env"
    if not env_path.exists() and not env_path.is_symlink():
        print("配置：尚未初始化，.env 不存在。")
        print("下一步：在本机运行 python3 setup.py，或先确认 Linux/Docker 环境。")
        return 0

    try:
        config = read_env(env_path)
        validate_config(config)
    except (SetupError, OSError, UnicodeError):
        print("配置：格式无效或无法读取，未输出文件内容。")
        return 1
    print("配置：已初始化；密码与 token 均已隐藏。")
    if os.name == "posix":
        mode = stat.S_IMODE(env_path.stat().st_mode)
        print(f".env 权限：{mode:04o}" + ("（建议 chmod 600 .env）" if mode & 0o077 else ""))
    print(f"固定镜像：{config['SONGLOFT_IMAGE']}")
    print(f"容器 UID/GID：{config['HOME_MUSIC_UID']}/{config['HOME_MUSIC_GID']}")
    print(f"LAN_IP：{config['LAN_IP']}")
    for name in ("music", "songloft-data", "provider-data"):
        path = BASE / name
        if path.is_dir() and not path.is_symlink():
            info = path.stat()
            print(f"目录 {name}/：存在；UID/GID={info.st_uid}/{info.st_gid}；权限={stat.S_IMODE(info.st_mode):04o}")
        else:
            print(f"目录 {name}/：缺失、不是目录或是符号链接。")

    all_reachable = True
    for label, key, path in (
        ("Songloft", "SONGLOFT_PORT", "/"),
        ("免费曲库", "FREE_MUSIC_PORT", "/healthz"),
    ):
        address, port = config["LAN_IP"], int(config[key])
        reachable = tcp_reachable(address, port)
        all_reachable = all_reachable and reachable
        status = http_status(f"http://{address}:{port}{path}") if reachable else "TCP 连接失败"
        print(f"{label} {address}:{port}：{status}")

    if docker_ready:
        # Fixed format excludes environment, labels, mounts, URLs and raw logs.
        # Use the exact compose file instead of relying on a shell's COMPOSE_FILE.
        environment = os.environ.copy()
        environment.update({key: config[key] for key in ENV_KEYS})
        argv = ["docker", "compose", "--project-name", "home-music", "--file", str(BASE / "compose.yaml"),
                "--env-file", str(env_path), "ps", "--all", "--format", "json"]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=10, cwd=BASE, env=environment)
            if result.returncode == 0:
                raw = result.stdout.strip()
                if not raw:
                    rows = []
                elif raw.startswith("["):
                    rows = json.loads(raw)
                else:
                    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
                print("容器：")
                for row in rows:
                    service = row.get("Service", "")
                    if service in ("songloft", "free-music"):
                        state = row.get("State", "unknown")
                        health = row.get("Health") or "无内置健康检查"
                        print(f"  {service}: {state}; {health}")
                if not rows:
                    print("  未创建。")
            else:
                print("容器状态：暂时无法读取。")
        except (OSError, ValueError, subprocess.SubprocessError):
            print("容器状态：暂时无法读取。")

    print("本报告不包含账号密码、cookie 或 token；未读取应用数据库或应用日志。")
    print("HTTP 可达只表示服务响应；音箱语音和真实播放需在家中设备上验证。")
    return 0 if all_reachable else 1


if __name__ == "__main__":
    raise SystemExit(main())
