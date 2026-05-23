"""
CAPTCHA 检测器 — 基于 DOM 选择器 + 页面标题的可靠检测，配合 AdsPower 打码插件
==================================================================

设计原则（控制论·前馈控制）:
  不检测 Agent 行为，只检测页面 DOM 状态。
  Agent 行为是后验信号（失败后才知道），DOM 是先验信号（出现前就能看到）。

支持的 CAPTCHA 类型:
  ① Cloudflare Turnstile  — iframe[src*='challenges.cloudflare.com']
  ② Google reCAPTCHA      — iframe[src*='google.com/recaptcha']
  ③ hCaptcha              — iframe[src*='hcaptcha.com']
  ④ Cloudflare 5s 盾      — title "Just a moment..." / "Checking your browser"

外部依赖:
  - browser-use BrowserSession（通过 cdp_client + Runtime.evaluate 执行 JS）
"""
from __future__ import annotations

import asyncio
from typing import Optional, Tuple


# ── CAPTCHA 特征库 ──────────────────────────────────────────
# 每个类型给出多个 DOM 选择器 + 标题模式，任一命中即认为存在
CAPTCHA_SIGNATURES: dict[str, dict] = {
    "cloudflare_turnstile": {
        "selectors": [
            "iframe[src*='challenges.cloudflare.com']",
            "div#cf-challenge",
            "div.cf-turnstile",
            "div#turnstile-widget",
        ],
        "title_patterns": [
            "just a moment",
            "checking your browser",
            "please wait",
            "verifying you are human",
        ],
        "wait_timeout": 60,  # 秒
    },
    "recaptcha": {
        "selectors": [
            "iframe[src*='google.com/recaptcha']",
            "iframe[src*='recaptcha/api2']",
            "div.g-recaptcha",
            "div#recaptcha",
        ],
        "title_patterns": [],
        "wait_timeout": 45,
    },
    "hcaptcha": {
        "selectors": [
            "iframe[src*='hcaptcha.com']",
            "iframe[src*='newassets.hcaptcha.com']",
            "div.h-captcha",
        ],
        "title_patterns": [],
        "wait_timeout": 45,
    },
}

# ── 轮询参数 ────────────────────────────────────────────────
POLL_INTERVAL = 3      # 每 3 秒检测一次
MAX_POLL_ATTEMPTS = 20  # 最多 60 秒（20×3），可被外部覆盖


# ── DOM 探针（通过 CDP Runtime.evaluate）─────────────────────

async def _evaluate_js(browser, js_expr: str) -> any:
    """在当前页执行 JS 表达式，返回求值结果。

    用 browser_use BrowserSession 的 CDP 通道，避免依赖具体 Page 对象。
    出错返回 None（容错优先，CAPTCHA 检测不能阻塞主流程）。
    """
    try:
        cdp_session = await browser.get_or_create_cdp_session()
        result = await cdp_session.cdp_client.send.Runtime.evaluate(
            params={"expression": js_expr, "returnByValue": True},
            session_id=cdp_session.session_id,
        )
        if result and "result" in result:
            return result["result"].get("value")
    except Exception:
        # 浏览器可能正处于导航中，临时拿不到 session
        return None
    return None


async def element_exists(browser, selector: str) -> bool:
    """检测当前页是否存在匹配 selector 的元素（任意 frame 顶层 + iframe 标记可见）。"""
    # 用 JSON.stringify 安全转义，避免选择器中含引号
    safe_selector = selector.replace("\\", "\\\\").replace("'", "\\'")
    js = f"!!document.querySelector('{safe_selector}')"
    result = await _evaluate_js(browser, js)
    return bool(result)


async def get_page_title(browser) -> str:
    """读取当前页 document.title — 容错优先。"""
    try:
        # BrowserSession.get_current_page_title 优先（避免 JS 执行开销）
        if hasattr(browser, "get_current_page_title"):
            t = await browser.get_current_page_title()
            if t:
                return t
    except Exception:
        pass
    # Fallback：通过 JS 获取
    title = await _evaluate_js(browser, "document.title || ''")
    return str(title or "")


# ── 公开 API ────────────────────────────────────────────────

async def detect_captcha(browser) -> Tuple[Optional[str], Optional[str]]:
    """检测当前页是否存在 CAPTCHA。

    返回:
        (captcha_type, strategy)
          captcha_type 为 CAPTCHA_SIGNATURES 的键，如 "cloudflare_turnstile"
          strategy 当前固定为 "wait_for_plugin"
        若未检测到，返回 (None, None)

    检测顺序:
      1. DOM 选择器（最可靠）— 命中即返回
      2. 页面标题（辅助）— DOM 都未命中时再查
    """
    # 1) DOM 选择器优先
    for captcha_type, config in CAPTCHA_SIGNATURES.items():
        for selector in config["selectors"]:
            if await element_exists(browser, selector):
                return captcha_type, "wait_for_plugin"

    # 2) 标题辅助检测
    title = (await get_page_title(browser)).lower()
    if title:
        for captcha_type, config in CAPTCHA_SIGNATURES.items():
            for pattern in config.get("title_patterns", []):
                if pattern.lower() in title:
                    return captcha_type, "wait_for_plugin"

    return None, None


async def is_captcha_resolved(browser, captcha_type: str) -> bool:
    """检查指定类型的 CAPTCHA 是否已被打码插件解决（所有特征元素消失）。"""
    config = CAPTCHA_SIGNATURES.get(captcha_type)
    if not config:
        return True  # 未知类型 → 当作已解决

    # 任一 DOM 仍存在 → 未解决
    for selector in config["selectors"]:
        if await element_exists(browser, selector):
            return False

    # DOM 都消失了 → 看标题是否还在挑战页
    title = (await get_page_title(browser)).lower()
    for pattern in config.get("title_patterns", []):
        if pattern.lower() in title:
            return False

    return True


async def wait_for_captcha_resolution(
    browser,
    captcha_type: str,
    *,
    poll_interval: float = POLL_INTERVAL,
    max_attempts: int = MAX_POLL_ATTEMPTS,
    on_progress=None,
) -> Tuple[bool, int]:
    """阻塞等待 CAPTCHA 被解决（轮询 DOM）。

    Args:
        browser: BrowserSession
        captcha_type: detect_captcha 返回的类型
        poll_interval: 轮询间隔（秒）
        max_attempts: 最大尝试次数
        on_progress: 可选 callback(attempt, elapsed_seconds) — 每次轮询触发

    Returns:
        (resolved, elapsed_seconds)
          resolved=True  → CAPTCHA 已消失
          resolved=False → 超时
    """
    config = CAPTCHA_SIGNATURES.get(captcha_type, {})
    timeout = config.get("wait_timeout", poll_interval * max_attempts)
    # 根据特征自带的 wait_timeout 覆盖 max_attempts，取更宽松的那个
    effective_max = max(max_attempts, int(timeout // poll_interval))

    for attempt in range(1, effective_max + 1):
        await asyncio.sleep(poll_interval)
        elapsed = attempt * poll_interval

        if await is_captcha_resolved(browser, captcha_type):
            return True, elapsed

        if on_progress:
            try:
                on_progress(attempt, elapsed)
            except Exception:
                pass

    return False, effective_max * poll_interval
