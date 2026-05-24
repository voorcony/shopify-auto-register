#!/usr/bin/env python3
"""
Shopify 自动注册 SOP — 标准操作程序
===================================
一条指令跑完全流程：
  ① 读飞书 → 下一个待注册配置 + prompt
  ② 从飞书同步经验补丁 → 覆盖本地
  ③ 启动 AdsPower + SSH 隧道
  ④ 检测 LLM 服务可用性
  ⑤ 组装完整 prompt = 基线 + 资料 + 经验补丁
  ⑥ 跑 browser-use + bu-30b (AutoPhaseRunner 自动分阶段)
  ⑦ 更新飞书状态 + 推经验回飞书

用法:
  python3 shopify_sop_runner.py                     # 自动找下一个
  python3 shopify_sop_runner.py --profile k1cl9nd6  # 指定配置
  python3 shopify_sop_runner.py --retry              # 重试上一个失败的
  
密钥管理:
  所有敏感信息从飞书 81b55a 表加载（config.load_feishu_secrets()），
  不在代码中硬编码任何密钥。
"""

import argparse, asyncio, json, os, socket, subprocess, sys, time, traceback
from datetime import datetime
from pathlib import Path

# ─── 路径适配 ────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_HOME = Path.home()  # /home/ubuntu
EXPERIENCE_FILE = str(_HOME / "shopify_experience.json")

# ─── 非敏感常量 ──────────────────────────────────
FEISHU_SHEET_REGIS = "T8Za6f"   # 注册资料 sheetId
FEISHU_SHEET_PROMPT = "0Uq2PS"  # 自动化prompt sheetId
FEISHU_SHEET_EXP = "11xbRm"     # 经验库 sheetId

MAX_STEPS = 100
TUNNEL_WAIT = 4.0

# ─── 密钥加载（延迟到 _init_config）─────────────
_feishu_app_id = ""
_feishu_secret = ""
_feishu_sheet_token = ""
_adspower_api_key = ""
_adspower_base = ""
_llm_base_url = ""
_ads_server = ""  # AdsPower Windows 服务器 IP
_ads_ssh_pass = ""

# ─── 配置初始化 ──────────────────────────────────

def _init_config():
    """从飞书 81b55a 表 + config.yaml 加载所有密钥和配置。"""
    global _feishu_app_id, _feishu_secret, _feishu_sheet_token
    global _adspower_api_key, _adspower_base, _llm_base_url
    global _ads_server, _ads_ssh_pass

    sys.path.insert(0, str(_HERE))
    from lib import config

    cfg = config.load_feishu_secrets()
    feishu_cfg = cfg.get("feishu", {})
    _feishu_app_id = feishu_cfg.get("app_id", "")
    _feishu_secret = feishu_cfg.get("app_secret", "")
    _feishu_sheet_token = feishu_cfg.get("sheet_token", "")

    adspower_cfg = cfg.get("adspower", {})
    _adspower_api_key = adspower_cfg.get("api_key", "")
    _adspower_base = adspower_cfg.get("base_url", "")

    llm_cfg = cfg.get("llm", {})
    _llm_base_url = llm_cfg.get("base_url", "")

    # AdsPower Windows 服务器
    infra_cfg = cfg.get("infra", {})
    _ads_server = infra_cfg.get("ads_server", "43.155.1.195")
    _ads_ssh_pass = infra_cfg.get("ads_ssh_pass", "ZHOUjiahao1!")


# ─── 经验补丁管理 ────────────────────────────────

