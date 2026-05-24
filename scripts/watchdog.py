#!/usr/bin/env python3
"""
watchdog.py — BU-30b GPU 显存恢复 Watchdog（Windows 端）

功能：
    1. 每 30 秒轮询飞书 81b55a 表，检查 B16 重启标志
    2. 若 B16=1，从 81b55a 读取 llama-server 和 cloudflared 重启命令
    3. 执行恢复操作：kill 旧进程 → 启动新进程
    4. 检测新 cloudflared URL → 写入 81b55a!B21（关键！Linux 侧用它更新配置）
    5. 恢复后清除标志（B16=0），失败标记 B16=3

部署：
    pip install requests
    python watchdog.py
"""

import os, re, subprocess, sys, time, traceback
from datetime import datetime
import requests

# ══════════════════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════════════════

CONFIG = {
    "feishu_app_id": os.environ.get("FEISHU_APP_ID", "cli_a9619830e2fadcd1"),
    "feishu_app_secret": os.environ.get("FEISHU_APP_SECRET", "kXdwL8yJZCDo9kwych0npgZ5W078RRkK"),
    "sheet_token": "IRFqsUM7Jh4Hybt96ZVc9e0Antc",
    "poll_interval": 30,
    "startup_wait_llama": 15,
    "startup_wait_tunnel": 8,
}

# 信号行号（与 Linux 端 lib/llm_health.py 一致）
FLAG, REASON, TS, HB = 16, 17, 18, 19
# B21: cloudflared 重启后的新 URL（Linux 侧会读取）
NEW_URL = 21
# 重启命令在 81b55a 中的行
CMD_DIR, CMD_LLAMA, CMD_TUNNEL = 11, 13, 15

API_BASE = "https://" + "open.feishu.cn/open-apis"


# ══════════════════════════════════════════════════════════════════════════
# 飞书 API
# ══════════════════════════════════════════════════════════════════════════

class Feishu:
    def __init__(self):
        self.token, self.expires = None, 0
    def _token(self):
        if self.token and time.time() < self.expires - 60:
            return self.token
        r = requests.post(f"{API_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": CONFIG["feishu_app_id"], "app_secret": CONFIG["feishu_app_secret"]}, timeout=10)
        d = r.json()
        self.token, self.expires = d["tenant_access_token"], time.time() + d.get("expire", 7200)
        return self.token
    def read(self, cell):
        r = requests.get(f"{API_BASE}/sheets/v2/spreadsheets/{CONFIG['sheet_token']}/values/81b55a!{cell}",
            headers={"Authorization": f"Bearer {self._token()}"}, timeout=10)
        v = r.json().get("data",{}).get("valueRange",{}).get("values",[])
        if not v or not v[0]: return ""
        val = v[0][0]
        if isinstance(val, dict): val = val.get("text","")
        elif isinstance(val, list): val = "".join(i.get("text","") if isinstance(i,dict) else str(i) for i in val)
        return str(val).strip()
    def write(self, cell, value):
        h = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}
        r = requests.put(f"{API_BASE}/sheets/v2/spreadsheets/{CONFIG['sheet_token']}/values",
            headers=h, json={"valueRange": {"range": f"81b55a!{cell}:{cell}", "values": [[value]]}}, timeout=10)
        return r.json().get("code") == 0


# ══════════════════════════════════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════════════════════════════════

def log(msg, level="INFO"):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level}] {msg}", flush=True)

