#!/usr/bin/env python3
"""
watchdog.py — BU-30b GPU 显存恢复 Watchdog（Windows 端）

功能：
    1. 每 30 秒轮询飞书 81b55a 表，检查 B16 重启标志
    2. 若 B16=1，从 81b55a 读取 llama-server 和 cloudflared 重启命令
    3. 执行恢复操作：kill 旧进程 → 启动新进程
    4. 恢复后清除标志（B16=0），失败标记 B16=3

部署：
    在本机 Windows 上运行：python watchdog.py
    建议最小化窗口或用 nssm 注册为 Windows 服务。

依赖：
    pip install requests

配置：
    通过以下环境变量设置飞书凭证（或直接修改下方 CONFIG）：
        FEISHU_APP_ID=<cli_a96...>
        FEISHU_APP_SECRET=<kXdwL8...>
"""

import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime

import requests


# ══════════════════════════════════════════════════════════════════════════
# 配置区（优先读环境变量，否则用默认值）
# ══════════════════════════════════════════════════════════════════════════

CONFIG = {
    # 飞书应用凭证
    "feishu_app_id": os.environ.get("FEISHU_APP_ID",
                                     "cli_a9619830e2fadcd1"),
    "feishu_app_secret": os.environ.get("FEISHU_APP_SECRET",
                                         "kXdwL8yJZCDo9kwych0npgZ5W078RRkK"),
    # 飞书表格
    "sheet_token": "IRFqsUM7Jh4Hybt96ZVc9e0Antc",
    # 轮询间隔（秒）
    "poll_interval": 30,
    # 启动后等待进程就绪的时间（秒）
    "startup_wait": 15,
    # 日志文件路径（None = 输出到控制台）
    "log_file": None,
}

# 81b55a 信号行号（与 Linux 端 llm_health.py 一致）
FLAG_ROW = 16       # B16: 重启标志（0=正常, 1=需要重启, 2=重启中, 3=重启失败）
REASON_ROW = 17     # B17: 重启原因
TIMESTAMP_ROW = 18  # B18: 最后重启时间戳
HEARTBEAT_ROW = 19  # B19: 心跳

# LLM 重启命令在 81b55a 中的行号
CMD_LLAMA_DIR = 11      # B11: 工作目录（如 "D:"）
CMD_LLAMA_CD = 12       # B12: cd 命令（如 "cd D:\\llama"）
CMD_LLAMA_RUN = 13      # B13: llama-server 启动命令
CMD_TUNNEL_RUN = 15     # B15: cloudflared tunnel 启动命令


# ══════════════════════════════════════════════════════════════════════════
# 飞书 API 封装
# ══════════════════════════════════════════════════════════════════════════

API_BASE = "https://" + "open.feishu.cn/open-apis"


class FeishuClient:
    """轻量飞书 API 客户端（仅在 Windows 上用，独立于 lib.feishu）。"""

    def __init__(self):
        self._app_id = CONFIG["feishu_app_id"]
        self._app_secret = CONFIG["feishu_app_secret"]
        self._token = None
        self._expires_at = 0
        self._sheet_token = CONFIG["sheet_token"]

    def _get_token(self) -> str:
        """获取 tenant_access_token，过期自动刷新。"""
        now = time.time()
        if self._token and now < self._expires_at - 60:
            return self._token
        r = requests.post(
            f"{API_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
            timeout=10,
        )
        data = r.json()
        if "tenant_access_token" not in data:
            raise RuntimeError(f"Feishu auth failed: {data}")
        self._token = data["tenant_access_token"]
        self._expires_at = now + data.get("expire", 7200)
        return self._token

    def _headers(self):
        return {"Authorization": f"Bearer {self._get_token()}"}

    def read_cell(self, sheet: str, cell: str) -> str:
        """读取飞书表格一个单元格的值。"""
        url = (f"{API_BASE}/sheets/v2/spreadsheets/{self._sheet_token}"
               f"/values/{sheet}!{cell}")
        r = requests.get(url, headers=self._headers(), timeout=10)
        data = r.json()
        values = data.get("data", {}).get("valueRange", {}).get("values", [])
        if not values or not values[0]:
            return ""
        val = values[0][0]
        if isinstance(val, dict):
            val = val.get("text", "")
        elif isinstance(val, list):
            val = "".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in val
            )
        return str(val).strip()

    def write_cell(self, sheet: str, cell: str, value: str) -> bool:
        """写入飞书表格一个单元格。"""
        url = (f"{API_BASE}/sheets/v2/spreadsheets/{self._sheet_token}"
               f"/values")
        body = {
            "valueRange": {
                "range": f"{sheet}!{cell}:{cell}",
                "values": [[value]],
            }
        }
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        r = requests.put(url, headers=headers, json=body, timeout=10)
        return r.json().get("code") == 0