def load_experience() -> list[dict]:
    """加载经验补丁库"""
    default = [
        {
            "trigger": "信用卡/付费页面",
            "patch": "如果页面让你输入信用卡号或选择付费计划：直接忽略，找 '3-day free trial' 或 'Start free trial' 链接。不要在任何输入信用卡的页面停留。如果找不到免费选项，刷新页面重试。",
            "hits": 1,
        },
        {
            "trigger": "已有标签页",
            "patch": "先检查浏览器当前所有标签页。如果已有 Shopify admin 或 Syncee 标签页，直接复用，不要新建。",
            "hits": 1,
        },
        {
            "trigger": "页面空白/加载失败",
            "patch": "如果页面空白或超时：等 3 秒后刷新页面。刷新 3 次还不行就放弃该步骤，尝试用其他方式完成目标（比如直接导航到 URL）。",
            "hits": 1,
        },
        {
            "trigger": "Cloudflare/验证码",
            "patch": "如果遇到 Cloudflare 验证或 CAPTCHA：尝试与页面交互通过验证。如果无法通过，报告失败。",
            "hits": 1,
        },
    ]
    try:
        with open(EXPERIENCE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_experience(patches: list[dict]):
    """保存经验补丁库"""
    with open(EXPERIENCE_FILE, "w") as f:
        json.dump(patches, f, indent=2, ensure_ascii=False)

def add_experience(patches: list[dict], trigger: str, patch: str):
    """新增或更新一条经验"""
    for p in patches:
        if p["trigger"] == trigger:
            p["patch"] = patch
            p["hits"] = p.get("hits", 0) + 1
            return
    patches.append({"trigger": trigger, "patch": patch, "hits": 1})

# ─── Feishu 操作 ─────────────────────────────────

def _feishu_token() -> str:
    import requests
    r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                      json={"app_id": _feishu_app_id, "app_secret": _feishu_secret}, timeout=10)
    return r.json()["tenant_access_token"]

def _feishu_headers() -> dict:
    return {"Authorization": f"Bearer {_feishu_token()}"}

def read_registration_data() -> list[dict]:
    """读取注册资料表，返回待注册列表"""
    import requests
    h = _feishu_headers()
    r = requests.get(
        f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{_feishu_sheet_token}/values/{FEISHU_SHEET_REGIS}!A1:N20",
        headers=h, timeout=10)
    data = r.json()
    values = data.get("data", {}).get("valueRange", {}).get("values", [])
    if not values:
        return []
    headers_row = values[0]
    records = []
    for row in values[1:]:
        if not row:
            continue
        rec = {}
        for i, hdr in enumerate(headers_row):
            rec[hdr] = row[i] if i < len(row) else ""
        records.append(rec)
    return records

def read_prompt() -> str:
    """读取飞书自动化 prompt"""
    import requests
    h = _feishu_headers()
    r = requests.get(
        f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{_feishu_sheet_token}/values/{FEISHU_SHEET_PROMPT}!A1:A2",
        headers=h, timeout=10)
    data = r.json()
    values = data.get("data", {}).get("valueRange", {}).get("values", [])
    if len(values) >= 2 and len(values[1]) > 0:
        return values[1][0]
    return ""

def update_feishu_status(row_index: int, status: str, extra: dict = None):
    """更新飞书注册状态"""
    import requests
    h = _feishu_headers()
    h["Content-Type"] = "application/json"
    # Row 1 = header, so data row 1 = sheet row 2
    sheet_row = row_index + 2
    url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{_feishu_sheet_token}/values"
    body = {
        "valueRange": {
            "range": f"{FEISHU_SHEET_REGIS}!A{sheet_row}:A{sheet_row}",
            "values": [[status]]
        }
    }
    requests.put(url, headers=h, json=body, timeout=10)


