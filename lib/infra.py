"""Infrastructure manager for Shopify automation.

Manages AdsPower browser profiles, SSH tunnels to CDP ports,
Cloudflare tunnel detection, and cleanup operations.

变更要点:
- P1-6: detect_cloudflare 检测到新 URL 时落盘到 config.yaml
- P1-7: cloudflared URL 严格校验为 *.trycloudflare.com 域名
- P1-8: SSH 隧道使用 sshpass + ConnectTimeout=10
- P1-11: find_free_port 改为 OS 分配（O(1)）
"""
import json
import os
import re
import shutil
import socket
import subprocess
import time
import requests
from pathlib import Path
from urllib.parse import urlparse

from lib import config

# ── Constants ──────────────────────────────────────────────────────────────
ADSPOWER_BASE = "http://127.0.0.1:15000"
SSH_REMOTE_USER = "Administrator"
SSH_REMOTE_PORT = 22
TUNNEL_PID_DIR = Path("/tmp")
MAX_TUNNEL_RETRIES = 3
TUNNEL_VERIFY_TIMEOUT = 15  # seconds to wait for tunnel to be live
ADSPOWER_GROUP_ID = "9562622"  # Shopify-Reg 组


# ── Helpers ────────────────────────────────────────────────────────────────

def _api_headers() -> dict:
    """Build standard AdsPower API headers with auth."""
    cfg = config.load()
    api_key = cfg["adspower"]["api_key"]
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def _api_url() -> str:
    """Return the AdsPower API base URL."""
    cfg = config.load()
    return cfg.get("adspower", {}).get("base_url", ADSPOWER_BASE)


def _ssh_host() -> str:
    """Return the remote SSH host for tunnels."""
    cfg = config.load()
    return cfg["adspower"]["server_host"]


def _tunnel_pid_path(profile_id: str) -> Path:
    """Return path to tunnel PID file for a given profile."""
    return TUNNEL_PID_DIR / f"tunnel_{profile_id}.pid"


