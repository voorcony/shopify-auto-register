"""
用户交互层 — Agent 阻塞时通过文件协议向人类求助，DeepSeek 翻译自然语言为结构化指令
=================================================================================

设计原则（控制论·综合集成 + 自适应控制）:
  ① 文件协议解耦：Agent / Web UI / CLI 用同一对文件通信，互不依赖
  ② 用户说"人话"，DeepSeek 翻译为机器可执行 action
  ③ 异步阻塞等待：保留 asyncio 主循环可用（不能简单 time.sleep）

协议文件:
  /tmp/.run_task_request   — Agent 写入；UI 读取并展示给用户
  /tmp/.run_task_response  — 用户写入（自然语言）；Agent 读取并翻译

action 字典（DeepSeek 输出的结构化结果）:
  - provide_data    : 用户提供了 Agent 需要的具体数据（验证码、邮箱、地址等）
  - skip            : 跳过当前 phase 进入下一个
  - abort           : 中止整个任务
  - new_instruction : 用户给出新指令，覆盖原 next_goal
  - retry           : 重试当前 step
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

# ── 协议文件路径 ────────────────────────────────────────────
REQUEST_FILE = Path("/tmp/.run_task_request")
RESPONSE_FILE = Path("/tmp/.run_task_response")

# ── 轮询参数 ────────────────────────────────────────────────
POLL_INTERVAL = 2          # 每 2 秒检查一次响应文件
DEFAULT_TIMEOUT = 300      # 默认 5 分钟超时


# ── 请求写入 ────────────────────────────────────────────────

def write_request(
    *,
    task_id: str,
    request_type: str,
    message: str,
    url: str = "",
    screenshot: str = "",
    options: Optional[list[str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    context: Optional[dict] = None,
) -> dict:
    """写入用户请求文件。

    Args:
        task_id: 任务唯一标识（一般为 profile_id 或自定义）
        request_type: 请求类型 — captcha_timeout / agent_needs_help / stuck / confirm_action
        message: 给用户看的提示文案（中文）
        url: 当前页 URL（辅助用户判断）
        screenshot: 截图路径
        options: 候选动作列表（可视为选项按钮，DeepSeek 翻译时参考）
        timeout: 等待超时（秒）
        context: 当前任务的额外上下文（task_description、phase_name 等）

    Returns:
        请求字典本体（已写入文件）
    """
    REQUEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 清掉旧响应，避免误读上次回复
    try:
        RESPONSE_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    request = {
        "ts": time.time(),
        "task_id": task_id,
        "request_type": request_type,
        "message": message,
        "url": url,
        "screenshot": screenshot,
        "options": options or ["continue", "skip", "abort"],
        "timeout": timeout,
        "context": context or {},
    }
    REQUEST_FILE.write_text(json.dumps(request, indent=2, ensure_ascii=False))
    return request


def check_response() -> Optional[dict]:
    """非阻塞读取用户响应；读到后立即消费（删除文件）。

    返回值约定：
      - 已结构化（含 "action"）→ 原样返回
      - 自然语言（含 "text" 字段）→ 返回 {"text": "...", "raw": True}，调用方需翻译
      - 文件不存在或解析失败 → None
    """
    if not RESPONSE_FILE.exists():
        return None

    try:
        raw = RESPONSE_FILE.read_text().strip()
    except Exception:
        return None

    if not raw:
        return None

    # 消费响应（先删后解析，避免重复消费）
    try:
        RESPONSE_FILE.unlink()
    except Exception:
        pass

    # 尝试解析 JSON
    try:
        resp = json.loads(raw)
        if isinstance(resp, dict) and "action" in resp:
            return resp
        # JSON 但不含 action → 当自然语言处理
        if isinstance(resp, dict) and "text" in resp:
            return {"text": str(resp["text"]), "raw": True}
    except json.JSONDecodeError:
        pass

    # 纯文本回复 → 标记为待翻译
    return {"text": raw, "raw": True}


# ── DeepSeek 翻译层 ─────────────────────────────────────────

_TRANSLATE_SYSTEM_PROMPT = """你是一个自动化任务的「用户指令翻译官」。
浏览器自动化 Agent 在执行任务时遇到了阻碍，向人类求助。
人类用自然语言（中文/英文）回复，你需要把回复翻译为机器可执行的 JSON action。

输出格式严格为 JSON：
{
  "action": "provide_data | skip | abort | new_instruction | retry",
  "data": { ... 取决于 action 类型 ... },
  "reasoning": "你对用户意图的理解（中文，一句话）"
}

action 类型说明:
  - provide_data    : 用户提供了 Agent 需要的具体数据（验证码、邮箱、密码、地址、姓名等）
                      data 字段示例: {"verification_code": "243253", "email": "x@y.com"}
                      data key 用英文 snake_case，便于注入。
  - skip            : 用户表示「跳过」「不要管这步」「下一个」
                      data: {}
  - abort           : 用户表示「取消」「停止」「不做了」「算了」
                      data: {}
  - new_instruction : 用户给出新的操作指令（不是数据，而是指挥 Agent 怎么做）
                      data: {"instruction": "用户原话或翻译后的英文指令"}
  - retry           : 用户表示「再试一次」「重新来」
                      data: {}

