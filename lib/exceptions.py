"""
自动化任务专用异常 — 用于 AutoPhaseRunner / task_runner 之间的协调通信
"""
from __future__ import annotations


class UserAbortException(Exception):
    """用户主动中止任务。

    触发场景：
      - 用户在 /tmp/.run_task_response 中返回 {"action": "abort"}
      - DeepSeek 翻译用户自然语言后产出 abort 指令
    捕获位置：
      - task_runner.run_with_retry — 提前终止重试循环，不再尝试下一次
    """
    pass


class CaptchaTimeoutException(Exception):
    """CAPTCHA 等待超时。

    触发场景：
      - captcha_detector 等待打码插件超过 wait_timeout 秒未解决
    捕获位置：
      - AutoPhaseRunner._on_agent_step — 在回调内部转化为用户请求
      - 若用户也未响应或选择 abort，则向上抛
    """
    pass
