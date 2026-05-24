#!/usr/bin/env python3.12
"""
AdsPower + SSH 隧道管理 — v2 稳定版
====================================
使用 autossh 做自动重连，不再丢失 SSH 隧道。

用法:
  python3 tunnel_mgr.py start     # 启动所有隧道
  python3 tunnel_mgr.py stop      # 停止所有隧道
  python3 tunnel_mgr.py status    # 状态检查
  python3 tunnel_mgr.py cdp PORT  # 为指定 CDP 端口建隧道
"""
import argparse, os, signal, socket, subprocess, sys, time

WIN_IP = "43.155.1.195"
WIN_USER = "administrator"
WIN_PASS = "ZHOUjiahao1!"
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
PID_FILE = os.path.join(WORK_DIR, ".tunnel_pids")

SSH_BASE = [
    "sshpass", "-p", WIN_PASS,
    "autossh",
    "-M", "0",        # disables monitoring port, uses TCP keepalive instead
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ConnectTimeout=30",
    "-o", "ServerAliveInterval=3",
    "-o", "ServerAliveCountMax=3",
    "-o", "TCPKeepAlive=yes",
    "-o", "ExitOnForwardFailure=yes",
]

PIDS = {}  # port -> pid


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check_port(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        result = sock.connect_ex(("127.0.0.1", port))
        return result == 0
    finally:
        sock.close()


def start_api_tunnel():
    """永久 API 隧道 (port 15000 → 50325)"""
    port = 15000
    log(f"🚇 Starting API tunnel: {port}→50325...")
    
    # Kill existing
    if check_port(port):
        log(f"   Port {port} already in use, checking...")
    
    cmd = SSH_BASE + [
        "-L", f"{port}:127.0.0.1:50325",
        "-N", "-f", f"{WIN_USER}@{WIN_IP}",
    ]
    full_cmd = cmd  # SSH_BASE already includes sshpass
    
    proc = subprocess.run(full_cmd, capture_output=True, text=True, timeout=20)
    time.sleep(2)
    
    if check_port(port):
        log(f"   ✅ API tunnel on :{port}")
        # Get the PID
        r = subprocess.run(["pgrep", "-f", f"autossh.*{port}:127.0.0.1:50325"], 
                          capture_output=True, text=True, timeout=5)
        if r.stdout.strip():
            PIDS[port] = int(r.stdout.strip().split("\n")[0])
        return True
    else:
        log(f"   ❌ API tunnel failed: {proc.stderr[:200]}")
        return False


def start_cdp_tunnel(remote_port, local_port=None):
    """为指定 CDP 端口建隧道"""
    if local_port is None:
        local_port = remote_port
    
    # Check if tunnel already exists
    if check_port(local_port):
        log(f"   ✅ CDP tunnel already up: :{local_port} ({remote_port})")
        return True
    
    log(f"🚇 Creating CDP tunnel: :{local_port}→...:{remote_port}")
    
    # Kill stale process on this port
    subprocess.run(["fuser", "-k", f"{local_port}/tcp"], capture_output=True, timeout=5)
    time.sleep(0.5)

    cmd = SSH_BASE + [
        "-L", f"{local_port}:127.0.0.1:{remote_port}",
        "-N", "-f", f"{WIN_USER}@{WIN_IP}",
    ]
    full_cmd = cmd  # SSH_BASE already includes sshpass

    proc = subprocess.run(full_cmd, capture_output=True, text=True, timeout=20)
    time.sleep(2)
    
    if check_port(local_port):
        log(f"   ✅ CDP tunnel :{local_port} → ...:{remote_port}")
        r = subprocess.run(["pgrep", "-f", f"autossh.*{local_port}:127.0.0.1:{remote_port}"],
                          capture_output=True, text=True, timeout=5)
        if r.stdout.strip():
            PIDS[local_port] = int(r.stdout.strip().split("\n")[0])
        return True
    else:
        log(f"   ❌ CDP tunnel failed: {proc.stderr[:200]}")
        return False


def stop_all():
    """停止所有 autossh 隧道"""
    log("🛑 Stopping all tunnels...")
    r = subprocess.run(["pgrep", "-a", "autossh"], capture_output=True, text=True, timeout=5)
    lines = [l for l in r.stdout.strip().split("\n") if l and "adspower" not in l.lower() and "tunnel_mgr" not in l.lower()]
    
    # Kill CDP tunnels only (skip API tunnel on :15000)
    for line in lines:
        if "-L" in line and "15000:127.0.0.1:50325" not in line:
            pid = line.split()[0]
            log(f"   Killing autossh PID {pid}: {line[:100]}")
            try:
                os.kill(int(pid), signal.SIGTERM)
            except:
                pass
    
    # Also kill the ssh child processes (skip API tunnel)
    r2 = subprocess.run(["pgrep", "-f", "ssh.*administrator@43.155.1.195"],
                       capture_output=True, text=True, timeout=5)
    for line in r2.stdout.strip().split("\n"):
        if line.strip() and "15000:127.0.0.1:50325" not in line:
            try:
                os.kill(int(line.strip()), signal.SIGTERM)
            except:
                pass
    
    time.sleep(1)
    log("   ✅ All tunnels stopped")


def status():
    """检查所有隧道状态"""
    log("📊 Tunnel Status:")
    
    # API tunnel
    api_ok = check_port(15000)
    log(f"   {'✅' if api_ok else '❌'} API :15000 → AdsPower API")
    
    if api_ok:
        r = subprocess.run(["curl", "-s", "http://127.0.0.1:15000/api/v1/user/list?page_size=3"],
                          capture_output=True, text=True, timeout=5)
        if "code\":0" in r.stdout:
            log(f"      AdsPower API: ✅")
    
    # CDP tunnels
    r = subprocess.run(["pgrep", "-a", "autossh"], capture_output=True, text=True, timeout=5)
    for line in r.stdout.strip().split("\n"):
        if "autossh" in line and "-L" in line:
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "-L" and i + 1 < len(parts):
                    fwd = parts[i + 1]
                    log(f"   🔗 autossh tunnel: {fwd} (PID {parts[0]})")
    
    # List any stale non-autossh SSH tunnels
    r2 = subprocess.run(["pgrep", "-a", "ssh.*-L.*-N"], capture_output=True, text=True, timeout=5)
    for line in r2.stdout.strip().split("\n"):
        if line.strip() and "autossh" not in line:
            log(f"   ⚠️  Stale SSH tunnel: {line[:100]}")


def main():
    parser = argparse.ArgumentParser(description="AdsPower Tunnel Manager v2")
    parser.add_argument("action", choices=["start", "stop", "status", "api", "cdp"])
    parser.add_argument("port", nargs="?", type=int, help="Remote CDP port")
    parser.add_argument("--local", type=int, default=None,
                        help="Local port to forward to (default: same as remote port)")
    parser.add_argument("--profile", type=str, default=None,
                        help="AdsPower profile_id (for /tmp/tunnel_mgr registry record)")
    args = parser.parse_args()

    if args.action == "start":
        log("=" * 50)
        log("🚇 Starting all tunnels...")
        log("=" * 50)
        start_api_tunnel()
        status()

    elif args.action == "api":
        start_api_tunnel()

    elif args.action == "stop":
        stop_all()

    elif args.action == "status":
        status()

    elif args.action == "cdp":
        if not args.port:
            print("❌ Usage: tunnel_mgr.py cdp <remote_port> [--local <local_port>] [--profile <id>]")
            sys.exit(1)
        ok = start_cdp_tunnel(args.port, local_port=args.local)
        # 写入 /tmp/tunnel_mgr/<profile>.json 注册表（供 infra.py 复用感知）
        if ok and args.profile:
            try:
                _register_tunnel(
                    profile_id=args.profile,
                    local_port=args.local if args.local is not None else args.port,
                    remote_port=args.port,
                )
            except Exception as _e:
                log(f"   ⚠️ registry write failed: {_e}")
        sys.exit(0 if ok else 1)

    save_pids()


def _register_tunnel(profile_id: str, local_port: int, remote_port: int):
    """把刚建好的隧道信息写入 /tmp/tunnel_mgr/<profile>.json 注册表。"""
    import json as _json
    reg_dir = "/tmp/tunnel_mgr"
    os.makedirs(reg_dir, exist_ok=True)
    reg_file = os.path.join(reg_dir, f"{profile_id}.json")
    # 查 PID（pgrep autossh）
    pid = None
    try:
        r = subprocess.run(
            ["pgrep", "-f", f"autossh.*{local_port}:127.0.0.1:{remote_port}"],
            capture_output=True, text=True, timeout=5,
        )
        if r.stdout.strip():
            pid = int(r.stdout.strip().split("\n")[0])
    except Exception:
        pid = None
    payload = {
        "profile_id": profile_id,
        "local_port": local_port,
        "remote_port": remote_port,
        "pid": pid,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(reg_file, "w") as f:
        _json.dump(payload, f, indent=2)
    log(f"   📝 registry: {reg_file} {payload}")


def load_pids():
    global PIDS
    try:
        with open(PID_FILE) as f:
            import json
            PIDS = json.load(f)
    except:
        PIDS = {}


def save_pids():
    import json
    try:
        with open(PID_FILE, "w") as f:
            json.dump(PIDS, f)
    except:
        pass


if __name__ == "__main__":
    load_pids()
    main()