def _is_port_open(host: str, port: int, timeout: float = 3.0) -> bool:
    """Check if a TCP port is open and accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.error):
        return False


def _kill_pid_file(pid_path: Path) -> bool:
    """Read PID from file, kill process if alive, remove file. Returns True if killed."""
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
        try:
            os.kill(pid, 0)  # check if alive
        except OSError:
            # Process already dead, clean up file
            pid_path.unlink(missing_ok=True)
            return False
        # Kill the process and any child processes
        subprocess.run(
            ["kill", "-TERM", str(pid)],
            capture_output=True, timeout=5
        )
        # Give it a moment then force kill if still alive
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
            subprocess.run(["kill", "-KILL", str(pid)], capture_output=True, timeout=5)
        except OSError:
            pass
        pid_path.unlink(missing_ok=True)
        return True
    except (ValueError, OSError, subprocess.TimeoutExpired):
        pid_path.unlink(missing_ok=True)
        return False


# ── Profile Management ─────────────────────────────────────────────────────

def ensure_profile(profile_id: str) -> dict:
    """Start an AdsPower browser profile with proper window size.

    1. Calls POST /api/v2/browser-profile/start with launch_args for 1280x1080.
    OS / browser kernel 已在 create_profile 的 fingerprint_config 中设置。

    Args:
        profile_id: AdsPower profile ID (e.g., 'k1clr6ji').

    Returns:
        Dict with 'cdp_port' and other browser info from the start API.

    Raises:
        RuntimeError: If either API call fails.
    """
    base = _api_url()
    headers = _api_headers()

    # Step 1: Start browser via v2 POST API with launch_args for window size
    start_payload = {
        "profile_id": profile_id,
        "open_tabs": 1,
        "launch_args": ["--window-size=1280,1080", "--window-position=0,0"],
        "cdp_mask": 1,
    }
    resp = requests.post(
        f"{base}/api/v2/browser-profile/start",
        json=start_payload,
        headers=headers,
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"AdsPower /api/v2/browser-profile/start failed (HTTP {resp.status_code}): {resp.text}"
        )
    start_data = resp.json()
    if start_data.get("code") != 0:
        raise RuntimeError(
            f"AdsPower /api/v2/browser-profile/start returned error: {start_data}"
        )
    # Extract CDP info from response (v2 uses ws/data/webdriver in data field)
    data = start_data.get("data", {})
    if not data:
        raise RuntimeError(f"AdsPower /api/v2/browser-profile/start returned no data: {start_data}")

    cdp_port = data.get("cdp_port") or data.get("debug_port") or data.get("port")
    print(f"   ✅  Profile {profile_id} started (cdp_port={cdp_port})", flush=True)
    return data


def create_profile(name: str, proxy_user: str = "", proxy_pass: str = "",
                   proxy_host: str = "gate.rola.vip", proxy_port: str = "2000",
                   group_id: str = ADSPOWER_GROUP_ID) -> dict | None:
    """通过 AdsPower API 创建新的浏览器配置文件。

    Args:
        name: 配置文件显示名称。
        proxy_user: 代理用户名（不含前缀，函数自动拼 gyd602_）。
        proxy_pass: 代理密码。
        proxy_host: 代理服务器地址，默认 gate.rola.vip。
        proxy_port: 代理端口，默认 2000。
        group_id: 分组 ID，默认 Shopify-Reg (9562622)。

    Returns:
        {"user_id": str, "name": str} 若成功，None 若失败。
    """
    base = _api_url()
    headers = _api_headers()

    # 构建代理配置
    proxy_config = {"proxy_soft": "no_proxy"}
    if proxy_user and proxy_pass:
        proxy_config = {
            "proxy_soft": "other",
            "proxy_type": "socks5",
            "proxy_host": proxy_host,
            "proxy_port": proxy_port,
            "proxy_user": f"gyd602_{proxy_user}-country-us-state-ca",
            "proxy_password": proxy_pass,
        }

    payload = {
        "name": name,
        "group_id": group_id,
        "user_proxy_config": proxy_config,
        "fingerprint_config": {
            "automatic_timezone": 1,
            "language": ["en-US"],
            "location": "allow",
            "webrtc": "disabled",
            "browser_kernel_config": {
                "version": "latest",
                "type": "chrome",
            },
            "random_ua": {
                "ua_system_version": ["Windows"],
            },
        },
    }

    try:
        resp = requests.post(
            f"{base}/api/v1/user/create",
            json=payload,
            headers=headers,
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"   ❌ create_profile HTTP {resp.status_code}", flush=True)
            return None
        data = resp.json()
        if data.get("code") != 0:
            print(f"   ❌ create_profile error: {data}", flush=True)
            return None
        user_id = data.get("data", {}).get("id", "")
        if not user_id:
            print(f"   ❌ create_profile: no id in response", flush=True)
            return None
        print(f"   ✅ Profile '{name}' created (user_id={user_id})", flush=True)
        return {"user_id": user_id, "name": name}
    except Exception as e:
        print(f"   ❌ create_profile exception: {e}", flush=True)
        return None


def stop_profile(profile_id: str) -> bool:
    """Stop an AdsPower browser profile and kill its SSH tunnel.

    1. Calls POST /api/v1/browser/stop.
    2. Kills SSH tunnel associated with this profile (by PID file).

    Args:
        profile_id: AdsPower profile ID.

    Returns:
        True if browser was stopped (or no start data), False on API error.
    """
    base = _api_url()
    headers = _api_headers()

    # Stop browser (AdsPower Local API uses GET with query params)
    stopped = False
    try:
        resp = requests.get(
            f"{base}/api/v1/browser/stop",
            params={"user_id": profile_id},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 0:
                print(f"   ✅  Profile {profile_id} stopped", flush=True)
                stopped = True
            else:
                print(f"   ⚠️  Profile {profile_id} stop returned: {data}", flush=True)
        else:
            print(f"   ⚠️  Profile {profile_id} stop HTTP {resp.status_code}", flush=True)
    except requests.RequestException as e:
        print(f"   ⚠️  Profile {profile_id} stop request failed: {e}", flush=True)

    # Kill SSH tunnel
    pid_path = _tunnel_pid_path(profile_id)
    killed = _kill_pid_file(pid_path)
    if killed:
        print(f"   ✅  Tunnel for {profile_id} killed", flush=True)
    else:
        print(f"   ℹ️   No active tunnel for {profile_id}", flush=True)

    return stopped


# ── SSH Tunnel Management ─────────────────────────────────────────────────

def ensure_tunnel(profile_id: str, cdp_port: int, local_port: int) -> bool:
    """Create an SSH tunnel from Linux to AdsPower server via CDP.

    The tunnel forwards ``local_port`` on localhost to ``127.0.0.1:cdp_port``
    on the remote server. A PID file is written to
    ``/tmp/tunnel_{profile_id}.pid``.

    Retries up to MAX_TUNNEL_RETRIES times if the tunnel doesn't come
    online within TUNNEL_VERIFY_TIMEOUT seconds.

    Args:
        profile_id: AdsPower profile ID (for PID file tracking).
        cdp_port: CDP port on the remote AdsPower server.
        local_port: Local port to forward.

    Returns:
        True if tunnel was established and verified.

    Raises:
        RuntimeError: If tunnel cannot be established after all retries.
    """
    host = _ssh_host()
    pid_path = _tunnel_pid_path(profile_id)

    # If a tunnel already exists and is alive, reuse it
    if pid_path.exists():
        try:
            old_pid = int(pid_path.read_text().strip())
            os.kill(old_pid, 0)  # check alive
            if _is_port_open("127.0.0.1", local_port, timeout=1):
                print(f"   ✅  Tunnel for {profile_id} already active (PID {old_pid}, port {local_port})", flush=True)
                return True
            else:
                # Tunnel process exists but port is closed, kill and recreate
                print(f"   ℹ️   Tunnel PID {old_pid} exists but port {local_port} is closed, recreating...", flush=True)
                _kill_pid_file(pid_path)
        except (OSError, ValueError):
            # PID file stale or process dead
            pid_path.unlink(missing_ok=True)

    # P1-8: 使用 sshpass 自动注入密码；如未安装则退回 key-based 模式
    # 并打印警告。生产环境可改为 known_hosts + key auth。
    cfg = config.load()
    server_pass = cfg.get("adspower", {}).get("server_pass", "")
    sshpass_bin = shutil.which("sshpass")

    base_ssh_args = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ServerAliveInterval=30",
        "-o", "ConnectTimeout=10",
        "-L", f"{local_port}:127.0.0.1:{cdp_port}",
        f"{SSH_REMOTE_USER}@{host}",
        "-p", str(SSH_REMOTE_PORT),
        "-N",
    ]

    if sshpass_bin and server_pass:
        cmd = [sshpass_bin, "-p", server_pass, "ssh"] + base_ssh_args
    else:
        if not sshpass_bin:
            print("   ⚠️  sshpass 未安装；将依赖 SSH key 认证（apt-get install sshpass）",
                  flush=True)
        if not server_pass:
            print("   ⚠️  adspower.server_pass 未配置；将依赖 SSH key 认证",
                  flush=True)
        cmd = ["ssh"] + base_ssh_args

    for attempt in range(1, MAX_TUNNEL_RETRIES + 1):
        print(f"   🔄  Tunnel attempt {attempt}/{MAX_TUNNEL_RETRIES}: {local_port} → {host}:{cdp_port}", flush=True)

        # Ensure old state is cleaned before each attempt
        _kill_pid_file(pid_path)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        pid = proc.pid
        pid_path.write_text(str(pid))

        # Wait briefly for the tunnel to establish
        deadline = time.monotonic() + TUNNEL_VERIFY_TIMEOUT
        tunnel_ok = False
        while time.monotonic() < deadline:
            # Check process is still alive
            ret = proc.poll()
            if ret is not None:
                print(f"   ⚠️  SSH process exited prematurely (exit code {ret})", flush=True)
                break
            # Check port is open
            if _is_port_open("127.0.0.1", local_port, timeout=1):
                tunnel_ok = True
                break
            time.sleep(0.5)

        if tunnel_ok:
            # Verify by attempting to connect to the CDP WebSocket
            try:
                ws_url = f"http://127.0.0.1:{local_port}/json/version"
                r = requests.get(ws_url, timeout=5)
                if r.status_code == 200:
                    print(f"   ✅  Tunnel for {profile_id} ready (port {local_port}, PID {pid})", flush=True)
                    return True
                else:
                    print(f"   ⚠️  CDP endpoint responded HTTP {r.status_code}, retrying...", flush=True)
            except requests.RequestException as e:
                print(f"   ⚠️  CDP {ws_url} unreachable: {e}", flush=True)

            # Port is open but CDP isn't responding — might be slow to boot
            # Give it a bit more time
            time.sleep(3)
            try:
                r = requests.get(f"http://127.0.0.1:{local_port}/json/version", timeout=5)
                if r.status_code == 200:
                    print(f"   ✅  Tunnel for {profile_id} ready (port {local_port}, PID {pid})", flush=True)
                    return True
            except requests.RequestException:
                pass

        # Retry: kill the failed tunnel process
        _kill_pid_file(pid_path)
        print(f"   ⚠️  Tunnel attempt {attempt} failed, {'retrying...' if attempt < MAX_TUNNEL_RETRIES else 'giving up'}", flush=True)

    raise RuntimeError(
        f"Failed to establish SSH tunnel for {profile_id} "
        f"(local={local_port}, remote={host}:{cdp_port}) after {MAX_TUNNEL_RETRIES} attempts"
    )


# ── Pre-flight 可达性检查 (前馈控制) ───────────────────────────────────
#
# 钱学森工程控制论原则：在干扰影响系统前提前测量并施加补偿。
# SSH 不可达就立刻报错而不是等隧道建立失败 ~30s 后才知道。
#

def check_ssh_reachable(host: str, port: int = 22,
                        timeout: float = 5.0) -> tuple[bool, str]:
    """检查 SSH 目标是否可达（前馈控制：提前测量干扰）。

    控制论原理：在建立 SSH 隧道前先用 TCP socket 做一次"低成本探针"，
    若不通则立即返回，省去后续 ~30s 的隧道重试。优先使用系统 ``nc``
    （netcat）做端口探测，不可用时降级到 Python socket。

    Args:
        host: 远端主机（IP 或域名）。
        port: SSH 端口（默认 22）。
        timeout: 探测超时秒数（默认 5）。

    Returns:
        ``(True, "ok")``  —— 端口可连接。
        ``(False, 错误描述)`` —— 不可达、超时、解析失败等。
    """
    if not host:
        return (False, "host is empty")

    # 优先用 nc -zw{timeout}（如果系统提供）
    nc_bin = shutil.which("nc")
    if nc_bin:
        try:
            result = subprocess.run(
                [nc_bin, "-zw", str(int(timeout)), host, str(port)],
                capture_output=True, text=True,
                timeout=timeout + 2,
            )
            if result.returncode == 0:
                return (True, "ok")
            stderr = (result.stderr or "").strip() or "nc returned non-zero"
            return (False, f"nc -z {host}:{port} failed: {stderr}")
        except subprocess.TimeoutExpired:
            return (False, f"nc -z {host}:{port} timed out after {timeout}s")
        except Exception as e:
            # nc 出问题就降级到 socket
            print(f"   ⚠️  nc 探测异常，回退 socket: {e}", flush=True)

    # 降级：Python socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (True, "ok")
    except socket.timeout:
        return (False, f"socket connect {host}:{port} timed out after {timeout}s")
    except socket.gaierror as e:
        return (False, f"DNS/host resolve failed for {host}: {e}")
    except OSError as e:
        return (False, f"socket connect {host}:{port} failed: {e}")


# ── Port Utilities ─────────────────────────────────────────────────────────

def find_free_port(start: int = 10000, end: int = 65535) -> int:
    """Return an available local TCP port.

    P1-11: 改为让 OS 分配空闲端口（O(1)），不再 O(N) 扫描。
    start/end 参数保留向后兼容但被忽略。
    """
    del start, end  # 显式标记为忽略
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


# ── Cloudflare Tunnel Detection ────────────────────────────────────────────

# P1-7: 严格校验 cloudflared URL 域名（防 SSRF 注入）
# 仅接受 https://<sub>.trycloudflare.com，其中 sub 只能是小写字母/数字/连字符
_TRYCLOUDFLARE_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _is_valid_trycloudflare_url(url: str) -> bool:
    """校验 URL 是否合法的 trycloudflare.com 子域。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if not host.endswith(".trycloudflare.com"):
        return False
    sub = host[: -len(".trycloudflare.com")]
    # 不允许嵌套多级 subdomain（"foo.bar.trycloudflare.com" 含 dot）
    if "." in sub:
        return False
    return bool(_TRYCLOUDFLARE_HOST_RE.match(sub))


