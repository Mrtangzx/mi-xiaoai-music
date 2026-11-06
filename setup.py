#!/usr/bin/env python3
"""Initialize and run the home music stack without overwriting existing data.

python3 setup.py                  # interactive LAN selection, then deploy
python3 setup.py --lan-ip 192.168.1.100 --init-only
python3 setup.py --lan-ip 192.168.1.100 --dry-run

This installer does not install Docker, open the firewall, configure a router,
or sign in to a Xiaomi account. It never displays generated or existing secrets.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request


BASE = Path(__file__).resolve().parent
# Release and Docker Hub tag verified on 2026-11-05; multi-platform manifest.
# https://github.com/songloft-org/songloft/releases/tag/v2.13.8
# https://hub.docker.com/v2/repositories/songloft/songloft/tags/2.13.8
SONGLOFT_IMAGE = (
    "songloft/songloft:2.13.8@sha256:"
    "2d404904dab32db885cff1c199cf0702b3e30b81c4ab32b7bea6cea36a076010"
)
RFC1918 = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
SECRET_KEYS = ("SONGLOFT_ADMIN_PASSWORD", "HOME_MUSIC_TOKEN")
ENV_KEYS = (
    "LAN_IP", "SONGLOFT_PORT", "FREE_MUSIC_PORT", "HOME_MUSIC_PUBLIC_URL",
    "SONGLOFT_IMAGE", "SONGLOFT_ADMIN_USERNAME", "SONGLOFT_ADMIN_PASSWORD",
    "HOME_MUSIC_TOKEN", "HOME_MUSIC_UID", "HOME_MUSIC_GID",
)


class SetupError(Exception):
    """A useful error which contains no account credentials."""


def private_ipv4(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value.strip())
    except ipaddress.AddressValueError as exc:
        raise SetupError("LAN_IP 必须是服务器的局域网 IPv4 地址。") from exc
    if not any(address in network for network in RFC1918):
        raise SetupError("仅接受 10.x、172.16–31.x 或 192.168.x 的局域网地址。")
    return str(address)


def read_env(path: Path) -> dict[str, str]:
    """Read the installer's simple dotenv syntax; never execute or interpolate it."""
    if path.is_symlink() or not path.is_file():
        raise SetupError(".env 必须是普通文件，不能是符号链接。")
    if path.stat().st_size > 16_384:
        raise SetupError(".env 过大，请检查配置文件。")
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SetupError(f".env 第 {number} 行不是 KEY=VALUE 格式。")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise SetupError(f".env 第 {number} 行有无效或重复的变量名。")
        if value[:1] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise SetupError(f".env 第 {number} 行的引号未闭合。")
            value = value[1:-1]
        # The generated configuration never needs Compose interpolation or escapes.
        # Reject those constructs to keep validation and Compose interpretation aligned.
        if any(char in value for char in ("$", "\\", "\x00", "\r", "\n")):
            raise SetupError(f".env 第 {number} 行包含不支持的插值或转义。")
        values[key] = value
    return values


def validate_config(values: dict[str, str]) -> None:
    missing = [key for key in ENV_KEYS if not values.get(key)]
    if missing:
        raise SetupError(".env 缺少配置项：" + ", ".join(missing))
    private_ipv4(values["LAN_IP"])
    for key in ("SONGLOFT_PORT", "FREE_MUSIC_PORT"):
        if not values[key].isdigit() or not 1024 <= int(values[key]) <= 65535:
            raise SetupError(f"{key} 必须为 1024–65535 的端口号。")
    if values["SONGLOFT_PORT"] == values["FREE_MUSIC_PORT"]:
        raise SetupError("两个服务不能使用相同的宿主机端口。")
    expected_url = f"http://{values['LAN_IP']}:{values['FREE_MUSIC_PORT']}"
    if values["HOME_MUSIC_PUBLIC_URL"].rstrip("/") != expected_url:
        raise SetupError("HOME_MUSIC_PUBLIC_URL 必须与 LAN_IP 和 FREE_MUSIC_PORT 一致。")
    for key in ("HOME_MUSIC_UID", "HOME_MUSIC_GID"):
        if not values[key].isdigit() or not 1 <= int(values[key]) < 2**31:
            raise SetupError(f"{key} 必须为大于 0 的 Linux UID/GID。")
    if not re.fullmatch(
        r"songloft/songloft:\d+\.\d+\.\d+(?:@sha256:[0-9a-f]{64})?",
        values["SONGLOFT_IMAGE"],
    ):
        raise SetupError("SONGLOFT_IMAGE 必须固定到 songloft/songloft 的正式数字版本。")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", values["SONGLOFT_ADMIN_USERNAME"]):
        raise SetupError("管理员用户名只允许英文字母、数字、下划线和短横线。")
    for key in SECRET_KEYS:
        minimum = 32 if key == "HOME_MUSIC_TOKEN" else 24
        if len(values[key]) < minimum or "REPLACE_" in values[key]:
            raise SetupError(f"{key} 尚未设置为至少 {minimum} 字符的随机凭据。")


