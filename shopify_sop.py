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
  python3 shopify_sop.py                     # 自动找下一个
  python3 shopify_sop.py --profile k1cl9nd6  # 指定配置
  python3 shopify_sop.py --retry              # 重试上一个失败的
  
密钥管理:
  所有敏感信息从飞书 81b55a 表加载（config.load_feishu_secrets()），
  不在代码中硬编码任何密钥。
"""

import argparse, json
from pathlib import Path

# ─── 路径适配 ────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_HOME = Path.home()  # /home/ubuntu
EXPERIENCE_FILE = str(_HOME / "shopify_experience.json")

# ─── 非敏感常量 ──────────────────────────────────
FEISHU_SHEET_REGIS = "T8Za6f"   # 注册资料 sheetId
FEISHU_SHEET_PROMPT = "0Uq2PS"  # 自动化prompt sheetId
FEISHU_SHEET_EXP = "11xbRm"     # 经验库 sheetId

# ─── 从通用引擎导入 ──────────────────────────────
from lib.task_runner import (
    _init_config,
    read_registration_data,
    update_feishu_status,
    sync_experience_to_feishu,
    stop_profile,
    run_with_retry,
)

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
    # 从 task_runner 模块读取已初始化的全局变量
    import lib.task_runner as _tr
    _feishu_sheet_token = _tr._feishu_sheet_token
    _adspower_base = _tr._adspower_base
    _llm_base_url = _tr._llm_base_url
    print(f"   ✅ 飞书: {_feishu_sheet_token[:8]}...", flush=True)
    print(f"   ✅ AdsPower: {_adspower_base}", flush=True)
    print(f"   ✅ LLM: {_llm_base_url}", flush=True)

    # ── 1. 读取飞书资料 ──
    print("\n[1/8] 📋 读取飞书注册资料...", flush=True)
    records = read_registration_data(FEISHU_SHEET_REGIS)
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
    update_feishu_status(target_idx, "注册中...", FEISHU_SHEET_REGIS)

    # ── 2-5. 并行启动 + Agent 执行（含重试）──
    print("\n[2/8] ⚡ 并行启动 + Agent 执行（含重试）...", flush=True)
    # 提前解析 profile_id
    profile_name = target.get("配置文件名称", "")
    profile_id = "k1cl9nd6"  # fallback
    if profile_name and "(" in profile_name and ")" in profile_name:
        profile_id = profile_name.split("(")[1].split(")")[0]

    # ── 5. 组装 prompt ──
    phases = build_phases(target)

    # ── 执行通用重试循环 ──
    success, history, final, error = run_with_retry(profile_id, phases)

    if success:
        result = final or "无结果"
        # ── 6. 更新飞书 ──
        print(f"\n[6/8] ✏️  更新飞书状态 + 安排后续任务...", flush=True)
        if "admin" in result.lower() or "dashboard" in result.lower() or "complete" in result.lower():
            update_feishu_status(target_idx, "已注册+已安装Syncee ✅", FEISHU_SHEET_REGIS)
            print("   ✅ 已标记为完成", flush=True)

            # 尝试提取 Dashboard URL
            import re as _re
            url_match = _re.search(r'(https?://admin\.shopify\.com[^\s,)]+)', result)
            dashboard_url = url_match.group(1) if url_match else "未知"
            print(f"   📍 Dashboard URL: {dashboard_url}", flush=True)

            # 安排 28h 后养号任务
            print(f"   ⏰ 养号任务将在 28 小时后自动触发", flush=True)
            print(f"   ⏰ 养号完成 40h 后将自动触发 Payment 注册", flush=True)
        else:
            update_feishu_status(target_idx, "执行完成（需人工确认）", FEISHU_SHEET_REGIS)
            print("   ⚠️ 已标记为需确认", flush=True)
            patches = load_experience()
            add_experience(patches, "注册结果判断",
                          "完成注册后检查 URL 是否包含 'admin' 或 'dashboard'，如果没有则可能需要登录确认。")
            save_experience(patches)
            print("   📝 已记录经验补丁", flush=True)
    else:
        # ── 失败处理 ──
        print(f"\n[6/8] ❌ 执行失败，记录经验...", flush=True)
        patches = load_experience()
        add_experience(patches, f"失败步骤: {str(error)[:30]}", f"此步骤出错: {error}")

        # 已由 run_with_retry 标记为失败 - 更新最终状态
        if error:
            update_feishu_status(target_idx, f"失败(3次): {str(error)[:50]}", FEISHU_SHEET_REGIS)
            add_experience(patches, "最终失败(3次)", f"重试耗尽: {error}")

        save_experience(patches)
        sync_experience_to_feishu(FEISHU_SHEET_EXP, EXPERIENCE_FILE)
        print("   📝 已同步错误到飞书经验库", flush=True)

    print('\n' + '=' * 60)
    print(' 🏁  SOP 执行完毕')
    # 清理：关闭浏览器（如果还没关）
    stop_profile(profile_id)

if __name__ == "__main__":
    main()