def run_bg(cmd, workdir=None):
    """在后台启动进程（不等待结束）。"""
    si = None
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO(); si.dwFlags |= 1; si.wShowWindow = 0
    p = subprocess.Popen(cmd, shell=True, cwd=workdir, startupinfo=si,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    log(f"  PID={p.pid}: {cmd[:100]}")
    return p

def kill(name):
    r = subprocess.run(["taskkill","/f","/im",name], capture_output=True, text=True, timeout=10)
    log(f"  {'Killed' if r.returncode==0 else 'Not running'}: {name}")

def detect_tunnel_url():
    """扫描 cloudflared 日志/进程，提取 trycloudflare.com URL。

    cloudflared 启动后会在控制台输出类似：
        https://xxxx-xxxx.trycloudflare.com
    也可能会写入日志文件。
    """
    # 方法1：从 cloudflared 日志目录读取最新日志
    log_dir = os.path.expanduser("~/.cloudflared")
    log_file = os.path.join(log_dir, "cloudflared.log")
    if os.path.exists(log_file):
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", content)
            if urls:
                return urls[-1]  # 取最新的
        except: pass

    # 方法2：从 process 抓取（如果 cloudflared 还在跑）
    try:
        r = subprocess.run(
            ['powershell', '-Command',
             'Get-Process cloudflared | ForEach { $_.CommandLine } 2>$null'],
            capture_output=True, text=True, timeout=10
        )
        urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", r.stdout + r.stderr)
        if urls:
            return urls[-1]
    except: pass

    # 方法3：扫描 temp 目录下的 cloudflared 日志
    for root, dirs, files in os.walk(os.environ.get("TEMP", "C:\\Temp")):
        for f in files:
            if "cloudflared" in f.lower() and f.endswith((".log", ".txt")):
                try:
                    with open(os.path.join(root, f), "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                    urls = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", content)
                    if urls:
                        return urls[-1]
                except: pass

    return None


# ══════════════════════════════════════════════════════════════════════════
# 恢复流程
# ══════════════════════════════════════════════════════════════════════════

def do_restart(f):
    log("=" * 60); log("🔄 开始 LLM 恢复流程")
    f.write(f"B{FLAG}", "2")  # 标记：重启中

    # 1. 从 81b55a 读命令
    llama_dir = f.read(f"B{CMD_DIR}")
    llama_cmd = f.read(f"B{CMD_LLAMA}")
    tunnel_cmd = f.read(f"B{CMD_TUNNEL}")
    if not llama_cmd:
        log("❌ 81b55a!B13 为空，无法启动", "ERR")
        return False

    # 2. 杀旧进程
    log("Step 1/5: 杀死旧进程...")
    kill("llama-server.exe"); kill("cloudflared.exe"); time.sleep(3)

    # 3. 启动 llama-server
    log("Step 2/5: 启动 llama-server...")
    wd = llama_dir or None
    run_bg(llama_cmd, wd)
    time.sleep(CONFIG["startup_wait_llama"])

    # 4. 启动 cloudflared tunnel
    log("Step 3/5: 启动 cloudflared tunnel...")
    if tunnel_cmd:
        run_bg(tunnel_cmd)
        time.sleep(CONFIG["startup_wait_tunnel"])
    else:
        log("ℹ️ 未配置 tunnel 命令，跳过")

    # 5. 检测新 URL（关键！）
    log("Step 4/5: 检测新 tunnel URL...")
    new_url = None
    for attempt in range(5):
        new_url = detect_tunnel_url()
        if new_url:
            break
        log(f"  等待 URL... ({attempt+1}/5)")
        time.sleep(3)

    if new_url:
        # 写入完整 v1 base URL
        full_url = new_url.rstrip("/") + "/v1"
        f.write(f"B{NEW_URL}", full_url)
        log(f"✅ 新 URL: {full_url}")
    else:
        log("⚠️ 未能检测到新 URL，请手动写入 81b55a!B21", "WARN")

    # 6. 清除标志
    log("Step 5/5: 清除标志...")
    f.write(f"B{FLAG}", "0")
    f.write(f"B{TS}", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    log("✅ 恢复完成！")
    if new_url:
        log(f"新 tunnel URL 已写入 81b55a!B21: {full_url}")
    log("=" * 60)
    return True


# ══════════════════════════════════════════════════════════════════════════
# 主循环
# ══════════════════════════════════════════════════════════════════════════

def main():
    f = Feishu()
    log("🚀 BU-30b Watchdog 启动 (带 URL 自动检测)")
    while True:
        try:
            flag = f.read(f"B{FLAG}").strip()
            f.write(f"B{HB}", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

            if flag == "1":
                reason = f.read(f"B{REASON}")
                log(f"🚩 检测到恢复信号（{reason}）")
                if do_restart(f):
                    pass
                else:
                    for i in range(2):
                        log(f"重试 #{i+1}..."); time.sleep(5)
                        if do_restart(f): break
                    else:
                        f.write(f"B{FLAG}", "3")
                        log("❌ 连续失败，已标记 B16=3", "ERR")

            time.sleep(CONFIG["poll_interval"])
        except KeyboardInterrupt:
            log("👋 退出"); break
        except:
            traceback.print_exc()
            log("❌ 异常，继续轮询", "ERR")
            time.sleep(CONFIG["poll_interval"])

if __name__ == "__main__":
    main()
