"""调度编排器 — 时间线管理 + 下一阶段调度指令

控制论意义（第3轮·自组织控制）：
    本模块给出"阶段图"（phase graph），其它模块（如 batch_scheduler）通过
    查询此图自主决定下一步动作，无需人工逐个发号施令。
"""

# ── 阶段图（Phase Graph）─────────────────────────────────────────────────
# key   = 当前完成阶段
# next  = 下一阶段命令名
# delay_h = 自上一阶段成功后须等待的小时数（避免风控）
# desc  = 人类可读的描述
PHASE_GRAPH: dict[str, dict] = {
    "register": {"next": "nurture", "delay_h": 28,
                 "desc": "养号（导入客户+创建订单）"},
    "setup":    {"next": "syncee",  "delay_h": 0,
                 "desc": "安装 Syncee 应用"},
    "syncee":   {"next": "import",  "delay_h": 0,
                 "desc": "通过 Syncee 导入产品"},
    "import":   {"next": "nurture", "delay_h": 28,
                 "desc": "养号（导入客户+创建订单）"},
    "nurture":  {"next": "payment", "delay_h": 40,
                 "desc": "注册 Shopify Payment"},
    "payment":  {"next": None,      "delay_h": 0,
                 "desc": "（终态：已完成全部 SOP）"},
}

# 完成阶段（终态）对应的 Feishu 状态码 → 视为"无下一步可执行"
TERMINAL_FEISHU_STATUSES = {
    "payment_registered", "completed", "done", "finished",
}

# Feishu 状态码 → 已完成哪一阶段
# 用于 batch_scheduler 推断 "该 profile 下一步该跑什么"
FEISHU_STATUS_TO_PHASE: dict[str, str] = {
    # 成功态
    "registered": "register",
    "setup_complete": "setup",
    "syncee_installed": "syncee",
    "products_imported": "import",
    "nurtured": "nurture",
    "payment_registered": "payment",
    # 待执行 / 空 → 当作还没注册
    "": "_pending",
    "待执行": "_pending",
    "pending": "_pending",
    "空": "_pending",
}


def next_phase(current_phase: str | None) -> str | None:
    """根据已完成阶段返回下一阶段命令名。

    Args:
        current_phase: 当前已完成的阶段名；None / 空字符串 / "_pending"
            表示该 profile 还未开始，应从 register 起步。

    Returns:
        下一阶段命令名（register/nurture/import/payment/setup）或 None
        （表示已到终态，无后续任务）。
    """
    if not current_phase or current_phase == "_pending":
        return "register"
    node = PHASE_GRAPH.get(current_phase)
    if not node:
        return None
    return node.get("next")


def phase_delay_hours(current_phase: str) -> int:
    """返回 current_phase 完成后到执行 next_phase 之间应等待的小时数。"""
    node = PHASE_GRAPH.get(current_phase)
    if not node:
        return 0
    return int(node.get("delay_h", 0))


def infer_completed_phase(feishu_status: str) -> str | None:
    """根据飞书"使用状态/状态"列推断当前已完成到哪个阶段。

    返回 PHASE_GRAPH 的 key（register/setup/...）或 "_pending"。
    无法识别返回 None。
    """
    if feishu_status is None:
        return "_pending"
    key = str(feishu_status).strip().lower()
    # 直接命中
    if key in FEISHU_STATUS_TO_PHASE:
        return FEISHU_STATUS_TO_PHASE[key]
    # 中文 / 大小写归一
    if feishu_status in FEISHU_STATUS_TO_PHASE:
        return FEISHU_STATUS_TO_PHASE[feishu_status]
    return None


def print_schedule(profile_id: str = "", stage: str = ""):
    """打印下一阶段的调度信息"""
    # P2-第3轮：复用 PHASE_GRAPH，避免两处定义漂移
    info_raw = PHASE_GRAPH.get(stage)
    info = None
    if info_raw and info_raw.get("next"):
        info = {
            "next": info_raw["next"],
            "delay": f"{info_raw.get('delay_h', 0)}小时",
            "desc": info_raw.get("desc", ""),
        }
    if info:
        print(f"\n⏰ 下一阶段调度:", flush=True)
        print(f"   📌 {stage} 完成 → {info['delay']}后执行 {info['next']}: {info['desc']}", flush=True)
        if profile_id:
            print(f"   🔗 Profile: {profile_id}", flush=True)
        print(f"   💡 命令: python3 shopify_controller.py {info['next']} --profile {profile_id}", flush=True)

    full_timeline = """【Shopify SOP 完整时间线】
第0天: 新建配置 + 域名邮箱（可并行）
第0天: 注册 Shopify + Syncee（间隔≥2h/个）
       ↓ 28h 后自动触发
第1天: 养号（导入客户+创建订单）
       ↓ 养号完成 40h 后自动触发
第3天: 注册 Shopify Payment"""

    if not stage:
        print(f"\n📋 {full_timeline}", flush=True)


def show_timeline():
    """显示完整SOP时间线"""
    print("\n" + "=" * 50, flush=True)
    print(" 📋  Shopify SOP 完整时间线", flush=True)
    print("=" * 50, flush=True)
    print("""
【时间线】
第0天: 新建配置 + 域名邮箱（可并行）
第0天: 注册 Shopify + Syncee（间隔≥2h/个）
       ↓ 28h 后自动触发
第1天: 养号（导入客户+创建订单）
       ↓ 养号完成 40h 后自动触发
第3天: 注册 Shopify Payment

【条件分支】
• 邮箱已有 → 跳过新建域名邮箱
• 使用状态=空 → 执行注册
• 使用状态=已注册 → 跳过注册，等待养号

【调度规则】
• 注册完成后 → 关闭浏览器 → 记录 Dashboard URL
• 注册时间 + 28h → 自动执行养号任务
• 养号完成 + 40h → 自动执行 Payment 注册

【核心原则】
• 任务间隔至少 2h
• 失败3次放弃，标记飞书
• 操作前检查已有标签页
""", flush=True)