def _extract_patch_text(cell) -> str:
    """从 Feishu 单元格提取纯文本（处理富文本多片段嵌套）"""
    if isinstance(cell, str):
        return cell
    if isinstance(cell, list):
        # 富文本格式：[{'text': '...', 'type': 'text'}, {'text': '...', 'link': '...', 'type': 'url'}]
        parts = []
        for item in cell:
            if isinstance(item, dict):
                parts.append(item.get("text", ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(cell)


def sync_experience_from_feishu():
    """
    从飞书经验库拉取→覆盖本地 JSON
    返回拉取到的经验条数
    """
    import requests
    h = _feishu_headers()
    r = requests.get(
        f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{_feishu_sheet_token}/values/{FEISHU_SHEET_EXP}!A1:D100",
        headers=h, timeout=10)
    data = r.json()
    values = data.get("data", {}).get("valueRange", {}).get("values", [])
    if len(values) < 2:
        print("   ⚠️  飞书经验库为空", flush=True)
        return 0

    # Header row
    header = values[0]  # [trigger, patch, hits, category]
    patches = []
    for row in values[1:]:
        if not row or not row[0]:
            continue
        trigger = str(row[0])
        patch = _extract_patch_text(row[1]) if len(row) > 1 else ""
        hits = int(row[2]) if len(row) > 2 and str(row[2]).isdigit() else 0
        patches.append({"trigger": trigger, "patch": patch, "hits": hits})

    with open(EXPERIENCE_FILE, "w") as f:
        json.dump(patches, f, indent=2, ensure_ascii=False)
    print(f"   ✅ 拉取 {len(patches)} 条经验补丁到本地", flush=True)
    return len(patches)


def sync_experience_to_feishu():
    """
    把本地 JSON 推送到飞书经验库
    """
    import requests
    try:
        with open(EXPERIENCE_FILE) as f:
            patches = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print("   ⚠️  本地经验文件不存在，跳过推送", flush=True)
        return

    h = _feishu_headers()
    h["Content-Type"] = "application/json"

    # 根据 trigger 分类
    reg_triggers = {
        "credit_card_or_payment_page", "existing_tabs", "blank_page_or_timeout",
        "cloudflare_verification", "registration_complete_check",
        "push_to_store_then_verify_products", "close_browser_on_success",
        "screenshot_format", "address_autocomplete_input"
    }

    header = ["trigger", "patch", "hits", "category"]
    rows = [header]
    for p in patches:
        cat = "nurturing" if "nurturing" in p["trigger"] else \
              ("registration" if p["trigger"] in reg_triggers else "general")
        rows.append([p["trigger"], p["patch"], str(p.get("hits", 0)), cat])

    num_rows = len(rows)
    range_str = f"{FEISHU_SHEET_EXP}!A1:D{num_rows}"
    write_url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{_feishu_sheet_token}/values"
    body = {
        "valueRange": {
            "range": range_str,
            "values": rows
        }
    }
    r = requests.put(write_url, headers=h, json=body, timeout=10)
    if r.json().get("code") == 0:
        print(f"   ✅ 推送 {len(patches)} 条经验到飞书", flush=True)
    else:
        print(f"   ⚠️  推送失败: {r.text[:100]}", flush=True)

# ─── AdsPower + Tunnel ──────────────────────────

ADS_SYS_CONFIG = {
    "os": "win",
    "resolution": "1280x1080",
}

def fix_profile_sys(profile_id: str):
    """确保配置的系统设置正确 (OS=win, 1280×1080)"""
    import httpx
    try:
        c = httpx.Client(base_url=_adspower_base, timeout=10)
        c.post("/api/v1/user/update", json={
            "user_id": profile_id,
            "sys": ADS_SYS_CONFIG,
        })
        c.close()
    except Exception:
        pass  # 非关键，不阻塞启动

def start_adsprofile(profile_id: str) -> dict:
    """启动 AdsPower 配置，返回 CDP 信息"""
    import httpx
    c = httpx.Client(base_url=_adspower_base, timeout=30)
    try:
        r = c.get("/api/v1/browser/start", params={
            "user_id": profile_id, "open_tabs": "1", "api_key": _adspower_api_key
        })
        data = r.json()
        if data.get("code") != 0:
            raise Exception(f"AdsPower start failed: {data}")
        ws = data.get("data", {}).get("ws", {})
        cdp = ws.get("puppeteer", "")
        debug_port = data.get("data", {}).get("debug_port", "")
        return {"cdp_url": cdp, "debug_port": debug_port, "raw": data}
    finally:
        c.close()

def _stop_profile(profile_id, base_url, api_key):
    """关闭 AdsPower 浏览器"""
    import httpx
    try:
        c = httpx.Client(base_url=base_url, timeout=10)
        c.get("/api/v1/browser/stop", params={"user_id": profile_id, "api_key": api_key})
        c.close()
        print("   ✅ 浏览器已关闭", flush=True)
    except Exception as e:
        print(f'   ⚠️ 关浏览器失败: {e}', flush=True)

def build_tunnel(cdp_url: str) -> tuple[int, str, None]:
    """建 SSH 隧道到 AdsPower — 使用 autossh 自动重连，返回 (local_port, local_cdp, None)"""
    import re, urllib.parse
    parsed = urllib.parse.urlparse(cdp_url)
    remote_port = parsed.port
    if not remote_port:
        m = re.search(r":(\d+)(?:/|$)", cdp_url)
        if m:
            remote_port = int(m.group(1))
        else:
            raise Exception(f"Cannot parse port from CDP: {cdp_url}")

    # Use same port locally for simplicity
    local_port = remote_port

    # Kill any stale tunnel on this port
    import subprocess as _sp
    _sp.run(["fuser", "-k", f"{local_port}/tcp"], capture_output=True, timeout=5)
    time.sleep(2)  # wait for port release

    # Build SSH tunnel directly (simple ssh -L, no autossh — CDP tunnel is short-lived)
    ssh_cmd = [
        "sshpass", "-p", _ads_ssh_pass,
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=30",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
        "-o", "TCPKeepAlive=yes",
        "-L", f"{local_port}:127.0.0.1:{remote_port}",
        "-N", "-f",
        f"administrator@{_ads_server}",
    ]
    _sp.run(ssh_cmd, capture_output=True, text=True, timeout=20)
    time.sleep(2)

    # Build CDP URL with new local port
    local_cdp = cdp_url.replace(str(remote_port), str(local_port))
    # Also replace host if needed
    if ":51919" in local_cdp or cdp_url.startswith("ws://") and "127.0.0.1" not in local_cdp:
        local_cdp = f"ws://127.0.0.1:{local_port}/devtools/browser/{cdp_url.split('/devtools/browser/')[-1]}"

    # Verify tunnel works
    for _verify_try in range(3):
        import socket as _sock
        _s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        _s.settimeout(3)
        _result = _s.connect_ex(("127.0.0.1", local_port))
        _s.close()
        if _result == 0:
            break
        print(f"   ⏳ 隧道未就绪，重试中... ({_verify_try+1}/3)", flush=True)
        time.sleep(3)
    else:
        raise Exception(f"CDP tunnel verification failed: port {local_port} not reachable")

    return local_port, local_cdp, None

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

def check_llm_alive() -> bool:
    """检查 LLM 服务 (BU-30b) 是否可用"""
    import httpx
    try:
        r = httpx.get(f"{_llm_base_url}/models", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


# ─── 并行启动 ⚡ ──────────────────────────────────

async def _startup_parallel(profile_id: str):
    """并行执行 LLM 检查 + AdsPower 启动 + SSH 隧道。

    LLM 健康检查独立，AdsPower 启动后立即触发隧道建立。
    预期：串行 6-11s → 并行 3-5s。

    返回: (llm_ok, cdp_info, local_port, local_cdp, tunnel_proc)
    任一步失败抛出异常 → 调用方终止本次 attempt。
    """
    import httpx

    async def _check_llm_async():
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{_llm_base_url}/models")
                return r.status_code == 200
        except Exception:
            return False

    async def _start_ads_async():
        loop = asyncio.get_event_loop()
        fix_profile_sys(profile_id)
        return await loop.run_in_executor(None, start_adsprofile, profile_id)

    # ── Phase 1: LLM 检查 + AdsPower 启动 并行 ──
    llm_task = asyncio.create_task(_check_llm_async())
    ads_task = asyncio.create_task(_start_ads_async())

    llm_ok, cdp_info = await asyncio.gather(llm_task, ads_task)

    if not llm_ok:
        raise Exception("LLM 服务不可用 (BU-30b)")

    # ── Phase 2: SSH 隧道 (依赖 AdsPower 结果) ──
    loop = asyncio.get_event_loop()
    local_port, local_cdp, tunnel_proc = await loop.run_in_executor(
        None, build_tunnel, cdp_info["cdp_url"]
    )

    return llm_ok, cdp_info, local_port, local_cdp, tunnel_proc

# ─── 任务阶段定义 ──────────────────────────────
# 每个阶段独立 prompt → 独立的 LLM 上下文，不再互相污染
# 只带当前阶段必要的信息，不背前面几十步的历史包袱

PHASES = [
    {
        "name": "register",
        "prompt": """Register a Shopify account.

CURRENT GOAL (do exactly this):
The browser may already be logged in with a store. CHECK the current page FIRST:
- If the browser is already at the admin dashboard, report SUCCESS immediately
- If there's a store selection page, pick any store to reach the dashboard

Otherwise:
1. Go to the Shopify website
2. Click "Start free trial" or "Get started"
3. Enter email when prompted
4. Click Continue/Next
5. Enter password and account info
6. On the plan selection page — find and click "3-day free trial" or "Skip" — NEVER enter credit card info
7. Navigate to the store's admin dashboard

ACCOUNT INFO:
Email: {email}
Password: {password}
Name: {first} {last}
Phone: {phone}

CRITICAL RULES:
- CHECK the current page FIRST before taking any action
- If the browser is already at the admin dashboard, report SUCCESS immediately
- Use the email and password above EXACTLY
- If you hit Cloudflare verification, wait 10-15 seconds — it auto-resolves
- DO NOT enter credit card or payment info — find "Skip" or free trial
- Once you reach the admin dashboard, report SUCCESS
- DO NOT close the browser when done""",
        "max_steps": 30,
    },
    {
        "name": "setup",
        "prompt": """Complete Shopify store setup/onboarding.

CURRENT GOAL (do exactly this):
The Shopify admin dashboard should already be open. Look at the current page.
If there's an onboarding wizard/setup checklist:
- Complete it (set country, timezone, currency, store name, etc.)
- Answer any industry/product questions
- Dismiss any upsells

If already past onboarding (you see the main admin sidebar), report SUCCESS immediately.

STORE NAME: {store}
COUNTRY: United States
TIMEZONE: America/Chicago
CURRENCY: USD

CRITICAL RULES:
- Check what page you're on first — don't navigate away unnecessarily
- If you already see the admin dashboard sidebar, you're done
- Skip any paid upgrades or marketing emails
- DO NOT close the browser when done""",
        "max_steps": 20,
    },
    {
        "name": "install-syncee",
        "prompt": """Install Syncee AI Dropship app from Shopify App Store.

CURRENT GOAL (do exactly this):
1. CHECK the current page FIRST. If you already see the Syncee dashboard or settings page, report SUCCESS immediately
2. Otherwise, from the Shopify admin dashboard, navigate to:
   https://apps.shopify.com/syncee-premium-ai-dropshipping
3. Click "Add app" or "Install" on the Syncee page
4. Confirm installation (may show permissions — click "Install")
5. Wait for the Syncee dashboard to load
6. Report the Syncee dashboard URL

CRITICAL RULES:
- Start from wherever the browser is — check current page first
- If you see "Already installed" or the Syncee dashboard, report SUCCESS
- If there's an onboarding wizard inside Syncee, just get to the main dashboard
- The correct app URL is: https://apps.shopify.com/syncee-1 (NOT syncee-premium-ai-dropshipping)
- DO NOT close the browser when done""",
        "max_steps": 15,
    },
    {
        "name": "import-products",
        "prompt": """Import products using Syncee AI Dropship.

CURRENT GOAL (do exactly this):
1. You should be on the Syncee dashboard already
2. Find the product import/search section
3. Select at least ONE product category to import from (e.g. Fashion, Accessories, etc.)
4. Search for products in that category
5. Click "Push all to store" or "Import all" to add products to Shopify
6. Navigate to the Shopify Products page to verify products are imported
7. If you see at least one product listed, report SUCCESS

VERIFICATION:
- Go to the Shopify Products page (use the store URL you are already on)
- If you see ANY products in the list, the task is done

CRITICAL RULES:
- Only need ONE successful product import to consider the task complete
- Do NOT wait for hundreds of products — one product = success
- Report the total number of products imported if you can see it
- DO NOT close the browser when done""",
        "max_steps": 25,
    },
]

def build_phases(profile_data: dict) -> list[dict]:
    """按 profile 数据组装每个阶段的完整 prompt"""
    email = profile_data.get("邮箱", "")
    # email could be a list/dict from feishu, extract text
    if isinstance(email, dict):
        email = email.get("text", "")
    elif isinstance(email, list):
        email = email[0].get("text", "") if email else ""

    shopify_pass = profile_data.get("shopify密码", "ZJHhewly@2025")
    if not shopify_pass:
        shopify_pass = "ZJHhewly@2025"
    first = profile_data.get("First_Name", "KYLEE")
    last = profile_data.get("Last_Name", "ACOBA")
    phone = profile_data.get("TEL", "7817455182")

    import random as _random
    _categories = ["Fashion","Style","Trend","Vogue","Chic","Luxe","Street","Urban","Modern","Elite","Prime","Noble","Royal","Icon","Aura","Glow","Halo","Vibe","Zen"]
    store = f"{first} {_random.choice(_categories)}"

    phases = []
    for phase in PHASES:
        task = phase["prompt"].format(
            email=email,
            password=shopify_pass,
            first=first,
            last=last,
            phone=phone,
            store=store,
        )
        if email:
            task += f"\n\nNOTE: Email {email} is already registered as a Shopify account. If prompted to create new account, use it to LOG IN instead."
        phases.append({
            "name": phase["name"],
            "task": task,
            "max_steps": phase["max_steps"],
        })
    return phases

# ─── 主流程 ────────────────────────────────────

async def run_agent(cdp_url: str, phases: list[dict]):
    """按任务阶段运行 browser-use — 自动分阶段避免 16K 上下文溢出。

    使用 lib/auto_phase_runner.AutoPhaseRunner 自动在 ~18 步切阶段。
    保留原始参数签名以实现向后兼容。
    """
    from lib.auto_phase_runner import AutoPhaseRunner

    # 将多个 phase 的 task 合并为一个完整任务
    full_task_parts = []
    dashboard_url = None
    for i, phase in enumerate(phases):
        name = phase.get("name", f"step-{i}")
        task = phase.get("task", "")
        full_task_parts.append(f"=== {name.upper()} ===\n{task}")

    full_task = "\n\n".join(full_task_parts)
    print(f"   📄 合并任务: {len(full_task)} chars, {len(phases)} 阶段", flush=True)

    # LLM 配置 (从 config 读取)
    llm_config = dict(
        model="bu-30b",
        base_url=_llm_base_url,
        api_key="not-needed",
        temperature=0.1,
        max_completion_tokens=8192,
    )

    runner = AutoPhaseRunner(
        llm_config=llm_config,
        cdp_url=cdp_url,
        viewport={"width": 1280, "height": 1080},
        max_steps_per_phase=18,
        max_phases=max(len(phases) * 2, 6),  # 每个阶段最多拆 2 次
        verbose=True,
    )

    result = await runner.run(full_task)

    # 向后兼容：从 result 中提取 history 和 final
    class CompatHistory:
        """包装 AutoPhaseRunner 返回值为兼容的 history 对象"""
        def __init__(self, runner_result):
            self._result = runner_result

        def final_result(self) -> str | None:
            return self._result.get("final_result") or None

        def is_done(self) -> bool:
            return self._result.get("success", False)

        def is_goal_achieved(self) -> bool:
            return self._result.get("success", False)

        def __len__(self) -> int:
            return self._result.get("total_steps", 0)

        def action_names(self) -> list:
            names = []
            for p in self._result.get("phases", []):
                if "history" in p:
                    try:
                        if hasattr(p["history"], "action_names"):
                            names.extend(p["history"].action_names())
                    except Exception:
                        pass
            return names

        @property
        def usage(self):
            u = self._result.get("total_tokens", {})
            return type('Usage', (), {
                "total_prompt_tokens": u.get("prompt", 0),
                "total_completion_tokens": u.get("completion", 0),
                "total_tokens": u.get("total", 0),
            })

    history = CompatHistory(result)
    final = result.get("final_result", "")

    # 提取 dashboard URL (兼容旧代码)
    if final:
        import re as _re
        url_match = _re.search(r'(https?://admin\.shopify\.com[^\s,)]+)', str(final))
        if url_match:
            dashboard_url = url_match.group(1)
            print(f"   🔗 Dashboard: {dashboard_url}", flush=True)

    total = result.get("total_tokens", {})
    print(f"\n{'='*52}", flush=True)
    print(f" 📊 总: {result.get('total_steps', 0)} 步, "
          f"{result.get('phases_completed', 0)} 阶段, "
          f"{result.get('total_elapsed', 0):.0f}s, "
          f"{total.get('total', 0):,} tokens", flush=True)
    print(f"{'='*52}", flush=True)

    return history, final

def main():
    parser = argparse.ArgumentParser(description="Shopify 自动注册 SOP")
    parser.add_argument("--profile", help="指定 AdsPower profile ID")
    parser.add_argument("--retry", action="store_true", help="重试上一个失败的")
    args = parser.parse_args()

    print("=" * 60)
    print(" 🏭  Shopify 自动注册 SOP")
    print("=" * 60)

    # ── 0. 初始化配置（从飞书 81b55a 加载密钥）──
    print("\n[0/8] 🔑 加载配置...", flush=True)
    _init_config()
    print(f"   ✅ 飞书: {_feishu_sheet_token[:8]}...", flush=True)
    print(f"   ✅ AdsPower: {_adspower_base}", flush=True)
    print(f"   ✅ LLM: {_llm_base_url}", flush=True)

    # ── 1. 读取飞书资料 ──
    print("\n[1/8] 📋 读取飞书注册资料...", flush=True)
    records = read_registration_data()
    if not records:
        print("❌ 飞书没有数据", flush=True)
        return

    # 找下一个待注册的
    target = None
    target_idx = -1
    for i, rec in enumerate(records):
        status = (rec.get('使用状态') or '').strip()
        if not status:
            target = rec
            target_idx = i
            break
    if args.profile:
        for i, rec in enumerate(records):
            if args.profile in (rec.get("配置文件名称") or ""):
                target = rec
                target_idx = i
                break
    if not target:
        print("✅ 所有配置都已注册，没有待处理的任务", flush=True)
        return

    print(f"   🎯 找到: {target.get('配置文件名称', '未知')} ({target.get('邮箱', '')})", flush=True)
    update_feishu_status(target_idx, "注册中...")

    # ── 2-4. 带重试的并行启动循环 ──
    MAX_RETRIES = 3
    last_error = None

    # 提前解析 profile_id
    profile_name = target.get("配置文件名称", "")
    profile_id = "k1cl9nd6"  # fallback
    if profile_name and "(" in profile_name and ")" in profile_name:
        profile_id = profile_name.split("(")[1].split(")")[0]

    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            print(f"\n{'='*50}", flush=True)
            print(f" 🔄 第 {attempt}/{MAX_RETRIES} 次重试", flush=True)
            print(f"{'='*50}", flush=True)

        # ── 并行启动: LLM 检查 + AdsPower + SSH 隧道 ⚡ ──
        print(f"\n[2/7] ⚡ 并行启动 (LLM 检查 + AdsPower + 隧道)...", flush=True)
        try:
            llm_ok, cdp_info, local_port, local_cdp, tunnel_proc = (
                asyncio.run(_startup_parallel(profile_id))
            )
            print(f"   ✅ LLM: {_llm_base_url}", flush=True)
            print(f"   ✅ Profile: {profile_id}", flush=True)
            print(f"   🔗 CDP: {cdp_info['cdp_url'][:60]}...", flush=True)
            print(f"   ✅ Tunnel: 127.0.0.1:{local_port} → Windows:{cdp_info['debug_port']}", flush=True)
            print(f"   🔗 Local CDP: {local_cdp[:80]}...", flush=True)
        except Exception as e:
            print(f"   ❌ 启动失败: {e}", flush=True)
            # 尝试清理 AdsPower（可能已部分启动）
            try:
                _stop_profile(profile_id, _adspower_base, _adspower_api_key)
            except Exception:
                pass
            if attempt == MAX_RETRIES:
                update_feishu_status(target_idx, f"失败: 启动{e}")
            continue

        # ── 5. 组装 prompt + 跑 agent ──
        print(f"\n[5/7] 🧩 组装阶段任务 + 启动 Agent...", flush=True)
        phases = build_phases(target)
        print(f"   📄 {len(phases)} 个阶段: {[p['name'] for p in phases]}", flush=True)
        print(f"   🤖 bu-30b @ local RTX 4090", flush=True)

        try:
            history, final = asyncio.run(run_agent(local_cdp, phases))
            result = final or "无结果"
            print(f"\n   ✅ 全部阶段完成!", flush=True)
            print(f"   📝 {result[:200]}", flush=True)
            last_error = None  # 成功，清空错误

            # ── 6. 更新飞书 ──
            print(f"\n[6/7] ✏️  更新飞书状态 + 安排后续任务...", flush=True)
            if "admin" in result.lower() or "dashboard" in result.lower() or "complete" in result.lower():
                update_feishu_status(target_idx, "已注册+已安装Syncee ✅")
                print("   ✅ 已标记为完成", flush=True)

                # 尝试提取 Dashboard URL
                import re as _re
                url_match = _re.search(r'(https?://admin\\.shopify\\.com[^\\s,)]+)', result)
                dashboard_url = url_match.group(1) if url_match else "未知"
                print(f"   📍 Dashboard URL: {dashboard_url}", flush=True)

                # 安排 28h 后养号任务
                print(f"   ⏰ 养号任务将在 28 小时后自动触发", flush=True)
                print(f"   ⏰ 养号完成 40h 后将自动触发 Payment 注册", flush=True)
            else:
                update_feishu_status(target_idx, "执行完成（需人工确认）")
                print("   ⚠️ 已标记为需确认", flush=True)
                patches = load_experience()
                add_experience(patches, "注册结果判断",
                              "完成注册后检查 URL 是否包含 'admin' 或 'dashboard'，如果没有则可能需要登录确认。")
                save_experience(patches)
                print("   📝 已记录经验补丁", flush=True)
            
            # 执行成功，跳出重试循环
            break

        except Exception as e:
            last_error = e
            print(f"\n   ❌ Agent 执行出错 (第{attempt}次): {e}", flush=True)
            traceback.print_exc()
            
            # 记录错误到经验库
            patches = load_experience()
            add_experience(patches, f"失败步骤: {str(e)[:30]}", f"此步骤出错: {e}")
            save_experience(patches)
            
            if attempt < MAX_RETRIES:
                print(f"   🔄 准备重试...", flush=True)
                # 关闭浏览器，切掉隧道，下一轮重新建
                _stop_profile(profile_id, _adspower_base, _adspower_api_key)
            else:
                print(f"   ❌ 已耗尽 {MAX_RETRIES} 次重试，标记失败", flush=True)
                update_feishu_status(target_idx, f"失败({MAX_RETRIES}次): {str(e)[:50]}")
                patches = load_experience()
                add_experience(patches, f"最终失败({MAX_RETRIES}次)", f"重试耗尽: {e}")
                sync_experience_to_feishu()
                print("   📝 已同步错误到飞书经验库", flush=True)

    print('\\\\n' + '=' * 60)
    print(' 🏁  SOP 执行完毕')
    # 清理：关闭浏览器（如果还没关）
    _stop_profile(profile_id, _adspower_base, _adspower_api_key)

if __name__ == "__main__":
    main()
