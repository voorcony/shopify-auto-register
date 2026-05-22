"""
AutoPhaseRunner — 自动分阶段执行 browser-use Agent，解决 BU-30b 16K 上下文窗口限制

核心原理：
  单个 browser-use Agent 每步增加 ~800-950 tokens，到 ~18 步时 16,384 窗口满。
  PhaseRunner 自动在 ~15-18 步时切阶段 → 新 LLM 上下文 → 共享 BrowserSession。

用法::

    from lib.auto_phase_runner import AutoPhaseRunner

    runner = AutoPhaseRunner(
        llm_config=dict(
            model="bu-30b",
            base_url="http://127.0.0.1:23434/v1",
            api_key="not-needed",
            temperature=0.1,
            max_completion_tokens=8192,
        ),
        cdp_url="ws://127.0.0.1:63779/devtools/browser/...",
        viewport={"width": 1280, "height": 1080},
        max_steps_per_phase=18,
        max_phases=6,
        verbose=True,
    )

    result = await runner.run(task="Navigate to amazon.com and search for shoes...")
    # {
    #   "success": True/False,
    #   "total_steps": 42,
    #   "phases_completed": 3,
    #   "total_tokens": {prompt, completion, total},
    #   "total_elapsed": 123.4,
    #   "final_result": "...",
    #   "all_results": [...],
    # }
"""
from __future__ import annotations

import asyncio
import json
import time
import traceback
from typing import Any