判断规则:
  - 若用户回复中包含数字/字符串等明确的「值」→ provide_data
  - 若用户只说话不给数据 → new_instruction
  - 含「跳过、下一步、不管、忽略」→ skip
  - 含「取消、停止、不要了、算了、abort、stop」→ abort
  - 含「重试、再来、retry」→ retry
  - 含糊不清时优先 new_instruction，把原话放入 instruction"""


def _build_translate_prompt(
    user_reply: str,
    request_context: dict,
) -> list[dict]:
    """构造 DeepSeek messages — 系统提示 + 用户回复 + 当前任务上下文。"""
    ctx_lines = [
        f"请求类型: {request_context.get('request_type', 'unknown')}",
        f"Agent 提示: {request_context.get('message', '')}",
    ]
    url = request_context.get("url", "")
    if url:
        ctx_lines.append(f"当前 URL: {url}")
    options = request_context.get("options") or []
    if options:
        ctx_lines.append(f"候选动作: {', '.join(options)}")
    extra_ctx = request_context.get("context") or {}
    if extra_ctx:
        for k, v in extra_ctx.items():
            if v:
                ctx_lines.append(f"{k}: {v}")

    user_content = (
        "## 当前任务上下文\n"
        + "\n".join(ctx_lines)
        + "\n\n## 用户回复\n"
        + user_reply
        + "\n\n请把用户回复翻译为 JSON action。"
    )

    return [
        {"role": "system", "content": _TRANSLATE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def translate_user_reply(user_reply: str, request_context: dict) -> dict:
    """调用 DeepSeek 把用户自然语言翻译为结构化 action。

    失败兜底（DeepSeek 不可用）：用关键词规则做最简推断，避免完全阻塞。
    """
    user_reply = (user_reply or "").strip()
    if not user_reply:
        return {"action": "abort", "data": {}, "reasoning": "用户回复为空"}

    # ── 快速规则兜底（关键词命中即返回，省一次 API 调用）──
    quick = _quick_match(user_reply)
    if quick:
        return quick

    # ── 调用 DeepSeek ──
    try:
        from lib import config as _config
        cfg = _config.load_feishu_secrets()
        api_key = cfg.get("deepseek", {}).get("api_key", "")
        if not api_key:
            return _fallback_translate(user_reply, "DeepSeek API key 未配置")

        messages = _build_translate_prompt(user_reply, request_context)

        # 直接复用 prompt_builder 的 DeepSeek 调用（带重试 + 401/402 处理）
        from lib.prompt_builder import _call_deepseek
        raw = _call_deepseek(messages, api_key, model="deepseek-chat")
        content = raw["choices"][0]["message"]["content"]
        result = json.loads(content)

        # 标准化
        action = result.get("action", "new_instruction")
        if action not in {"provide_data", "skip", "abort", "new_instruction", "retry"}:
            action = "new_instruction"
        return {
            "action": action,
            "data": result.get("data", {}) or {},
            "reasoning": result.get("reasoning", ""),
        }
    except Exception as e:
        return _fallback_translate(user_reply, f"DeepSeek 翻译失败: {e}")


def _quick_match(reply: str) -> Optional[dict]:
    """关键词快速匹配，命中即返回 action，避免不必要的 API 调用。"""
    r = reply.strip().lower()

    # 显式 abort
    if r in {"abort", "stop", "cancel", "取消", "停止", "中止", "算了", "不做了", "不要了"}:
        return {"action": "abort", "data": {}, "reasoning": "用户明确取消"}

    # 显式 skip
    if r in {"skip", "next", "跳过", "下一个", "下一步", "忽略"}:
        return {"action": "skip", "data": {}, "reasoning": "用户跳过当前步骤"}

    # 显式 retry
    if r in {"retry", "again", "重试", "再来", "再试一次"}:
        return {"action": "retry", "data": {}, "reasoning": "用户重试"}

    return None


def _fallback_translate(reply: str, reason: str) -> dict:
    """DeepSeek 不可用时的兜底翻译。

    简化规则：
      - 纯数字 6/4 位 → 当作 verification_code
      - 含 @ 当作 email
      - 其他 → new_instruction，把原话作为指令
    """
    reply = reply.strip()
    # 纯数字（4-8 位）→ 大概率是验证码
    if reply.isdigit() and 4 <= len(reply) <= 8:
        return {
            "action": "provide_data",
            "data": {"verification_code": reply},
            "reasoning": f"兜底规则：当作验证码（{reason}）",
        }
    # 含 @ → email
    if "@" in reply and "." in reply and len(reply) < 100:
        return {
            "action": "provide_data",
            "data": {"email": reply},
            "reasoning": f"兜底规则：当作邮箱（{reason}）",
        }
    # 其他 → 当作新指令
    return {
        "action": "new_instruction",
        "data": {"instruction": reply},
        "reasoning": f"兜底规则：当作自由指令（{reason}）",
    }


# ── 异步阻塞等待 ────────────────────────────────────────────

async def wait_for_user(
    *,
    task_id: str,
    request_type: str,
    message: str,
    url: str = "",
    screenshot: str = "",
    options: Optional[list[str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    context: Optional[dict] = None,
) -> dict:
    """阻塞等待用户响应；超时返回 timeout action。

    Returns:
        {
          "action": "provide_data | skip | abort | new_instruction | retry | timeout",
          "data": { ... },
          "reasoning": "..."
        }
    """
    request = write_request(
        task_id=task_id,
        request_type=request_type,
        message=message,
        url=url,
        screenshot=screenshot,
        options=options,
        timeout=timeout,
        context=context,
    )

    start = time.time()
    while time.time() - start < timeout:
        resp = check_response()
        if resp is not None:
            # 已结构化：直接返回
            if "action" in resp and not resp.get("raw"):
                return resp
            # 自然语言：调 DeepSeek 翻译
            translated = translate_user_reply(resp.get("text", ""), request)
            return translated

        await asyncio.sleep(POLL_INTERVAL)

    # 超时：清掉请求文件，避免遗留
    try:
        REQUEST_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    return {"action": "timeout", "data": {}, "reasoning": f"等待 {timeout}s 无响应"}
