"""LLM 客户端 — browser-use 调用封装 + 降级策略。

变更要点 (P0-1/P0-2/P1-5):
- run_phase 改为同步函数，内部用 asyncio.run() 驱动 agent
- 签名与 shopify_controller 对齐：phase/prompt/cdp_url/use_vision/max_steps/
  max_failures/model/base_url/temperature/max_tokens/validation
- 返回 dict {"success", "dashboard_url", "message"} 而非 tuple
- 不再从 phase["prompt_template"] 取 task，直接用调用方传入的 prompt
- 修复死分支（原 93-95 行 elif hasattr 与 if 条件相同）
- 修复 vision 降级时所有重试都关 vision 的问题：vision 重试只针对 vision 错误
- 验证逻辑同时检查 check_keywords 与 success/complete 字样，且避免
  "did not succeed" 误判为成功
"""
import asyncio
import re
import time
import sys
import traceback
from typing import Optional

sys.path.insert(0, "/home/agentuser/wa-automation")
sys.path.insert(0, "/home/agentuser/browser_automation_bridge")

from browser_use.llm.openai.chat import ChatOpenAI
from browser_use.browser.session import BrowserSession as Browser
from browser_use import Agent

from lib import config as cfg
from lib.llm_health import GpuHealthMonitor, check_tunnel_health, \
    check_tunnel_with_fallback, signal_recovery_needed, get_monitor


# ── 工厂函数 ───────────────────────────────────────────────────────────


def create_llm(model: Optional[str] = None, base_url: Optional[str] = None,
               temperature: Optional[float] = None,
               max_tokens: Optional[int] = None) -> ChatOpenAI:
    """创建 bu-30b 兼容 LLM 实例。所有参数若为 None 则从 config 读取。"""
    llm_cfg = cfg.load().get("llm", {})
    base_url = base_url or llm_cfg.get("base_url", "")
    model = model or llm_cfg.get("model", "bu-30b")
    t = temperature if temperature is not None else llm_cfg.get("temperature", 0.1)
    mt = max_tokens if max_tokens is not None else llm_cfg.get("max_tokens", 8192)

    llm = ChatOpenAI(
        model=model,
        base_url=base_url,
        api_key="not-needed",
        temperature=t,
        max_completion_tokens=mt,
    )
    # 本地模型不支持 frequency_penalty
    llm.frequency_penalty = 0
    return llm


def create_browser(cdp_url: str) -> Browser:
    """创建 browser-use Browser session"""
    return Browser(
        cdp_url=cdp_url,
        keep_alive=True,
        viewport={"width": 1280, "height": 1080},
        device_scale_factor=1.0,
    )


def create_agent(task: str, llm, browser, max_steps: int = 100,
                 max_failures: int = 5, use_vision: bool = True) -> Agent:
    """创建 browser-use Agent"""
    return Agent(
        task=task,
        llm=llm,
        browser=browser,
        use_vision=use_vision,
        use_thinking=True,
        max_failures=max_failures,
        generate_gif=False,
    )


# ── 内部异步执行体 ─────────────────────────────────────────────────────


async def _agent_run(task: str, cdp_url: str, *, use_vision: bool,
                     max_steps: int, max_failures: int,
                     model: Optional[str], base_url: Optional[str],
                     temperature: float, max_tokens: int):
    llm = create_llm(model=model, base_url=base_url,
                     temperature=temperature, max_tokens=max_tokens)
    browser = create_browser(cdp_url)
    agent = create_agent(task, llm, browser,
                         max_steps=max_steps,
                         max_failures=max_failures,
                         use_vision=use_vision)
    return await agent.run(max_steps=max_steps)


# ── 公开同步接口 ───────────────────────────────────────────────────────