def candidate_addresses() -> list[tuple[str, str]]:
    """Read interface addresses locally; do not scan the LAN or contact the Internet."""
    if shutil.which("ip") is None:
        return []
    try:
        result = subprocess.run(
            ["ip", "-j", "-4", "addr", "show"], capture_output=True,
            text=True, timeout=5, check=True,
        )
        records = json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        return []
    addresses = []
    seen = set()
    for interface in records:
        name = str(interface.get("ifname", ""))
        if name == "lo" or name.startswith(("docker", "br-", "veth", "virbr", "cni", "flannel")):
            continue
        for info in interface.get("addr_info", []):
            try:
                address = private_ipv4(str(info.get("local", "")))
            except SetupError:
                continue
            if address not in seen:
                addresses.append((name, address))
                seen.add(address)
    return addresses


def select_address(provided: str | None) -> str:
    if provided:
        return private_ipv4(provided)
    candidates = candidate_addresses()
    if not sys.stdin.isatty():
        raise SetupError("非交互运行请指定 --lan-ip，例如 --lan-ip 192.168.1.100。")
    print("请选择小爱音箱可以访问的服务器局域网 IP：")
    for index, (interface, address) in enumerate(candidates, 1):
        print(f"  {index}. {address} ({interface})")
    prompt = "输入序号或 IPv4 地址"
    if len(candidates) == 1:
        prompt += "，直接回车选择 1"
    answer = input(prompt + "：").strip()
    if not answer and len(candidates) == 1:
        return candidates[0][1]
    if answer.isdigit() and 1 <= int(answer) <= len(candidates):
        return candidates[int(answer) - 1][1]
    return private_ipv4(answer)


def container_ids(uid: int | None, gid: int | None) -> tuple[int, int]:
    current_uid = os.getuid() if hasattr(os, "getuid") else 1000
    current_gid = os.getgid() if hasattr(os, "getgid") else 1000
    if current_uid == 0:
        sudo_uid = os.environ.get("SUDO_UID", "")
        sudo_gid = os.environ.get("SUDO_GID", "")
        current_uid = int(sudo_uid) if sudo_uid.isdigit() and int(sudo_uid) > 0 else 1000
        current_gid = int(sudo_gid) if sudo_gid.isdigit() and int(sudo_gid) > 0 else 1000
    if uid is not None:
        current_uid = uid
    if gid is not None:
        current_gid = gid
    if not (1 <= current_uid < 2**31 and 1 <= current_gid < 2**31):
        raise SetupError("容器必须使用有效的非 root UID/GID。")
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        if current_uid != os.getuid() or current_gid != os.getgid():
            raise SetupError("普通用户请使用当前 UID/GID；修改目录属主需要在服务器上以管理员身份处理。")
    return current_uid, current_gid


def make_config(lan_ip: str, uid: int, gid: int) -> dict[str, str]:
    return {
        "LAN_IP": lan_ip,
        "SONGLOFT_PORT": "58091",
        "FREE_MUSIC_PORT": "58092",
        "HOME_MUSIC_PUBLIC_URL": f"http://{lan_ip}:58092",
        "SONGLOFT_IMAGE": SONGLOFT_IMAGE,
        "SONGLOFT_ADMIN_USERNAME": "admin",
        "SONGLOFT_ADMIN_PASSWORD": secrets.token_urlsafe(27),
        "HOME_MUSIC_TOKEN": secrets.token_urlsafe(36),
        "HOME_MUSIC_UID": str(uid),
        "HOME_MUSIC_GID": str(gid),
    }