class AutoPhaseRunner:
    """自动分阶段执行 browser-use Agent。

    控制论意义：
        「被控系统的最优控制」—— 在上下文即将溢出 (16K) 前主动切阶段，
        等价于 Shopify SOP runner 的手动分阶段，但完全自动化。

    参数:
        llm_config: ChatOpenAI 构造参数
        cdp_url: AdsPower CDP WebSocket URL
        viewport: 浏览器视口
        max_steps_per_phase: 每阶段最大步数 (默认 30，硬上限兜底)
        max_tokens_per_phase: 每阶段最大 token 数 (默认 14000，BU-30b 16K 窗口留 2000 缓冲)
        max_phases: 最大阶段数 (默认 6 = 180 步)
        verbose: 打印详细日志
    """

    def __init__(
        self,
        llm_config: dict[str, Any],
        cdp_url: str,
        viewport: dict | None = None,
        max_steps_per_phase: int = 30,
        max_tokens_per_phase: int = 14000,
        max_phases: int = 6,
        verbose: bool = True,
    ):
        self.llm_config = llm_config
        self.cdp_url = cdp_url
        self.viewport = viewport or {"width": 1280, "height": 1080}
        self.max_steps_per_phase = max_steps_per_phase
        self.max_tokens_per_phase = max_tokens_per_phase
        self.max_phases = max_phases
        self.verbose = verbose

        self._browser = None
        self._total_steps = 0
        self._total_elapsed = 0.0
        self._all_results: list[dict] = []
        self._token_usage = {"prompt": 0, "completion": 0, "total": 0}
        self._current_url = ""
        self._phase_summaries: list[str] = []

    def _log(self, msg: str):
        if self.verbose:
            print(f"  🏃 [{msg}]", flush=True)

    # ── 阶段间状态提取 ──────────────────────────────────────

    def _extract_phase_summary(self, history) -> str:
        """从上一阶段 history 提取摘要给下一阶段用。

        提取：当前 URL、已完成的操作、最终结果。
        """
        parts = []

        # 当前 URL
        if hasattr(history, "urls") and history.urls:
            urls = history.urls
            if isinstance(urls, list) and urls:
                parts.append(f"Current URL: {urls[-1]}")
                self._current_url = urls[-1]

        # 最终结果
        final = ""
        if hasattr(history, "final_result"):
            final = str(history.final_result() or "")
        if final:
            parts.append(f"Progress so far: {final[:300]}")

        # 动作列表摘要
        action_names = []
        if hasattr(history, "action_names"):
            try:
                action_names = history.action_names()
            except Exception:
                pass
        if action_names:
            summary = ", ".join(action_names[-5:])  # 最近 5 个动作
            parts.append(f"Recent actions: {summary}")

        return "\n".join(parts)

    def _build_continuation_prompt(self, task: str, phase_idx: int, last_summary: str) -> str:
        """构造续接阶段的 prompt。

        第一阶段的 prompt = 原始任务。
        后续阶段的 prompt = 进度回顾 + 剩余任务。
        """
        if phase_idx == 0:
            return task

        continuation = (
            f"CONTINUE the previous task. Do NOT restart.\n\n"
            f"ORIGINAL TASK:\n{task}\n\n"
        )

        if last_summary:
            continuation += f"WHAT HAS BEEN DONE:\n{last_summary}\n\n"

        # 注入当前 URL
        if self._current_url:
            continuation += (
                f"INSTRUCTIONS:\n"
                f"1. The browser is already at: {self._current_url}\n"
                f"2. Continue from where you left off — do NOT go back to homepage\n"
                f"3. Do NOT repeat steps already completed\n"
                f"4. Complete the remaining steps\n"
            )
        else:
            continuation += (
                f"INSTRUCTIONS:\n"
                f"1. Continue from the current browser state\n"
                f"2. Do NOT repeat steps already completed\n"
                f"3. Complete the remaining steps\n"
            )

        return continuation

    # ── 单阶段执行 ──────────────────────────────────────────

    async def _run_phase(
        self,
        task: str,
        phase_idx: int,
    ) -> dict:
        """执行单个阶段，返回结果字典。

        使用手动 step 循环 (替代 agent.run()) 实现动态 token 跟踪：
        - 每步后实时累计 prompt + completion tokens
        - 当 token 消耗超过 max_tokens_per_phase 时主动切阶段
        - max_steps_per_phase 作为硬上限兜底
        - 任务完成 (is_done) 优先于 token 限制
        """
        from browser_use.llm.openai.chat import ChatOpenAI
        from browser_use import Agent

        phase_label = f"Phase {phase_idx+1}"
        self._log(f"{phase_label} — max {self.max_steps_per_phase} steps, "
                  f"budget {self.max_tokens_per_phase:,} tokens")

        llm = ChatOpenAI(**self.llm_config)

        agent = Agent(
            task=task,
            llm=llm,
            browser=self._browser,
            use_vision=True,
            use_thinking=True,
            max_failures=5,
            max_steps=self.max_steps_per_phase,
        )

        start_ts = time.time()
        phase_prompt = 0
        phase_completion = 0
        steps = 0
        stopped_by_token_limit = False

        try:
            # ── 手动 step 循环：每步后检查 token 消耗 ──
            for _step_idx in range(self.max_steps_per_phase):
                await agent.step()
                steps += 1

                # 从 agent history 中累计 token
                phase_prompt = 0
                phase_completion = 0
                if hasattr(agent, 'state') and agent.state:
                    history_obj = agent.state.history
                    if hasattr(history_obj, 'history'):
                        for h in history_obj.history:
                            meta = getattr(h, 'metadata', None)
                            if meta:
                                u = getattr(meta, 'usage', None)
                                if u:
                                    phase_prompt += getattr(u, 'prompt_tokens', 0)
                                    phase_completion += getattr(u, 'completion_tokens', 0)

                # 判断任务是否完成 (优先于 token 限制)
                is_done_flag = False
                if hasattr(agent, 'state') and agent.state:
                    hist = agent.state.history
                    if hasattr(hist, 'is_done'):
                        is_done_flag = hist.is_done()
                    if hasattr(hist, 'is_goal_achieved'):
                        is_done_flag = is_done_flag or hist.is_goal_achieved()

                if is_done_flag:
                    self._log(f"{phase_label} task done at step {steps}")
                    break

                # 动态 token 检查：当前累计 + 预估下步 (~900 tokens) > 预算 → 切阶段
                total_tokens = phase_prompt + phase_completion
                if total_tokens > 0 and total_tokens + 900 > self.max_tokens_per_phase:
                    self._log(f"{phase_label} token budget: {total_tokens:,}/{self.max_tokens_per_phase:,} "
                              f"({total_tokens*100//self.max_tokens_per_phase}%) — stopping")
                    stopped_by_token_limit = True
                    break

            elapsed = time.time() - start_ts

            # 获取最终 history (手动循环后 agent.state.history 即最终 state)
            history = agent.state.history if hasattr(agent, 'state') and agent.state else None

            # 提取结果
            is_done = False
            if history and hasattr(history, 'is_done'):
                is_done = history.is_done()
            if history and hasattr(history, 'is_goal_achieved'):
                is_done = history.is_goal_achieved()

            final = ""
            if history and hasattr(history, 'final_result'):
                final = str(history.final_result() or "")

            self._total_steps += steps
            self._total_elapsed += elapsed

            # Fallback: 如果 history 内没拿到 token，从 agent usage 属性取
            if phase_prompt == 0:
                if hasattr(history, 'usage') and history.usage:
                    u = history.usage
                    phase_prompt = u.total_prompt_tokens
                    phase_completion = u.total_completion_tokens

            self._token_usage["prompt"] += phase_prompt
            self._token_usage["completion"] += phase_completion
            self._token_usage["total"] += phase_prompt + phase_completion

            # 阶段摘要：记录 token 使用率
            token_pct = ((phase_prompt + phase_completion) * 100 // self.max_tokens_per_phase
                         if self.max_tokens_per_phase else 0)
            result = {
                "phase": phase_idx,
                "success": is_done,
                "steps": steps,
                "elapsed": elapsed,
                "prompt_tokens": phase_prompt,
                "completion_tokens": phase_completion,
                "total_tokens": phase_prompt + phase_completion,
                "token_budget_pct": token_pct,
                "stopped_by_token": stopped_by_token_limit,
                "final": final,
                "history": history,
            }
            self._all_results.append(result)

            status_icon = '✅' if is_done else ('🔄' if stopped_by_token_limit else '🔁')
            self._log(f"{phase_label} done: {status_icon} "
                      f"{steps} steps, {elapsed:.0f}s, "
                      f"{phase_prompt + phase_completion:,} tokens ({token_pct}% budget)")

            return result

        except Exception as e:
            elapsed = time.time() - start_ts
            self._total_elapsed += elapsed
            self._log(f"{phase_label} FAILED: {e}")
            traceback.print_exc()

            result = {
                "phase": phase_idx,
                "success": False,
                "steps": steps,
                "elapsed": elapsed,
                "error": str(e),
            }
            self._all_results.append(result)
            return result

    # ── 主入口 ──────────────────────────────────────────────

    async def run(self, task: str) -> dict:
        """自动分阶段执行完整任务。

        返回:
            {
                "success": bool,
                "total_steps": int,
                "phases_completed": int,
                "total_tokens": {"prompt": int, "completion": int, "total": int},
                "total_elapsed": float,
                "final_result": str,
                "phases": [{"phase": int, "success": bool, ...}, ...],
            }
        """
        from browser_use.browser.session import BrowserSession as Browser

        self._log(f"AutoPhaseRunner: task={len(task)} chars, "
                  f"max_steps_per_phase={self.max_steps_per_phase}, "
                  f"max_tokens_per_phase={self.max_tokens_per_phase:,}, "
                  f"max_phases={self.max_phases}")

        # 创建共享 BrowserSession
        self._browser = Browser(
            cdp_url=self.cdp_url,
            keep_alive=True,  # 关键：防止 agent cleanup 杀掉连接
            viewport=self.viewport,
            device_scale_factor=1.0,
        )

        overall_start = time.time()
        overall_success = False
        final_result = ""

        try:
            for phase_idx in range(self.max_phases):
                phase_task = self._build_continuation_prompt(
                    task, phase_idx,
                    self._phase_summaries[-1] if self._phase_summaries else ""
                )

                result = await self._run_phase(phase_task, phase_idx)

                # 提取摘要供下一阶段使用
                history = result.get("history")
                if history:
                    summary = self._extract_phase_summary(history)
                    if summary:
                        self._phase_summaries.append(summary)

                # 检查是否完成
                if result.get("success"):
                    overall_success = True
                    final_result = result.get("final", "")
                    self._log(f"✅ Task completed in {phase_idx + 1} phases!")
                    break

                if result.get("error"):
                    self._log(f"⚠️  Phase failed with error, stopping")
                    break

                # 如果一步都没走，卡死了
                if result.get("steps", 0) <= 1 and phase_idx > 0:
                    self._log(f"⚠️  Phase only took {result.get('steps')} step(s), likely stuck")
                    if phase_idx >= 1:  # 连续两次卡死就放弃
                        break

            else:
                self._log(f"⏰ Reached max phases ({self.max_phases}) without completing")

        finally:
            try:
                await self._browser.close()
            except Exception:
                pass

        total_elapsed = time.time() - overall_start

        return {
            "success": overall_success,
            "total_steps": self._total_steps,
            "phases_completed": len(self._all_results),
            "total_tokens": dict(self._token_usage),
            "total_elapsed": total_elapsed,
            "final_result": final_result,
            "phases": [
                {k: v for k, v in p.items() if k != "history"}
                for p in self._all_results
            ],
        }

    # ── 便捷启动 ⚡ ──────────────────────────────────────────

    @staticmethod
    async def quick_run(
        task: str,
        cdp_url: str,
        llm_config: dict | None = None,
        **kwargs,
    ) -> dict:
        """一行命令启动全自动分阶段执行。

        用法::

            result = await AutoPhaseRunner.quick_run(
                task="Register on StockX...",
                cdp_url="ws://127.0.0.1:63779/devtools/browser/...",
                llm_config={
                    "model": "bu-30b",
                    "base_url": "http://127.0.0.1:23434/v1",
                    "api_key": "not-needed",
                    "temperature": 0.1,
                    "max_completion_tokens": 8192,
                },
                max_steps_per_phase=18,
                verbose=True,
            )
        """
        default_llm = {
            "model": "bu-30b",
            "base_url": "http://127.0.0.1:23434/v1",
            "api_key": "not-needed",
            "temperature": 0.1,
            "max_completion_tokens": 8192,
        }
        runner = AutoPhaseRunner(
            llm_config=llm_config or default_llm,
            cdp_url=cdp_url,
            **kwargs,
        )
        return await runner.run(task)