def _persist_tunnel_url(new_url: str) -> bool:
    """P1-6: 把新的 base_url 写回 config.yaml，让下次进程启动也能用。

    用正则替换原文件中 base_url 行，保持其它格式 / 注释 / 占位符不变。
    """
    cfg_path = Path(config._CONFIG_PATH)  # type: ignore[attr-defined]
    try:
        original = cfg_path.read_text()
        # 匹配 llm: 段中的 base_url: "..." 行（保守做法：替换第一处出现）
        pattern = re.compile(
            r'(^\s*base_url:\s*)"[^"\n]*"',
            re.MULTILINE,
        )
        if not pattern.search(original):
            print("   ⚠️  config.yaml 中未找到 base_url 行，跳过写回", flush=True)
            return False
        updated = pattern.sub(lambda m: f'{m.group(1)}"{new_url}"', original,
                              count=1)
        cfg_path.write_text(updated)
        # 让后续 config.load() 拿到新值
        config.reload()
        print(f"   💾  config.yaml 已更新 base_url={new_url}", flush=True)
        return True
    except Exception as e:
        print(f"   ⚠️  写回 config.yaml 失败: {e}", flush=True)
        return False


def detect_cloudflare() -> tuple:
    """Detect the current Cloudflare tunnel URL.

    Strategy:
    1. Try the existing ``llm.base_url`` from config.
    2. If unreachable, scan ``cloudflared`` process args / known log files
       for a new tunnel URL. P1-7: 仅接受 *.trycloudflare.com 域名。
    3. P1-6: 找到新 URL 后写回 config.yaml（落盘），让进程重启也能复用。

    Returns:
        Tuple of ``(base_url, success)``.
    """
    cfg = config.load()
    current_url = cfg.get("llm", {}).get("base_url", "")

    # Step 1: Try the current URL
    if current_url:
        test_url = current_url.rstrip("/") + "/models"
        try:
            r = requests.get(test_url, timeout=10)
            if r.status_code == 200:
                print(f"   ✅  Current tunnel URL works: {current_url}", flush=True)
                return (current_url, True)
            elif r.status_code == 502:
                print(f"   ⚠️  Current URL returned 502, tunnel may be dead", flush=True)
            else:
                print(f"   ⚠️  Current URL returned HTTP {r.status_code}", flush=True)
        except requests.RequestException as e:
            print(f"   ⚠️  Current URL unreachable: {e}", flush=True)

    def _try_candidate(raw_url: str) -> tuple | None:
        """尝试候选 URL：先校验域名，再 HTTP 验活，最后写回 config。"""
        if not _is_valid_trycloudflare_url(raw_url):
            print(f"   🚫  拒绝非法 URL（非 trycloudflare.com）：{raw_url}",
                  flush=True)
            return None
        new_url = raw_url.rstrip("/") + "/v1"
        # Hot-patch the cache
        if "llm" not in cfg:
            cfg["llm"] = {}
        cfg["llm"]["base_url"] = new_url
        try:
            r = requests.get(new_url.rstrip("/") + "/models", timeout=10)
            if r.status_code == 200:
                print(f"   ✅  New tunnel URL verified: {new_url}", flush=True)
                _persist_tunnel_url(new_url)  # P1-6 写回
                return (new_url, True)
            print(f"   ⚠️  New URL returned HTTP {r.status_code}: {new_url}",
                  flush=True)
        except requests.RequestException as e:
            print(f"   ⚠️  New URL unreachable: {e}", flush=True)
        return (new_url, False)

    # Step 2: Find cloudflared process and extract new URL
    print("   🔍  Scanning for cloudflared process...", flush=True)
    try:
        result = subprocess.run(
            ["pgrep", "-af", "cloudflared"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                # 只接受 https://<host>.trycloudflare.com，无路径/查询
                for raw in re.findall(
                    r"https://[A-Za-z0-9.-]+\.trycloudflare\.com",
                    line,
                ):
                    res = _try_candidate(raw)
                    if res is not None:
                        return res
        else:
            print("   ⚠️  No cloudflared process found", flush=True)

        # Try log files
        print("   ℹ️   No URL in process args, trying log extraction...",
              flush=True)
        log_paths = [
            Path.home() / ".cloudflared" / "cloudflared.log",
            Path("/var/log/cloudflared.log"),
            Path("/tmp/cloudflared.log"),
        ]
        for log_path in log_paths:
            if log_path.exists():
                log_content = log_path.read_text(errors="replace")
                for raw in re.findall(
                    r"https://[A-Za-z0-9.-]+\.trycloudflare\.com",
                    log_content,
                ):
                    res = _try_candidate(raw)
                    if res is not None:
                        return res

    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"   ⚠️  Error scanning processes: {e}", flush=True)

    print("   ❌  Could not detect a working Cloudflare tunnel URL", flush=True)
    return (current_url, False)


# ── Cleanup ────────────────────────────────────────────────────────────────

def cleanup_all() -> None:
    """Stop all known profiles and kill all SSH tunnels.

    Iterates through all profiles defined in config, stops each browser,
    and cleans up any orphaned tunnel PID files in /tmp.
    """
    print(f"\n🧹  Running full infrastructure cleanup...", flush=True)
    cfg = config.load()
    profiles = cfg.get("profiles", {})
    profile_ids = list(profiles.keys())

    if not profile_ids:
        print(f"   ℹ️   No profiles defined in config", flush=True)

    for pid in profile_ids:
        try:
            stop_profile(pid)
        except Exception as e:
            print(f"   ⚠️  Error stopping profile {pid}: {e}", flush=True)

    # Kill any orphaned tunnel processes (PIDs in /tmp/tunnel_*.pid)
    print(f"   🔍  Cleaning orphaned tunnels...", flush=True)
    for pid_file in TUNNEL_PID_DIR.glob("tunnel_*.pid"):
        profile_id_from_file = pid_file.stem.replace("tunnel_", "")
        if profile_id_from_file not in profile_ids:
            killed = _kill_pid_file(pid_file)
            if killed:
                print(f"   ✅  Orphaned tunnel {pid_file.name} cleaned", flush=True)

    print(f"   ✅  Cleanup complete", flush=True)