# ══════════════════════════════════════════════════════════════════════════
# 日志
# ══════════════════════════════════════════════════════════════════════════

def log(msg: str, level: str = "INFO") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}"
    print(line, flush=True)
    if CONFIG.get("log_file"):
        try:
            with open(CONFIG["log_file"], "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ══════════════════════════════════════════════════════════════════════════
# 核心逻辑
# ══════════════════════════════════════════════════════════════════════════

def _run_cmd(cmd: str, workdir: str | None = None, timeout: int = 30) -> bool:
    """执行一条命令并等待结束。

    对于需要后台持续运行的进程（llama-server、cloudflared），
    使用 CREATE_NO_WINDOW 标志避免弹出控制台窗口。
    """
    try:
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=workdir,
            startupinfo=startupinfo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        log(f"   🚀 CMD: {cmd[:120]} (PID={proc.pid})", level="INFO")
        return True
    except Exception as e:
        log(f"   ❌ 启动失败: {e}", level="ERR")
        return False


def _kill_process(name: str) -> None:
    """强制终止指定名称的进程。"""
    try:
        result = subprocess.run(
            ["taskkill", "/f", "/im", name],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            log(f"   ✅ Killed: {name}", level="INFO")
        else:
            # 0x128 = 没有找到进程
            log(f"   ℹ️  {name}: {result.stderr.strip() or 'not running'}",
                level="INFO")
    except Exception as e:
        log(f"   ⚠️  杀进程失败 {name}: {e}", level="WARN")


def do_restart(feishu: FeishuClient) -> bool:
    """执行完整的恢复流程：杀进程 → 重启 llama-server → 重启 tunnel。

    Returns:
        True 若恢复成功，False 若失败。
    """
    log("=" * 60, level="INFO")
    log("🔄 开始 LLM 恢复流程", level="INFO")

    # ── 标记为"重启中" ──────────────────────────────────────────────
    feishu.write_cell("81b55a", f"B{FLAG_ROW}", "2")

    # ── 从 81b55a 读取重启命令 ────────────────────────────────────────
    llama_dir = feishu.read_cell("81b55a", f"B{CMD_LLAMA_DIR}")
    llama_cd = feishu.read_cell("81b55a", f"B{CMD_LLAMA_CD}")
    llama_cmd = feishu.read_cell("81b55a", f"B{CMD_LLAMA_RUN}")
    tunnel_cmd = feishu.read_cell("81b55a", f"B{CMD_TUNNEL_RUN}")

    log(f"   llama目录: {llama_dir}", level="INFO")
    log(f"   cd:        {llama_cd}", level="INFO")
    log(f"   llama命令: {llama_cmd[:80]}...", level="INFO")
    log(f"   tunnel命令:{tunnel_cmd[:80]}...", level="INFO")

    if not llama_cmd:
        log("   ❌ 81b55a 中找不到 llama-server 启动命令（B13 为空）",
            level="ERR")
        return False

    # ── 第1步：杀旧进程 ──────────────────────────────────────────────
    log("   📍 Step 1/4: 杀死旧进程...", level="INFO")
    _kill_process("llama-server.exe")
    _kill_process("cloudflared.exe")
    time.sleep(3)

    # ── 第2步：启动 llama-server ─────────────────────────────────────
    log("   📍 Step 2/4: 启动 llama-server...", level="INFO")
    workdir = None
    if llama_dir:
        workdir = llama_dir
    elif llama_cd and llama_cd.startswith("cd "):
        workdir = llama_cd[3:].strip()

    if workdir:
        log(f"   工作目录: {workdir}", level="INFO")

    llama_ok = _run_cmd(llama_cmd, workdir=workdir)
    if not llama_ok:
        return False

    # 等待 llama-server 就绪
    log(f"   ⏳ 等待 {CONFIG['startup_wait']} 秒让 llama-server 启动...",
        level="INFO")
    time.sleep(CONFIG["startup_wait"])

    # ── 第3步：启动 cloudflared tunnel ───────────────────────────────
    log("   📍 Step 3/4: 启动 cloudflared tunnel...", level="INFO")
    if tunnel_cmd:
        tunnel_ok = _run_cmd(tunnel_cmd)
        if not tunnel_ok:
            log("   ⚠️  Tunnel 启动失败，但 llama-server 已启动", level="WARN")
            # 不返回 False，tunnel 可以手动补
    else:
        log("   ℹ️  81b55a 中没有配置 tunnel 命令（B15 为空），跳过",
            level="INFO")

    # ── 第4步：写入恢复完成标志 ───────────────────────────────────────
    log("   📍 Step 4/4: 清除飞书标志...", level="INFO")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    feishu.write_cell("81b55a", f"B{FLAG_ROW}", "0")      # 标志归零
    feishu.write_cell("81b55a", f"B{TIMESTAMP_ROW}", timestamp)  # 记录时间

    log("✅ LLM 恢复流程完成！", level="INFO")
    log("=" * 60, level="INFO")
    return True


def poll_loop() -> None:
    """主轮询循环。"""
    feishu = FeishuClient()
    consecutive_failures = 0

    log("=" * 60, level="INFO")
    log("🚀 BU-30b Watchdog 启动", level="INFO")
    log(f"   轮询间隔: {CONFIG['poll_interval']} 秒", level="INFO")
    log(f"   飞书表格: {CONFIG['sheet_token']}", level="INFO")
    log(f"   信号行:   B{FLAG_ROW}（标志）", level="INFO")
    log("=" * 60, level="INFO")

    while True:
        try:
            # ── 读重启标志 ────────────────────────────────────────────
            flag = feishu.read_cell("81b55a", f"B{FLAG_ROW}")
            flag = flag.strip()

            # ── 更新心跳 ──────────────────────────────────────────────
            feishu.write_cell(
                "81b55a", f"B{HEARTBEAT_ROW}",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )

            if flag == "1":
                reason = feishu.read_cell("81b55a", f"B{REASON_ROW}")
                log(f"🚩 检测到恢复信号（原因: {reason}）", level="INFO")

                success = do_restart(feishu)
                if success:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        feishu.write_cell("81b55a", f"B{FLAG_ROW}", "3")
                        log("❌ 连续 3 次恢复失败，已标记为 FAILED（B16=3）",
                            level="ERR")

            elif flag == "3":
                log(f"⚠️  恢复已被标记为失败（B16=3），等待人工介入",
                    level="WARN")

            # ── 等待下一轮 ────────────────────────────────────────────
            time.sleep(CONFIG["poll_interval"])

        except KeyboardInterrupt:
            log("👋 Watchdog 被用户中断", level="INFO")
            break
        except Exception:
            traceback.print_exc()
            log(f"❌ 轮询异常，{CONFIG['poll_interval']} 秒后重试",
                level="ERR")
            time.sleep(CONFIG["poll_interval"])


# ══════════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    poll_loop()