def write_new_env(path: Path, values: dict[str, str]) -> None:
    """O_EXCL makes accidental overwrites and symlink races fail closed."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("# Private local credentials. Do not upload or share this file.\n")
            for key in ENV_KEYS:
                handle.write(f"{key}={values[key]}\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    # Keep a root-created .env owned by the invoking human account, if one exists.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        sudo_uid, sudo_gid = os.environ.get("SUDO_UID", ""), os.environ.get("SUDO_GID", "")
        if sudo_uid.isdigit() and sudo_gid.isdigit() and int(sudo_uid) > 0:
            os.chown(path, int(sudo_uid), int(sudo_gid))


def ensure_directories(values: dict[str, str]) -> None:
    uid, gid = int(values["HOME_MUSIC_UID"]), int(values["HOME_MUSIC_GID"])
    for name, mode in (("music", 0o750), ("songloft-data", 0o700), ("provider-data", 0o700)):
        path = BASE / name
        if path.is_symlink():
            raise SetupError(f"{name} 不能是符号链接；请在 compose.yaml 显式配置已有音乐挂载。")
        if path.exists():
            if not path.is_dir():
                raise SetupError(f"{name} 已存在但不是目录，未修改它。")
            st = path.stat()
            bits = (st.st_mode >> (6 if st.st_uid == uid else 3 if st.st_gid == gid else 0)) & 7
            if uid != 0 and bits & 7 != 7:
                raise SetupError(f"{name} 对配置的 UID {uid}/GID {gid} 不可读写或遍历；请核对目录所有者与权限后重试，安装器未修改已有权限。")
            continue
        path.mkdir(mode=mode)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(path, uid, gid)


def seed_demo_music(values: dict[str, str]) -> None:
    """Copy the two attributed demo recordings only when destination is absent."""
    source = BASE / "demo-music"
    if not source.is_dir():
        return
    destination = BASE / "music" / "免费试听曲"
    if destination.is_symlink():
        raise SetupError("免费试听曲目录不能是符号链接。")
    destination.mkdir(exist_ok=True, mode=0o750)
    uid, gid = int(values["HOME_MUSIC_UID"]), int(values["HOME_MUSIC_GID"])
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        os.chown(destination, uid, gid)
    count = 0
    for original in source.iterdir():
        if original.is_symlink() or not original.is_file():
            continue
        target = destination / original.name
        if target.exists() or target.is_symlink():
            continue
        with original.open("rb") as incoming, target.open("xb") as output:
            shutil.copyfileobj(incoming, output)
        target.chmod(0o640)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(target, uid, gid)
        if target.suffix.lower() == ".mp3":
            count += 1
    if count:
        print(f"已添加 {count} 首开放授权试听曲及来源说明，已有文件保留。")


def redact(text: str, values: dict[str, str]) -> str:
    for key in SECRET_KEYS:
        value = values.get(key)
        if value:
            text = text.replace(value, "[已隐藏]")
    return text


def run_command(argv: list[str], label: str, values: dict[str, str], timeout: int = 30) -> str:
    print(label, flush=True)
    environment = os.environ.copy()
    # Generated .env values deliberately win over similarly named shell variables.
    environment.update({key: values[key] for key in ENV_KEYS})
    try:
        result = subprocess.run(
            argv, cwd=BASE, env=environment, capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SetupError("操作超时；配置和音乐已保留，可排查网络后重新运行。") from exc
    except OSError as exc:
        raise SetupError("无法启动 Docker 命令；请确认 Docker 已正确安装。") from exc
    if result.returncode:
        detail = redact(result.stderr or result.stdout, values)[-3500:]
        raise SetupError(f"{label}失败（退出码 {result.returncode}）。\n{detail}")
    return result.stdout.strip()


def docker_preflight(values: dict[str, str]) -> None:
    if platform.system() != "Linux":
        raise SetupError("此安装脚本面向 Linux 服务器；当前系统可用 --init-only 准备配置。")
    if not shutil.which("docker"):
        raise SetupError("没有找到 Docker。请先安装 Docker Engine 和 Compose v2，再重新运行。")
    compose_version = run_command(["docker", "compose", "version", "--short"], "检查 Docker Compose…", values)
    if not re.match(r"^v?[2-9](?:\.|$)", compose_version):
        raise SetupError("需要 Docker Compose v2 或更新版本。")
    if os.environ.get("DOCKER_HOST"):
        endpoint = os.environ["DOCKER_HOST"]
    else:
        endpoint = run_command(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            "检查 Docker 运行位置…", values,
        )
    if not endpoint.startswith("unix://"):
        raise SetupError("当前 Docker 指向远程或非 Unix 端点；请在家庭服务器上使用本地 Docker。")
    run_command(["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"], "检查 Docker 服务权限…", values)
    # Binding port zero verifies that the chosen address actually belongs to this host.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((values["LAN_IP"], 0))
    except OSError as exc:
        raise SetupError("LAN_IP 不属于这台服务器，或本地不允许绑定该地址；请检查 .env。") from exc
    if not (BASE / "provider" / "server.py").is_file():
        raise SetupError("缺少 provider/server.py；请完整解压项目后运行。")


def wait_for_http(values: dict[str, str], seconds: int = 45) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    urls = [
        f"http://{values['LAN_IP']}:{values['SONGLOFT_PORT']}/",
        f"http://{values['LAN_IP']}:{values['FREE_MUSIC_PORT']}/healthz",
    ]
    deadline = time.monotonic() + seconds
    pending = set(urls)
    while pending and time.monotonic() < deadline:
        for url in tuple(pending):
            try:
                with opener.open(url, timeout=2) as response:
                    if response.status < 400:
                        pending.remove(url)
            except (OSError, urllib.error.URLError):
                pass
        if pending:
            time.sleep(2)
    return not pending


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初始化并部署家庭音乐服务；不会覆盖已有 .env 或音乐数据。")
    parser.add_argument("--lan-ip", help="服务器局域网 IPv4 地址")
    parser.add_argument("--uid", type=int, help="容器运行 UID；默认当前用户，root 默认 1000")
    parser.add_argument("--gid", type=int, help="容器运行 GID；默认当前用户，root 默认 1000")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init-only", action="store_true", help="仅生成 .env、数据目录和试听曲，不调用 Docker")
    mode.add_argument("--dry-run", action="store_true", help="仅检查并显示计划，不写任何文件、不调用 Docker")
    args = parser.parse_args(argv)

    env_path = BASE / ".env"
    if env_path.exists() or env_path.is_symlink():
        values = read_env(env_path)
        validate_config(values)
        if args.lan_ip and private_ipv4(args.lan_ip) != values["LAN_IP"]:
            raise SetupError("已有 .env 的 LAN_IP 不同，未覆盖；请先在本机编辑配置再重启。")
        for arg, key in ((args.uid, "HOME_MUSIC_UID"), (args.gid, "HOME_MUSIC_GID")):
            if arg is not None and str(arg) != values[key]:
                raise SetupError(f"已有 .env 的 {key} 不同，未覆盖。")
        new_config = False
        print("检测到已有 .env；将保留原账号、密码、地址和音乐。")
        if os.name == "posix" and stat.S_IMODE(env_path.stat().st_mode) & 0o077:
            print("提示：.env 的权限较宽；可在本机执行 chmod 600 .env。")
    else:
        uid, gid = container_ids(args.uid, args.gid)
        values = make_config(select_address(args.lan_ip), uid, gid)
        validate_config(values)
        new_config = True

    print(f"Songloft 地址：http://{values['LAN_IP']}:{values['SONGLOFT_PORT']}")
    print(f"免费曲库服务：http://{values['LAN_IP']}:{values['FREE_MUSIC_PORT']}")
    print("已有歌曲目录：music/；服务数据目录：songloft-data/ 与 provider-data/。")
    if args.dry_run:
        print("检查完成；未生成密码文件、数据目录或容器。")
        return 0
    if new_config:
        write_new_env(env_path, values)
        print("已生成 .env（权限 0600）；请在本机用文本编辑器查看初始管理员密码。")
    ensure_directories(values)
    seed_demo_music(values)
    if args.init_only:
        print("初始化完成；尚未部署。准备好 Docker 后执行 python3 setup.py。")
        return 0

    docker_preflight(values)
    compose = ["docker", "compose", "--project-name", "home-music", "--file", str(BASE / "compose.yaml"), "--env-file", str(env_path)]
    run_command(compose + ["config", "--quiet"], "校验部署配置…", values)
    run_command(compose + ["pull", "songloft"], "拉取固定版本的 Songloft（首次可能需要几分钟）…", values, timeout=1800)
    run_command(compose + ["build", "free-music"], "构建免费音乐服务（首次需要下载 Python 和 FFmpeg）…", values, timeout=1800)
    run_command(compose + ["up", "--detach", "--no-build"], "启动两个服务…", values, timeout=120)
    print("等待服务响应…", flush=True)
    if wait_for_http(values):
        print("两个 HTTP 服务均已响应。")
    else:
        print("容器已请求启动，但服务暂未全部就绪。请执行 python3 doctor.py 排查。")
        return 1
    connector = BASE / "connect_songloft.py"
    if connector.is_file():
        output = run_command([sys.executable, str(connector)], "安装并配置智能音箱插件…", values, timeout=240)
        if output:
            print(redact(output, values))
    print("小米账号只在你自己的 Songloft MIoT 插件中登录；不要发送 .env 或小米凭据。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SetupError, OSError, UnicodeError) as error:
        print(f"安装未完成：{error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\n已取消。已有配置和音乐保留。", file=sys.stderr)
        raise SystemExit(130)
