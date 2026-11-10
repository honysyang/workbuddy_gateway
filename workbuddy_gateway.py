#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workbuddy-gateway
=================
把本机 WorkBuddy 的模型封装成 OpenAI 兼容 API，只监听 127.0.0.1。

链路:
    OpenAI 客户端 -> 本脚本(HTTP) -> ACP JSON-RPC -> codebuddy --serve -> WorkBuddy 云端

用法:
    python workbuddy_gateway.py                 # 起网关，默认 127.0.0.1:8788
    python workbuddy_gateway.py --selftest      # 只验证上游链路，列模型 + 跑一次对话
    python workbuddy_gateway.py --no-reuse      # 不复用桌面端，自己拉一个上游

两种上游模式（--no-reuse 可强制第二种）:
  1) 复用模式：发现 WorkBuddy 桌面端预热好的本地服务并接上去。
     优点是完全继承桌面端的登录态与产品配置，最稳。
  2) 自建模式：自己拉起 codebuddy --serve。
     不依赖桌面端开着，但需要自行注入登录态与产品配置（脚本已自动处理）。

实测踩到的三个坑（都已在此文件里处理）:
  * session/new 返回 500 且内部报 ENOENT/EPERM：
    CLI 写 <config>/projects/<cwd 的 slug>/<uuid>.jsonl 时不建父目录。
    -> ensure_project_dir() 预建，必要时回退到可写目录。
  * 只给 CODEBUDDY_AUTH_TOKEN 时模型集会退化成 CodeBuddy 默认那套（9 个），
    拿不到 WorkBuddy 的 hy4free/hy3/space-bunny 等。
    -> 还需要 ACC_PRODUCT_CONFIG_V3，内容取自桌面端按 userId 缓存的
       云端产品配置（local_storage/*.info），load_cached_product_config()。
  * 每条请求新建 ACP 连接而不释放，会耗尽上游连接额度、后续请求一直挂住。
    -> 一条会话绑定一条连接，淘汰时 DELETE 释放。

安全提醒:
  只绑 127.0.0.1。把它暴露到公网 = 共享/转售你的 WorkBuddy 额度，违反服务条款
  且大概率被风控封号。
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from typing import Any, Callable, Iterable

# ---------------------------------------------------------------- 默认路径

DEFAULT_ELECTRON = r"D:\腾讯下载\wb\WorkBuddy\WorkBuddy.exe"
DEFAULT_CLI = r"D:\腾讯下载\wb\WorkBuddy\resources\app.asar.unpacked\cli\bin\codebuddy"
DEFAULT_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy")

# 桌面端把登录态存在这里（account.uid + auth.accessToken/refreshToken）
AUTH_INFO_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "CodeBuddyExtension", "Data", "Public", "auth",
                 "workbuddy-desktop.info"),
]

GATEWAY_HOST = "127.0.0.1"
DEFAULT_GATEWAY_PORT = 8788
DEFAULT_UPSTREAM_PORT = 18899
UPSTREAM_BOOT_TIMEOUT = 180.0

# 局域网模式下默认只放行私有网段，避免误暴露到公网网卡
DEFAULT_LAN_CIDRS = (
    "127.0.0.1/32",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",   # 链路本地
    "::1/128",
    "fe80::/10",
    "fc00::/7",
)


def log(*a: Any) -> None:
    print("[wb-gw]", *a, flush=True)


# ---------------------------------------------------------------- 网络工具


def parse_networks(spec: str | None) -> list:
    """把 "192.168.1.0/24,10.0.0.0/8" 解析成网段列表；空值用默认私有网段。"""
    raw = (spec or "").strip()
    if not raw:
        raw = ",".join(DEFAULT_LAN_CIDRS)
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log(f"忽略无效网段: {part}")
    return nets


def ip_in_networks(ip: str, nets: list) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    # 处理 IPv4-mapped IPv6，例如 ::ffff:192.168.1.5
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    for net in nets:
        try:
            if addr in net:
                return True
        except TypeError:
            continue
    return False


def is_loopback_host(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")


GATEWAY_CONFIG_NAME = "gateway.json"


def config_candidates(workdir: str) -> list[str]:
    """配置文件候选路径：先看工作目录，再看脚本所在目录。"""
    here = os.path.dirname(os.path.abspath(__file__))
    out: list[str] = []
    for base in (workdir, here):
        path = os.path.abspath(os.path.join(base, ".workbuddy-gateway", GATEWAY_CONFIG_NAME))
        if path not in out:
            out.append(path)
    return out


def load_gateway_config(workdir: str) -> tuple[dict, str]:
    """读取持久化配置 -> (配置字典, 命中路径)。没有就返回空配置。"""
    for path in config_candidates(workdir):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data, path
        except Exception as e:
            log(f"配置文件解析失败 {path}: {e}")
    return {}, ""


def save_gateway_config(path: str, data: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        return True
    except Exception as e:
        log(f"写入配置失败 {path}: {e}")
        return False


def default_config_path(workdir: str) -> str:
    return config_candidates(workdir)[0]


def load_or_create_key(path: str) -> str:
    """读取或生成持久化的 API Key，避免每次重启都换 Key 导致客户端失效。"""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
    except Exception:
        pass
    key = secrets.token_urlsafe(24)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(key + "\n")
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
    except Exception as e:
        log(f"无法保存 API Key 到 {path}: {e}")
    return key


def local_ipv4_addresses() -> list[str]:
    out: list[str] = []
    # 主路由探测：连一个外部地址但不真的发包
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        out.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out:
                out.append(ip)
    except Exception:
        pass
    return [ip for ip in out if not ip.startswith("127.")]


# ---------------------------------------------------------------- 登录态发现


def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload = payload.replace("-", "+").replace("_", "/")
        payload += "=" * (-len(payload) % 4)
        return json.loads(__import__("base64").urlsafe_b64decode(payload + "==="))
    except Exception:
        return {}


def _jwt_exp(token: str) -> int:
    try:
        return int(_jwt_claims(token).get("exp") or 0)
    except Exception:
        return 0


def discover_token(explicit: str | None = None) -> tuple[str, str]:
    """找到 WorkBuddy 的 accessToken -> (token, 来源描述)。找不到返回 ("", "")。"""
    if explicit:
        return explicit.strip(), "--auth-token"
    for env_key in ("WB_AUTH_TOKEN", "CODEBUDDY_AUTH_TOKEN"):
        v = (os.environ.get(env_key) or "").strip()
        if v:
            return v, f"env {env_key}"

    for path in AUTH_INFO_CANDIDATES:
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                info = json.load(f)
        except Exception as e:
            log(f"读取登录态文件失败 {path}: {e}")
            continue
        auth = info.get("auth") or {}
        for key in ("accessToken", "token"):
            tok = (auth.get(key) or "").strip()
            if not tok:
                continue
            exp = _jwt_exp(tok)
            if exp:
                left = exp - int(time.time())
                if left <= 0:
                    log(f"accessToken 已过期（{left // 86400} 天），尝试 refreshToken")
                    continue
                log(f"找到登录态: uid={(info.get('account') or {}).get('uid')} "
                    f"有效期还剩 {left // 3600} 小时")
            return tok, path
    return "", ""


def load_cached_product_config(config_dir: str, uid: str) -> dict | None:
    """读取桌面端缓存的云端产品配置（按 userId 存于 local_storage）。

    这份配置里才有 WorkBuddy 的模型清单（hy4free / hy3 / space-bunny ...）。
    没有它，CLI 会退回 CodeBuddy 的默认模型集，能跑但不是 WorkBuddy 那套。
    """
    if not uid:
        return None
    folder = os.path.join(config_dir, "local_storage")
    if not os.path.isdir(folder):
        return None
    best: dict | None = None
    try:
        names = os.listdir(folder)
    except Exception:
        return None
    for name in names:
        if not name.endswith(".info"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                obj = json.load(f)
        except Exception:
            continue
        items = obj if isinstance(obj, list) else [obj]
        for it in items:
            if not isinstance(it, dict):
                continue
            data = it.get("data")
            if not isinstance(data, dict) or not data.get("models"):
                continue
            if it.get("userId") == uid:
                # 同一用户可能有多份，取模型最多的那份
                if best is None or len(data["models"]) > len(best.get("models") or []):
                    best = data
    return best


def build_product_config(token: str, product_data: dict | None = None) -> str:
    """构造 ACC_PRODUCT_CONFIG_V3。

    优先用桌面端缓存的产品配置（能拿到 WorkBuddy 全套模型），
    退化为只声明 custom-token 登录态的最小配置。
    """
    cfg: dict = dict(product_data) if product_data else {}
    cfg.pop("authentication", None)
    cfg["authentication"] = {"type": "custom-token", "attributes": {"token": token}}
    return json.dumps(cfg, ensure_ascii=False)


def project_slug(cwd: str) -> str:
    """把工作目录映射成 WorkBuddy 的 projects 目录名。

    例：G:\\dsh -> g-dsh
        C:\\Users\\x\\AppData\\Local\\Temp\\a -> c-Users-x-AppData-Local-Temp-a
    规则：盘符小写，去掉 ':'，路径分隔符统一成 '-'。
    """
    p = os.path.abspath(cwd)
    p = p.replace(":", "").replace("\\", "-").replace("/", "-")
    if p and p[0].isalpha():
        p = p[0].lower() + p[1:]
    return p


def ensure_project_dir(config_dir: str, cwd: str) -> str:
    """确保会话记录目录存在。

    CLI 写 <config>/projects/<slug>/<uuid>.jsonl 时不会自己建父目录，
    缺目录会让 session/new 直接 500（内部报 ENOENT）。
    """
    target = os.path.join(config_dir, "projects", project_slug(cwd))
    try:
        os.makedirs(target, exist_ok=True)
    except Exception as e:
        log(f"无法创建会话目录 {target}: {e}")
    return target


def dir_writable(path: str) -> bool:
    """真写一个临时文件来判断目录可写（只 os.access 在 Windows 上不可靠）。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, f".probe-{uuid.uuid4().hex[:8]}")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 活 host 发现

SESSIONS_DIR = os.path.join(DEFAULT_CONFIG_DIR, "sessions")
MAIN_LOG = os.path.join(os.environ.get("LOCALAPPDATA", ""), "WorkBuddy", "logs", "main.log")


def _gateway_password() -> str:
    """取桌面端为本地 HTTP 服务生成的密码。

    优先级：环境变量 > main.log 里最近一次启动横幅。
    这个密码每次 App 启动都会重新生成，所以必须现取。
    """
    v = (os.environ.get("WB_UPSTREAM_PASSWORD") or "").strip()
    if v:
        return v
    if not os.path.exists(MAIN_LOG):
        return ""
    try:
        tail = ""
        with open(MAIN_LOG, "r", encoding="utf-8", errors="replace") as f:
            # 只读末尾 2MB，日志可能很大
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 2 * 1024 * 1024))
            tail = f.read()
    except Exception:
        return ""
    found = re.findall(r"Password\s+([A-Za-z0-9_\-]{20,})", tail)
    return found[-1] if found else ""


def discover_live_host(max_age_sec: float = 300.0) -> tuple[str, str] | None:
    """找到正在运行的 WorkBuddy 本地服务 -> (url, password)。

    桌面端会持续预热一个 CLI host 并写心跳文件；复用它就不需要自己 fork，
    也自然继承了完整的登录态和产品配置。
    """
    password = _gateway_password()
    if not password:
        log("未能获取本地服务密码（main.log 里没有），跳过活 host 发现")
        return None
    if not os.path.isdir(SESSIONS_DIR):
        return None

    now_ms = time.time() * 1000
    candidates: list[tuple[float, str]] = []
    for name in os.listdir(SESSIONS_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(SESSIONS_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                info = json.load(f)
        except Exception:
            continue
        url = (info.get("endpoint") or info.get("url") or "").strip()
        beat = float(info.get("lastHeartbeat") or 0)
        if not url or now_ms - beat > max_age_sec * 1000:
            continue
        candidates.append((beat, url))

    for beat, url in sorted(candidates, reverse=True):
        try:
            c = AcpClient(url, password)
            c.connect()
            log(f"发现可用活 host: {url}（心跳 {int((now_ms - beat) / 1000)}s 前）")
            return url, password
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- SSE 解析


def iter_sse(resp: Any) -> Iterable[dict]:
    """从 HTTP 响应流里逐个吐出 JSON 消息（兼容 SSE 与裸 JSON 行）。"""
    data_lines: list[str] = []
    for raw in resp:
        try:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
        except Exception:
            continue
        if not line:
            if data_lines:
                blob = "\n".join(data_lines)
                data_lines = []
                msg = _try_json(blob)
                if msg is not None:
                    yield msg
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line.startswith("event:") or line.startswith("id:") or line.startswith("retry:"):
            continue
        else:
            # 非 SSE 的裸 JSON 流（每行一条）
            msg = _try_json(line)
            if msg is not None:
                yield msg
    if data_lines:
        msg = _try_json("\n".join(data_lines))
        if msg is not None:
            yield msg


def _try_json(blob: str) -> Any:
    blob = blob.strip()
    if not blob:
        return None
    try:
        return json.loads(blob)
    except Exception:
        return None


# ---------------------------------------------------------------- 上游进程


class Upstream:
    """负责拉起并守护 `codebuddy --serve`，或复用一个已在跑的服务。"""

    def __init__(self,
                 electron: str,
                 cli: str,
                 port: int,
                 password: str,
                 config_dir: str,
                 cwd: str,
                 reuse: bool = True,
                 auth_token: str = "",
                 extra_env: dict | None = None,
                 inject_product_config: bool = True,
                 extra_args: list[str] | None = None,
                 omit_args: set[str] | None = None):
        self.electron = electron
        self.cli = cli
        self.port = port
        self.password = password
        self.config_dir = config_dir
        self.source_config_dir = config_dir  # 只读也留着，用来读产品配置
        self.cwd = cwd
        self.reuse = reuse
        self.auth_token = auth_token
        self.extra_env = dict(extra_env or {})
        self.inject_product_config = inject_product_config
        self.extra_args = list(extra_args or [])
        self.omit_args = set(omit_args or ())
        self.proc: subprocess.Popen | None = None
        self.owns_proc = False
        self.attached_url: str | None = None
        self._log_tail: list[str] = []
        self._log_lock = threading.Lock()

    # -- 基本探活

    @property
    def base_url(self) -> str:
        return self.attached_url or f"http://127.0.0.1:{self.port}"

    def port_open(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.35)
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def probe(self) -> bool:
        """POST /api/v1/acp/connect，200 即认为可用。"""
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/v1/acp/connect",
                data=b"{}",
                headers=self._headers(),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "x-codebuddy-request": "1",
            "Authorization": f"Bearer {self.password}",
        }

    # -- 启动

    def ensure(self) -> None:
        if self.reuse:
            found = discover_live_host()
            if found:
                self.attached_url, self.password = found
                log(f"复用桌面端已登录的本地服务: {self.attached_url}")
                return
            log("没有可用的活 host，改为自己拉起一个 CLI 服务")
        self.start()

    def start(self) -> None:
        if self.port_open():
            raise RuntimeError(
                f"端口 {self.port} 已被占用但握手失败。换一个 --upstream-port，"
                f"或确认占用它的是不是别的程序。"
            )
        for p in (self.electron, self.cli):
            if not os.path.exists(p):
                raise RuntimeError(f"找不到文件: {p}")

        # 必须先建好项目目录，否则 session/new 会因 ENOENT/EPERM 返回 500
        target = ensure_project_dir(self.config_dir, self.cwd)
        if not dir_writable(target):
            alt = os.path.join(self.cwd, ".workbuddy-gateway")
            log(f"配置目录 {self.config_dir} 不可写，改用 {alt}")
            self.config_dir = alt
            ensure_project_dir(self.config_dir, self.cwd)

        env = dict(os.environ)
        env.update({
            # 让 Electron 二进制以 Node 模式运行 CLI
            "ELECTRON_RUN_AS_NODE": "1",
            "DISABLE_AUTOUPDATER": "1",
            # 固定网关密码，避免解析随机值
            "CODEBUDDY_GATEWAY_AUTH": "password",
            "CODEBUDDY_GATEWAY_PASSWORD": self.password,
            "CODEBUDDY_GATEWAY_DISABLE_API_DOCS": "1",
            # 继承 WorkBuddy 的配置目录（登录态所在）
            "WORKBUDDY_CONFIG_DIR": self.config_dir,
            "CODEBUDDY_CONFIG_DIR": self.config_dir,
            # 关掉一批会拖慢/干扰启动的东西
            "CODEBUDDY_DISABLE_REQUEST_VALIDATION": "1",
            "CODEBUDDY_SKIP_GIT_BASH_CHECK": "1",
            "CODEBUDDY_DISABLE_CRON": "1",
            "CODEBUDDY_PROMPT_SUGGESTION_DISABLED": "1",
            "CODEBUDDY_GIT_REPO_SCAN_DISABLED": "1",
            "CODEBUDDY_CODE_DISABLE_SESSION_SUMMARY": "1",
            "CODEBUDDY_DISABLE_AUTO_MEMORY": "1",
            "CODEBUDDY_DISABLE_SYSTEM_REMINDER_MD": "1",
        })

        # 注入登录态：这决定 CLI 能不能代表你的 WorkBuddy 账号发请求
        if self.auth_token:
            env["CODEBUDDY_AUTH_TOKEN"] = self.auth_token
            if self.inject_product_config:
                uid = str(_jwt_claims(self.auth_token).get("sub") or "")
                # 产品配置始终从「原始」配置目录读，因为它可能只读不可写
                data = (load_cached_product_config(self.source_config_dir, uid)
                        or load_cached_product_config(self.config_dir, uid))
                if data:
                    n = len(data.get("models") or [])
                    log(f"已载入 WorkBuddy 产品配置（候选模型 {n} 个）")
                else:
                    log("未找到缓存的产品配置，使用最小配置（模型集可能不是 WorkBuddy 那套）")
                env["ACC_PRODUCT_CONFIG_V3"] = build_product_config(self.auth_token, data)
            log("已注入 WorkBuddy 登录态（custom-token）")
        else:
            log("!! 未找到登录态，上游大概率会在 session/new 返回 500")

        if self.extra_env:
            env.update({k: v for k, v in self.extra_env.items() if v is not None})

        # 以 (flag, values...) 组织，便于按需精简，避免参数不匹配导致启动失败
        groups: list[tuple[str, list[str]]] = [
            ("--serve", []),
            ("--host", ["127.0.0.1"]),
            ("--port", [str(self.port)]),
            ("--setting-sources", ["user"]),
            # 关键：关闭全部内置工具，让模型只做纯对话
            ("--tools", [""]),
            # 不加载任何 MCP server
            ("--mcp-config", ['{"mcpServers":{}}']),
            ("--strict-mcp-config", []),
        ]
        argv = [self.electron, self.cli]
        for flag, values in groups:
            if flag in self.omit_args:
                continue
            argv.append(flag)
            argv.extend(values)
        argv.extend(self.extra_args)

        flags = 0
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

        log(f"启动上游: {self.base_url}  (cwd={self.cwd})")
        self.proc = subprocess.Popen(
            argv,
            cwd=self.cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        self.owns_proc = True
        threading.Thread(target=self._drain, name="upstream-log", daemon=True).start()

        deadline = time.time() + UPSTREAM_BOOT_TIMEOUT
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    "上游进程启动后立即退出。最后输出:\n" + self.tail()
                )
            if self.port_open() and self.probe():
                log(f"上游就绪，用时 {UPSTREAM_BOOT_TIMEOUT - (deadline - time.time()):.1f}s")
                return
            time.sleep(0.6)
        raise RuntimeError(f"上游 {UPSTREAM_BOOT_TIMEOUT:.0f}s 内未就绪。最后输出:\n" + self.tail())

    def _drain(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                line = raw.decode("utf-8", "replace").rstrip()
            except Exception:
                continue
            if not line:
                continue
            with self._log_lock:
                self._log_tail.append(line)
                if len(self._log_tail) > 200:
                    del self._log_tail[:-200]

    def tail(self, n: int = 15) -> str:
        with self._log_lock:
            return "\n".join(self._log_tail[-n:]) or "(无输出)"

    def stop(self) -> None:
        if self.owns_proc and self.proc and self.proc.poll() is None:
            log("停止上游进程")
            try:
                self.proc.terminate()
                self.proc.wait(timeout=8)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass


# ---------------------------------------------------------------- ACP 客户端


class AcpError(RuntimeError):
    pass


class AcpClient:
    """ACP JSON-RPC over HTTP。通知随 POST 响应流一起回来（与官方 Web UI 一致）。"""

    def __init__(self, base_url: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.connection_id: str | None = None
        self.session_token: str | None = None
        self._next_id = 1
        self._id_lock = threading.Lock()

    def _headers(self) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "x-codebuddy-request": "1",
            "Authorization": f"Bearer {self.password}",
        }
        if self.connection_id:
            h["acp-connection-id"] = self.connection_id
        if self.session_token:
            h["acp-session-token"] = self.session_token
        return h

    def connect(self) -> None:
        req = urllib.request.Request(
            f"{self.base_url}/api/v1/acp/connect",
            data=b"{}",
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read() or b"{}")
        self.connection_id = body.get("connectionId")
        self.session_token = body.get("sessionToken")
        log(f"ACP 已连接 connectionId={self.connection_id}")

    def disconnect(self) -> None:
        """释放 ACP 连接。不释放会让上游的连接额度被逐步耗尽。"""
        cid = self.connection_id
        if not cid:
            return
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/v1/acp",
                headers=self._headers(),
                method="DELETE",
            )
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:
            pass
        finally:
            self.connection_id = None
            self.session_token = None

    def rpc(self,
            method: str,
            params: dict,
            on_notification: Callable[[dict], None] | None = None,
            timeout: float = 600.0) -> dict:
        with self._id_lock:
            rid = self._next_id
            self._next_id += 1

        payload = json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
            ensure_ascii=False,
        ).encode("utf-8")

        req = urllib.request.Request(
            f"{self.base_url}/api/v1/acp",
            data=payload,
            headers=self._headers(),
            method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:800]
            raise AcpError(f"{method} -> HTTP {e.code}: {detail}") from None

        ctype = (resp.headers.get("Content-Type") or "").lower()
        try:
            if "event-stream" in ctype or "stream" in ctype:
                for msg in iter_sse(resp):
                    if not isinstance(msg, dict):
                        continue
                    if msg.get("id") == rid:
                        if "error" in msg:
                            raise AcpError(f"{method} 失败: {json.dumps(msg['error'], ensure_ascii=False)}")
                        return msg.get("result") or {}
                    if "method" in msg and on_notification is not None:
                        on_notification(msg)
            else:
                raw = resp.read()
                msg = _try_json(raw.decode("utf-8", "replace"))
                if not isinstance(msg, dict):
                    raise AcpError(f"{method} 返回无法解析: {raw[:300]!r}")
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise AcpError(f"{method} 失败: {json.dumps(msg['error'], ensure_ascii=False)}")
                    return msg.get("result") or {}
                # 少数实现会直接返回裸 result
                if "result" in msg or "models" in msg or "sessionId" in msg:
                    return msg.get("result") or msg
                raise AcpError(f"{method} 返回不匹配: {json.dumps(msg, ensure_ascii=False)[:300]}")
        finally:
            try:
                resp.close()
            except Exception:
                pass
        raise AcpError(f"{method} 没有拿到结果")

    # -- 高层封装

    def initialize(self) -> dict:
        return self.rpc("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "workbuddy-gateway", "version": "0.1.0"},
            "clientCapabilities": {"_meta": {"codebuddy.ai": {"question": True, "promptSuggestion": False}}},
        })

    def new_session(self, cwd: str) -> dict:
        return self.rpc("session/new", {"cwd": cwd, "mcpServers": []})

    def set_model(self, session_id: str, model_id: str) -> dict:
        try:
            return self.rpc("session/set_model", {"sessionId": session_id, "modelId": model_id})
        except AcpError as e:
            log(f"设置模型失败({model_id})，沿用默认: {e}")
            return {}

    def prompt(self,
               session_id: str,
               blocks: list[dict],
               on_notification: Callable[[dict], None] | None = None) -> dict:
        return self.rpc("session/prompt",
                        {"sessionId": session_id, "prompt": blocks},
                        on_notification=on_notification)


# ---------------------------------------------------------------- 模型归一化


def extract_models(payload: Any) -> list[dict]:
    """从 session/new 结果里挖出模型列表，兼容多种形状。"""
    found: list[dict] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6 or node is None:
            return
        if isinstance(node, list):
            for x in node:
                walk(x, depth + 1)
            return
        if not isinstance(node, dict):
            return
        # 看起来像一个模型条目
        if any(k in node for k in ("modelId", "id")) and any(
            k in node for k in ("name", "displayName", "contextWindow", "maxTokens")
        ):
            mid = node.get("modelId") or node.get("id")
            if isinstance(mid, str) and mid:
                found.append({
                    "id": mid,
                    "name": node.get("name") or node.get("displayName") or mid,
                    "context_window": node.get("contextWindow") or node.get("context_window"),
                    "max_tokens": node.get("maxTokens") or node.get("max_tokens") or node.get("maxOutput"),
                    "supports_images": node.get("supportsImages") or node.get("supports_images"),
                    "raw": node,
                })
        for k in ("models", "availableModels", "available_models", "items", "list", "options"):
            if k in node:
                walk(node[k], depth + 1)

    walk(payload)
    # 去重保序
    seen: OrderedDict[str, dict] = OrderedDict()
    for m in found:
        seen.setdefault(m["id"], m)
    return list(seen.values())


# ---------------------------------------------------------------- 会话池


class SessionPool:
    """按「已确认的消息前缀」复用会话，实现多轮上下文。

    一条会话绑定一条 ACP 连接；会话被淘汰时同步释放连接，
    否则上游的连接额度会被慢慢耗光（表现为后续请求一直挂住）。
    """

    def __init__(self, max_size: int = 24):
        self.max_size = max_size
        self._lock = threading.Lock()
        # key: 期望的下一轮前缀哈希 -> {session_id, model, client, lock}
        self._by_prefix: "OrderedDict[str, dict]" = OrderedDict()

    @staticmethod
    def fingerprint(messages: list[dict], model: str) -> str:
        h = hashlib.sha256()
        h.update(model.encode("utf-8"))
        for m in messages:
            h.update(b"\x00")
            h.update(str(m.get("role", "")).encode("utf-8"))
            h.update(b"\x01")
            h.update(_message_text(m).encode("utf-8"))
        return h.hexdigest()

    def get(self, key: str) -> dict | None:
        with self._lock:
            rec = self._by_prefix.get(key)
            if rec is not None:
                self._by_prefix.move_to_end(key)
            return rec

    def put(self, key: str, rec: dict) -> None:
        evicted: list[dict] = []
        with self._lock:
            old = self._by_prefix.pop(key, None)
            if old is not None and old is not rec:
                evicted.append(old)
            self._by_prefix[key] = rec
            self._by_prefix.move_to_end(key)
            while len(self._by_prefix) > self.max_size:
                _, victim = self._by_prefix.popitem(last=False)
                evicted.append(victim)
        for r in evicted:
            _close_record(r)

    def drop(self, key: str) -> dict | None:
        with self._lock:
            return self._by_prefix.pop(key, None)

    def drain(self) -> None:
        with self._lock:
            victims = list(self._by_prefix.values())
            self._by_prefix.clear()
        for r in victims:
            _close_record(r)


def _close_record(rec: dict) -> None:
    c = rec.get("client")
    if c is not None:
        try:
            c.disconnect()
        except Exception:
            pass


def _message_text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for part in c:
            if isinstance(part, dict) and part.get("type") == "text":
                out.append(part.get("text") or "")
            elif isinstance(part, str):
                out.append(part)
        return "".join(out)
    return "" if c is None else str(c)


# ---------------------------------------------------------------- 消息转换

IMAGE_DATA_URL = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.S)


def build_prompt(messages: list[dict], continuation: bool) -> list[dict]:
    """把 OpenAI messages 变成 ACP 的 prompt content blocks。"""
    blocks: list[dict] = []

    if continuation:
        tail = messages[-1] if messages else {"content": ""}
        blocks.extend(_content_blocks(tail.get("content")))
        if not blocks:
            blocks = [{"type": "text", "text": "继续"}]
        return blocks

    # 全新会话：整段历史拍平成 transcript，system 提到最前
    system_parts: list[str] = []
    transcript: list[str] = []
    last_user_blocks: list[dict] = []

    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break

    for i, m in enumerate(messages):
        role = (m.get("role") or "user").lower()
        text = _message_text(m)
        if role == "system":
            system_parts.append(text)
            continue
        if i == last_user_idx:
            last_user_blocks = _content_blocks(m.get("content"))
            continue
        label = "User" if role == "user" else "Assistant" if role == "assistant" else role
        if text.strip():
            transcript.append(f"<{label}>\n{text.strip()}\n</{label}>")

    head: list[str] = []
    if system_parts:
        head.append("\n\n".join(system_parts).strip())
    if transcript:
        head.append("以下是此前的对话记录，供你了解上下文：\n" + "\n\n".join(transcript))

    if head:
        blocks.append({"type": "text", "text": "\n\n".join(head)})
    blocks.extend(last_user_blocks)
    if not blocks:
        blocks = [{"type": "text", "text": "你好"}]
    return blocks


def _content_blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": "" if content is None else str(content)}]
    out: list[dict] = []
    for part in content:
        if isinstance(part, str):
            out.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            out.append({"type": "text", "text": part.get("text") or ""})
        elif ptype in ("image_url", "image"):
            url = ""
            if isinstance(part.get("image_url"), dict):
                url = part["image_url"].get("url") or ""
            elif isinstance(part.get("image_url"), str):
                url = part["image_url"]
            elif isinstance(part.get("source"), dict) and part["source"].get("type") == "base64":
                src = part["source"]
                out.append({"type": "image", "data": src.get("data") or "",
                            "mimeType": src.get("media_type") or "image/png"})
                continue
            mm = IMAGE_DATA_URL.match(url or "")
            if mm:
                out.append({"type": "image", "data": mm.group(2), "mimeType": mm.group(1)})
        # 其它类型（如工具结果）在纯对话场景直接忽略
    return out


def notification_text(msg: dict) -> tuple[str, str]:
    """从 session/update 通知里取出增量文本 -> (文本, 类型)。"""
    if msg.get("method") not in ("session/update", "_codebuddy.ai/sessionUpdate"):
        return "", ""
    upd = (msg.get("params") or {}).get("update") or {}
    kind = upd.get("sessionUpdate") or upd.get("type") or ""
    content = upd.get("content")
    text = ""
    if isinstance(content, dict):
        text = content.get("text") or ""
        if not text and content.get("type") == "text":
            text = content.get("text") or ""
    elif isinstance(content, str):
        text = content
    elif isinstance(upd.get("text"), str):
        text = upd["text"]
    return text, kind


# ---------------------------------------------------------------- HTTP 服务

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


def sse(obj: dict) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8")


def chunk(model: str, delta: dict, finish: str | None = None, cid: str = "") -> dict:
    return {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": ([{"index": 0, "delta": delta, "finish_reason": finish}]
                    if finish or delta else []),
    }


def open_client(up: "Upstream") -> AcpClient:
    """建立一条已握手、已 initialize 的 ACP 连接；失败时重新发现活 host 再试一次。"""
    last: Exception | None = None
    for attempt in range(2):
        try:
            c = AcpClient(up.base_url, up.password)
            c.connect()
            c.initialize()
            return c
        except Exception as e:
            last = e
            if attempt == 0 and up.reuse:
                log(f"连接上游失败（{e}），重新发现活 host")
                found = discover_live_host()
                if found:
                    up.attached_url, up.password = found
                    continue
            break
    raise last or AcpError("无法连接上游")


class Handler(BaseHTTPRequestHandler):
    server_version = "workbuddy-gateway/0.1"
    protocol_version = "HTTP/1.1"

    # 注入的运行时状态
    upstream: Upstream
    client_factory: Callable[[], AcpClient]
    pool: SessionPool
    cwd: str
    api_key: str | None
    models_cache: list[dict]
    models_lock: threading.Lock
    model_ttl: float
    # 局域网相关
    allowed_networks: list = []
    require_auth: bool = False
    semaphore: threading.Semaphore | None = None
    stats: dict = {}
    stats_lock: threading.Lock | None = None
    lan_mode: bool = False
    queue_timeout: float = 60.0

    # -- 工具

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
        return

    def _send_json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, message: str, etype: str = "invalid_request_error") -> None:
        self._send_json(code, {"error": {"message": message, "type": etype, "code": code}})

    def _client_ip(self) -> str:
        return (self.client_address[0] if self.client_address else "") or ""

    def _ip_allowed(self) -> bool:
        nets = Handler.allowed_networks
        if not nets:
            return True
        if ip_in_networks(self._client_ip(), nets):
            return True
        log(f"拒绝来自 {self._client_ip()} 的访问（不在允许的网段内）")
        self._error(403, f"来源地址 {self._client_ip()} 不在允许的网段内", "permission_error")
        return False

    def _authorized(self) -> bool:
        if not self._ip_allowed():
            return False
        if not Handler.require_auth:
            return True
        expect = Handler.api_key or ""
        got = ""
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            got = auth[7:].strip()
        if not got:
            got = (self.headers.get("x-api-key") or "").strip()
        # 长度不等时也做一次比较，避免用时间差猜长度
        if got and secrets.compare_digest(got, expect):
            return True
        self._error(401, "缺少或错误的 API Key", "authentication_error")
        return False

    def _bump(self, key: str) -> None:
        lock = Handler.stats_lock
        if lock is None:
            return
        with lock:
            Handler.stats[key] = Handler.stats.get(key, 0) + 1

    def _read_json(self) -> dict | None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            self._error(400, "请求体不是合法 JSON")
            return None

    # -- 路由

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._authorized():
            return
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/health"):
            lock = Handler.stats_lock
            stats = dict(Handler.stats) if lock is not None else {}
            self._send_json(200, {
                "status": "ok",
                "mode": "lan" if Handler.lan_mode else "local",
                "upstream": self.upstream.base_url,
                "models": len(self._get_models()),
                "auth_required": bool(Handler.require_auth),
                "lan_only": bool(Handler.allowed_networks),
                "stats": stats,
            })
        elif path == "/stats":
            lock = Handler.stats_lock
            self._send_json(200, {
                "stats": dict(Handler.stats) if lock is not None else {},
                "callers": len(Handler.pool._by_prefix),
            })
        elif path == "/v1/models":
            self._send_json(200, self._models_payload())
        elif path == "/v1/models/raw":
            self._send_json(200, {"models": self._get_models()})
        else:
            self._error(404, f"未知路径 {path}")

    def do_POST(self) -> None:
        if not self._authorized():
            return
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path == "/v1/chat/completions":
            self._chat_completions()
        elif path == "/v1/models/refresh":
            with Handler.models_lock:
                Handler.models_cache = []
            self._send_json(200, self._models_payload())
        else:
            self._error(404, f"未知路径 {path}")

    # -- 模型

    def _get_models(self) -> list[dict]:
        with Handler.models_lock:
            if Handler.models_cache:
                return Handler.models_cache
        models: list[dict] = []
        client = None
        try:
            client = self.client_factory()
            res = client.new_session(self.cwd)
            models = extract_models(res)
        except Exception as e:
            log(f"拉取模型列表失败: {e}")
        finally:
            if client is not None:
                client.disconnect()
        with Handler.models_lock:
            if models:
                Handler.models_cache = models
            return Handler.models_cache

    def _models_payload(self) -> dict:
        data = []
        for m in self._get_models():
            item = {
                "id": m["id"],
                "object": "model",
                "created": int(time.time()),
                "owned_by": "workbuddy",
                "name": m.get("name") or m["id"],
            }
            if m.get("context_window"):
                item["context_window"] = m["context_window"]
            if m.get("max_tokens"):
                item["max_tokens"] = m["max_tokens"]
            data.append(item)
        return {"object": "list", "data": data}

    def _resolve_model(self, requested: str | None) -> str:
        models = self._get_models()
        ids = [m["id"] for m in models]
        if requested:
            if requested in ids:
                return requested
            low = requested.lower()
            for i in ids:
                if i.lower() == low:
                    return i
            log(f"未知模型 {requested}，回落到默认")
        current = None
        for m in models:
            cur = (m.get("raw") or {}).get("currentModelId")
            if cur:
                current = cur
                break
        if current and current in ids:
            return current
        return ids[0] if ids else ""

    # -- 对话

    def _chat_completions(self) -> None:
        """统一入口：先过并发闸门，再真正处理。"""
        sem = Handler.semaphore
        acquired = False
        if sem is not None:
            acquired = sem.acquire(timeout=Handler.queue_timeout)
            if not acquired:
                log(f"并发已满，拒绝来自 {self._client_ip()} 的请求")
                self._bump("rejected_busy")
                return self._error(
                    429,
                    "网关繁忙（并发已满），请稍后重试",
                    "rate_limit_error",
                )
        self._bump("requests")
        try:
            self._chat_completions_body()
        finally:
            if acquired and sem is not None:
                sem.release()

    def _chat_completions_body(self) -> None:
        body = self._read_json()
        if body is None:
            return
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._error(400, "messages 不能为空")

        stream = bool(body.get("stream"))
        model_req = body.get("model")
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        model = self._resolve_model(model_req)
        prefix_key = SessionPool.fingerprint(messages[:-1], model)
        rec = Handler.pool.get(prefix_key)
        continuation = rec is not None and rec.get("model") == model

        if continuation:
            client = rec["client"]
            session_id = rec["session_id"]
        else:
            try:
                client = self.client_factory()
            except Exception as e:
                log(f"连接上游失败: {e}")
                return self._error(502, f"无法连接上游 WorkBuddy: {e}", "upstream_error")
            try:
                res = client.new_session(self.cwd)
            except Exception as e:
                log(f"session/new 失败: {e}")
                client.disconnect()
                return self._error(502, f"上游建会话失败: {e}", "upstream_error")
            session_id = res.get("sessionId")
            if not session_id:
                log(f"session/new 未返回 sessionId: {json.dumps(res, ensure_ascii=False)[:300]}")
                client.disconnect()
                return self._error(502, f"上游未返回 sessionId: {json.dumps(res)[:400]}",
                                   "upstream_error")
            if model:
                client.set_model(session_id, model)
            rec = {"session_id": session_id, "model": model, "client": client,
                   "lock": threading.Lock()}

        try:
            blocks = build_prompt(messages, continuation=continuation)

            snapshot = {"text": ""}
            saw_thought = {"n": 0}
            lock = threading.Lock()

            def on_notification(msg: dict) -> None:
                text, kind = notification_text(msg)
                if not text:
                    return
                if "thought" in kind or "reasoning" in kind:
                    with lock:
                        saw_thought["n"] += 1
                    return
                with lock:
                    snapshot["text"] += text
                if stream:
                    self._write_sse(chunk(model, {"content": text}, cid=cid))

            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.close_connection = True
                self._write_sse(chunk(model, {"role": "assistant", "content": ""}, cid=cid))

            sess_lock = rec.get("lock") or threading.Lock()
            result = None
            last_err: Exception | None = None
            with sess_lock:
                for attempt in range(2):
                    try:
                        result = client.prompt(session_id, blocks, on_notification=on_notification)
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        log(f"prompt 失败（第 {attempt + 1} 次）: {e}")
                        if attempt == 0:
                            # 上游可能已回收这条连接/会话，重建一次再试
                            try:
                                client.disconnect()
                            except Exception:
                                pass
                            try:
                                client = self.client_factory()
                                res2 = client.new_session(self.cwd)
                                session_id = res2["sessionId"]
                                if model:
                                    client.set_model(session_id, model)
                                rec = {"session_id": session_id, "model": model,
                                       "client": client, "lock": sess_lock}
                            except Exception as e2:
                                log(f"重建会话失败: {e2}")
                                break
            if result is None:
                _close_record(rec)
                e = last_err or AcpError("未知错误")
                if stream:
                    self._write_sse(chunk(model, {"content": f"\n[上游错误] {e}"}, cid=cid))
                    self._write_sse(chunk(model, {}, finish="stop", cid=cid))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                return self._error(502, f"上游对话失败: {e}", "upstream_error")

            # 兜底：如果一条增量都没收到，从最终结果里取文本
            if not snapshot["text"]:
                fallback = _final_text(result)
                if fallback:
                    snapshot["text"] = fallback
                    if stream:
                        self._write_sse(chunk(model, {"content": fallback}, cid=cid))

            Handler.pool.put(SessionPool.fingerprint(messages, model), rec)

            usage = _extract_usage(result)
            if stream:
                self._write_sse(chunk(model, {}, finish="stop", cid=cid))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            else:
                self._send_json(200, {
                    "id": cid,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": snapshot["text"]},
                        "finish_reason": "stop",
                    }],
                    "usage": usage,
                })
        except Exception as e:
            traceback.print_exc()
            try:
                if stream:
                    self._write_sse(chunk(model_req or "", {"content": f"\n[网关错误] {e}"}, cid=cid))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                else:
                    self._error(500, f"网关内部错误: {e}", "server_error")
            except Exception:
                pass

    def _write_sse(self, obj: dict) -> None:
        try:
            self.wfile.write(sse(obj))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise


def _final_text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    for k in ("text", "message", "content", "output"):
        v = result.get(k)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            t = notification_text({"method": "session/update",
                                   "params": {"update": {"content": v}}})[0]
            if t:
                return t
    upd = result.get("update")
    if isinstance(upd, dict):
        return notification_text({"method": "session/update", "params": {"update": upd}})[0]
    return ""


def _extract_usage(result: Any) -> dict:
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 5 or not isinstance(node, dict):
            return
        u = node.get("usage")
        if isinstance(u, dict):
            for src, dst in (("input_tokens", "prompt_tokens"),
                             ("prompt_tokens", "prompt_tokens"),
                             ("output_tokens", "completion_tokens"),
                             ("completion_tokens", "completion_tokens")):
                if isinstance(u.get(src), int) and u[src] > 0:
                    usage[dst] = max(usage[dst], u[src])
        for v in node.values():
            if isinstance(v, dict):
                walk(v, depth + 1)

    walk(result if isinstance(result, dict) else {})
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    return usage


# ---------------------------------------------------------------- 自检


def selftest(args: argparse.Namespace) -> int:
    password = args.upstream_password or secrets.token_urlsafe(24)
    token, src = discover_token(args.auth_token)
    if token:
        log(f"登录态来源: {src}")
    up = Upstream(
        electron=args.electron, cli=args.cli, port=args.upstream_port,
        password=password, config_dir=args.config_dir, cwd=args.workdir,
        reuse=not args.no_reuse, auth_token=token,
    )
    try:
        up.ensure()
        client = open_client(up)
        log(f"已连接到 {up.base_url}")

        res = client.new_session(args.workdir)
        log(f"sessionId = {res.get('sessionId')}")
        models = extract_models(res)
        log(f"发现 {len(models)} 个模型:")
        for m in models:
            ctx = m.get("context_window") or "-"
            log(f"  - {m['id']:<28} {m.get('name') or '':<28} ctx={ctx}")

        if not models:
            log("!! 没解析到模型，session/new 原始返回:")
            log(json.dumps(res, ensure_ascii=False)[:3000])

        target = args.selftest_model or (models[0]["id"] if models else None)
        if target:
            client.set_model(res["sessionId"], target)
        log(f"用模型 {target} 测一次对话 ...")

        got: list[str] = []

        def on_notif(msg: dict) -> None:
            t, k = notification_text(msg)
            if t and "thought" not in k:
                got.append(t)
                sys.stdout.write(t)
                sys.stdout.flush()

        r = client.prompt(res["sessionId"], [{"type": "text", "text": "用一句话回答：1+1 等于几？"}],
                          on_notification=on_notif)
        print()
        log(f"回答长度={len(''.join(got))} 字符，result={json.dumps(r, ensure_ascii=False)[:300]}")
        if not got:
            log("!! 没有收到任何增量文本，说明通知格式和预期不同")
            return 2
        log("自检通过 ✅")
        return 0
    except Exception as e:
        log(f"自检失败: {e}")
        if up.owns_proc:
            log("上游最后输出:\n" + up.tail(30))
        traceback.print_exc()
        return 1
    finally:
        up.stop()


# ---------------------------------------------------------------- main


class GatewayServer(ThreadingHTTPServer):
    """客户端正常断开时不要打印堆栈（keep-alive 收尾会触发 ConnectionReset）。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # ---- 读取持久化配置（让 `python workbuddy_gateway.py` 直接就是想要的效果）----
    cfg, cfg_path = load_gateway_config(os.getcwd())

    def cget(key: str, fallback: Any = None) -> Any:
        v = cfg.get(key)
        return fallback if v is None else v

    p = argparse.ArgumentParser(
        description="本地 WorkBuddy 模型的 OpenAI 兼容网关",
        epilog="提示：加 --host 0.0.0.0 --save-config 可把局域网设置保存下来，"
               "之后直接 python workbuddy_gateway.py 即可。",
    )
    p.add_argument("--host", default=cget("host", GATEWAY_HOST),
                   help="监听地址：127.0.0.1 仅本机；0.0.0.0 或本机内网 IP 供局域网访问")
    p.add_argument("--port", type=int, default=cget("port", DEFAULT_GATEWAY_PORT),
                   help="网关端口")
    p.add_argument("--upstream-port", type=int,
                   default=cget("upstream_port", DEFAULT_UPSTREAM_PORT),
                   help="codebuddy --serve 的端口")
    p.add_argument("--upstream-password", default=os.environ.get("WB_UPSTREAM_PASSWORD"),
                   help="上游密码；不给就随机生成")
    p.add_argument("--electron", default=DEFAULT_ELECTRON)
    p.add_argument("--cli", default=DEFAULT_CLI)
    p.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR)
    p.add_argument("--workdir", default=os.getcwd(), help="会话 cwd")
    p.add_argument("--api-key", default=os.environ.get("WB_GATEWAY_KEY"),
                   help="调用方需要携带的 API Key；局域网模式不指定会自动生成一个")
    p.add_argument("--lan-cidr", default=cget("lan_cidr", os.environ.get("WB_LAN_CIDR")),
                   help="允许访问的网段，逗号分隔；默认仅私有网段。传 'any' 放开")
    p.add_argument("--max-concurrency", type=int,
                   default=cget("max_concurrency",
                                int(os.environ.get("WB_MAX_CONCURRENCY") or 4)),
                   help="同时处理的最大请求数（默认 4），超出排队")
    p.add_argument("--queue-timeout", type=float,
                   default=cget("queue_timeout",
                                float(os.environ.get("WB_QUEUE_TIMEOUT") or 60)),
                   help="排队等待上限秒数，超时返回 429")
    p.add_argument("--no-auth", action="store_true",
                   help="局域网模式下也强制不校验 Key（危险，仅供隔离网络）")
    p.add_argument("--save-config", action="store_true",
                   help="把本次参数写入 .workbuddy-gateway/gateway.json，下次直接启动即生效")
    p.add_argument("--auth-token", default=None,
                   help="WorkBuddy accessToken；不给就自动从桌面端登录态文件读取")
    p.add_argument("--no-reuse", action="store_true", help="不复用已存在的上游，强制自己拉一个")
    p.add_argument("--selftest", action="store_true", help="只验证上游链路")
    p.add_argument("--selftest-model", default=None)
    args = p.parse_args(argv)

    if cfg_path:
        log(f"已读取配置: {cfg_path}")

    if args.save_config:
        payload = {
            "host": args.host,
            "port": args.port,
            "upstream_port": args.upstream_port,
            "lan_cidr": args.lan_cidr,
            "max_concurrency": args.max_concurrency,
            "queue_timeout": args.queue_timeout,
        }
        target = cfg_path or default_config_path(os.getcwd())
        if save_gateway_config(target, payload):
            log(f"配置已保存: {target}")
            if not is_loopback_host(args.host):
                log("以后直接运行 python workbuddy_gateway.py 就是局域网模式")
        else:
            log("配置保存失败，本次仍按命令行参数运行")

    if args.selftest:
        return selftest(args)

    password = args.upstream_password or secrets.token_urlsafe(24)
    token, src = discover_token(args.auth_token)
    if token:
        log(f"登录态来源: {src}")
    up = Upstream(
        electron=args.electron, cli=args.cli, port=args.upstream_port,
        password=password, config_dir=args.config_dir, cwd=args.workdir,
        reuse=not args.no_reuse, auth_token=token,
    )
    try:
        up.ensure()
    except Exception as e:
        log(f"上游启动失败: {e}")
        return 1

    # ---- 局域网 / 鉴权配置 ----
    lan_mode = not is_loopback_host(args.host)
    api_key = (args.api_key or "").strip()

    if lan_mode and not api_key and not args.no_auth:
        key_path = os.path.join(args.workdir, ".workbuddy-gateway", "gateway.key")
        api_key = load_or_create_key(key_path)
        log(f"局域网模式未指定 --api-key，使用持久化 Key（{key_path}）")

    require_auth = bool(api_key) and not args.no_auth

    if args.lan_cidr and args.lan_cidr.strip().lower() == "any":
        allowed: list = []
        log("!! 已放开来源限制（--lan-cidr any），任何能连上的人都可以调用")
    elif lan_mode:
        allowed = parse_networks(args.lan_cidr)
    else:
        # 本机模式不必做来源过滤
        allowed = []

    Handler.upstream = up
    Handler.client_factory = staticmethod(lambda: open_client(up))
    Handler.pool = SessionPool()
    Handler.cwd = args.workdir
    Handler.api_key = api_key or None
    Handler.require_auth = require_auth
    Handler.allowed_networks = allowed
    Handler.lan_mode = lan_mode
    Handler.queue_timeout = max(1.0, args.queue_timeout)
    Handler.semaphore = threading.Semaphore(max(1, args.max_concurrency))
    Handler.stats = {}
    Handler.stats_lock = threading.Lock()
    Handler.models_cache = []
    Handler.models_lock = threading.Lock()

    # 预热模型列表
    def warm() -> None:
        c = None
        try:
            c = open_client(up)
            res = c.new_session(args.workdir)
            ms = extract_models(res)
            with Handler.models_lock:
                Handler.models_cache = ms
            log(f"已加载 {len(ms)} 个模型: {', '.join(m['id'] for m in ms[:8])}")
        except Exception as e:
            log(f"预热模型列表失败（不影响启动）: {e}")
        finally:
            if c is not None:
                c.disconnect()

    threading.Thread(target=warm, name="warmup", daemon=True).start()

    srv = GatewayServer((args.host, args.port), Handler)
    srv.daemon_threads = True

    # ---- 启动横幅 ----
    if lan_mode:
        log("=" * 64)
        log(f"局域网模式：监听 {args.host}:{args.port}")
        for ip in local_ipv4_addresses():
            log(f"  邻居可访问 -> http://{ip}:{args.port}/v1")
        log(f"  Base URL   http://<本机内网IP>:{args.port}/v1")
        log(f"  API Key    {api_key if require_auth else '(未启用，不校验)'}")
        if allowed:
            log(f"  允许网段   {', '.join(str(n) for n in allowed)}")
        else:
            log("  允许网段   不限制")
        log(f"  最大并发   {args.max_concurrency}（排队上限 {args.queue_timeout:.0f}s）")
        log("=" * 64)
        log("提示：Windows 防火墙默认会拦入站连接，首次可能需要在弹窗里勾选“专用网络”，")
        log("      或管理员执行：New-NetFirewallRule -DisplayName \"WorkBuddy Gateway\"")
        log("      -Direction Inbound -Action Allow -Protocol TCP -LocalPort " + str(args.port))
    else:
        log(f"网关就绪 -> http://{args.host}:{args.port}")

    log(f"  GET  /v1/models")
    log(f"  POST /v1/chat/completions  (stream=true 支持)")
    log(f"  GET  /health   /stats")
    if require_auth:
        log("  已启用 Bearer 校验")
    log("Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("收到中断，正在退出")
    finally:
        srv.shutdown()
        up.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