def run_phase(phase: dict, prompt: str, cdp_url: str, *,
              use_vision: bool = True, max_steps: int = 25,
              max_failures: int = 6, model: Optional[str] = None,
              base_url: Optional[str] = None, temperature: float = 0.1,
              max_tokens: int = 8192,
              validation: Optional[dict] = None) -> dict:
    """运行一个阶段（同步接口，内部用 asyncio.run 驱动 agent）。

    Args:
        phase: phase 配置 dict（用于读取 prompt_template 之外的元数据）。
            注意：本函数不再从 phase 取 task；task 来自 ``prompt`` 参数。
        prompt: 已经渲染好的完整任务文本。
        cdp_url: Chrome DevTools Protocol URL。
        use_vision: 是否启用视觉模式（首次失败若是 vision 错误会降级）。
        max_steps / max_failures: agent 限制。
        model / base_url / temperature / max_tokens: LLM 参数。
        validation: { "check_keywords": [...], "max_retries": N } 用于结果校验。

    Returns:
        {"success": bool, "dashboard_url": str|None, "message": str}
    """
    validation = validation or phase.get("validation", {}) or {}
    check_keywords = validation.get("check_keywords", []) or []
    max_retries = int(validation.get("max_retries", 2))

    dashboard_url: Optional[str] = None
    vision_fallback_enabled = cfg.load().get("llm", {}).get(
        "vision_fallback", True
    )

    # current_vision 在循环之间会变化（仅当遇到 vision 错误时降级）
    # 自适应控制：vision 降级后并非永久 —— 每 2 次重试尝试一次恢复，
    # 若恢复后再次失败则继续降级，形成"探测—回退"的极值搜索。
    original_vision = bool(use_vision)
    current_vision = bool(use_vision)
    last_error: Optional[str] = None
    # 记录最近一次降级是在哪个 attempt（用于决定何时尝试恢复）
    last_downgrade_attempt: int = -1
    # 防抖：若刚刚尝试恢复又因 vision 错误失败，需立即再降级
    recovery_attempted_this_round: bool = False

    for attempt in range(max_retries + 1):
        # ── 前馈控制：每次 LLM 调用前快速探测隧道健康 ──────────────────
        # 钱学森：在干扰影响系统前提前测量并施加补偿。
        # 隧道断线 → 尝试从飞书读新 URL → 若找到则自动更新配置
        tunnel_alive, tunnel_msg, tunnel_new_url = check_tunnel_with_fallback(
            base_url, timeout=3.0)
        if not tunnel_alive:
            print(f"   🚫  Tunnel health check failed: {tunnel_msg}", flush=True)
            signal_recovery_needed("tunnel_down")
            return {
                "success": False,
                "dashboard_url": dashboard_url,
                "message": f"tunnel_down: {tunnel_msg}",
            }
        # 如果有新 URL（watchdog 重启后自动检测到），更新 base_url
        if tunnel_new_url and tunnel_new_url != base_url:
            base_url = tunnel_new_url
            print(f"   ✅  Tunnel URL updated to: {base_url}", flush=True)

        # 自适应 vision 恢复：每隔 2 次重试且当前处于降级态时探测一次
        # 控制论：极值搜索 —— 周期性地"摇一摇"控制量，看能否到更好工作点
        if (original_vision and not current_vision and vision_fallback_enabled
                and last_downgrade_attempt >= 0
                and attempt - last_downgrade_attempt >= 2):
            print(
                f"   🔄 Vision 自适应恢复探测 (attempt={attempt})：尝试重新启用 vision",
                flush=True,
            )
            current_vision = True
            recovery_attempted_this_round = True

        try:
            _t_start = time.time()
            history = asyncio.run(_agent_run(
                prompt, cdp_url,
                use_vision=current_vision,
                max_steps=max_steps,
                max_failures=max_failures,
                model=model, base_url=base_url,
                temperature=temperature, max_tokens=max_tokens,
            ))
            _t_elapsed = time.time() - _t_start

            # 提取最终结果文本（修复原 93-95 死分支）
            final: Optional[str] = None
            if hasattr(history, "final_result"):
                try:
                    final = history.final_result()
                except Exception:
                    final = None
            if final is None:
                final = str(history)[:500] if history is not None else ""

            # 提取 dashboard URL
            if not dashboard_url and final:
                url_match = re.search(
                    r"(https?://admin\.shopify\.com[^\s,)]+)", str(final)
                )
                if url_match:
                    dashboard_url = url_match.group(1)

            # 结果验证
            result_text = str(final or "")
            lower = result_text.lower()
            # 防 "did not succeed" 误判：先看是否含明确失败短语
            failure_phrases = (
                "did not succeed", "didn't succeed", "did not complete",
                "didn't complete", "failed to", "could not complete",
                "couldn't complete",
            )
            says_failed = any(p in lower for p in failure_phrases)

            keyword_hit = bool(check_keywords) and any(
                kw.lower() in lower for kw in check_keywords
            )
            says_succeeded = (not says_failed) and (
                "success" in lower or "completed successfully" in lower
                or "complete." in lower or "complete!" in lower
            )

            if keyword_hit or says_succeeded:
                # ── GPU 健康观测：记录本次调用的速度和 token 量 ──────
                # 估计 token 数：最终结果文本的字符数 ÷ 4（中英混合粗估）
                _estimated_tokens = max(1, len(result_text) // 4)
                try:
                    get_monitor().record_call(_estimated_tokens, _t_elapsed)
                    if get_monitor().is_degraded:
                        print(f"   ⚠️  GPU 显存退化检测（速度 {get_monitor().avg_tps:.1f} t/s，"
                              f"基线 {get_monitor().baseline_tps} t/s），"
                              f"发送恢复信号", flush=True)
                        signal_recovery_needed("gpu_degraded")
                except Exception:
                    pass  # 观测器异常不阻断主流程

                return {
                    "success": True,
                    "dashboard_url": dashboard_url,
                    "message": result_text[:500],
                }

            # 没有任何成功信号；记录后继续重试
            last_error = (
                f"validation failed: keywords={check_keywords}, "
                f"final={result_text[:200]}"
            )
            if attempt < max_retries:
                print(f"   ⚠️  阶段验证未通过 (第{attempt+1}次), 重试...",
                      flush=True)
                continue
            return {
                "success": False,
                "dashboard_url": dashboard_url,
                "message": last_error,
            }

        except Exception as e:
            error_msg = str(e).lower()
            is_vision_error = any(
                w in error_msg
                for w in ("vision", "image", "decode", "png", "jpeg")
            )

            # vision 错误：降级到纯文本 + 立即重试
            # 自适应控制：不再"永久降级"，记录降级时间点供周期性探测恢复
            if is_vision_error and current_vision and vision_fallback_enabled:
                if recovery_attempted_this_round:
                    # 刚才尝试过恢复又失败 —— 重新降级，但不重置周期
                    print(
                        f"   ⚠️  Vision 恢复探测失败 ({e})，继续降级",
                        flush=True,
                    )
                else:
                    print(
                        f"   ⚠️  Vision 失败 ({e}), 降级到纯文本模式重试...",
                        flush=True,
                    )
                current_vision = False
                last_downgrade_attempt = attempt
                recovery_attempted_this_round = False
                # 不计入失败次数：直接重试
                continue
            else:
                # 非 vision 错误下若处于"试探恢复中"状态，保持当前 vision
                # 直到下次周期再决定
                recovery_attempted_this_round = False

            last_error = f"{type(e).__name__}: {e}"
            if attempt < max_retries:
                print(
                    f"   ⚠️  执行失败 (第{attempt+1}次), 重试... ({e})",
                    flush=True,
                )
                continue

            print(f"   ❌ 阶段失败: {e}", flush=True)
            traceback.print_exc()
            return {
                "success": False,
                "dashboard_url": dashboard_url,
                "message": last_error,
            }

    # 理论不可达
    return {
        "success": False,
        "dashboard_url": dashboard_url,
        "message": last_error or "max retries exceeded",
    }
