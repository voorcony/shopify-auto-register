"""
ConversationEngine — 对话引擎
=============================
控制论定位：系统的「输入传感器 + 前馈控制器」。

职责：
  1. 理解用户意图（注册 / 查询 / 配置 / 闲聊 / 执行）
  2. 从对话和历史中抽取结构化信息（profile_id、邮箱、密码 …）
  3. 检测缺失字段并主动追问
  4. 信息齐全后调用 prompt_builder 生成 phases，等待用户确认
  5. 输出统一的 ConversationResponse 给上层（CLI / Web UI / 飞书）

用法::

    from lib.data_service import SQLiteDataService
    from lib.conversation_engine import ConversationEngine

    ds = SQLiteDataService()
    engine = ConversationEngine(ds)
    resp = await engine.process("default_user", "帮我用 k1cl8tvk 注册 Cursor")
    # resp.action ∈ {"ask", "confirm", "execute", "chat"}

DeepSeek 调用复用 lib.prompt_builder._call_deepseek，
API Key 从 lib.config.load_feishu_secrets() 加载。
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# 确保可以 import lib.*
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.data_service import AbstractDataService  # noqa: E402

# ── 任务类型 → 必需字段（首版固定映射，后续可放进 task_templates 表）──
_TASK_REQUIRED_FIELDS: dict[str, list[str]] = {
    "account_registration": ["email", "shopify_password", "first_name", "last_name"],
    "shopify_setup":        ["email", "shopify_password", "store_name"],
    "product_import":       ["email", "shopify_password"],
    "generic_browser":      [],  # 通用浏览器任务无强制字段
}

# ── 缺失字段 → 给用户看的中文提示 ──
_FIELD_PROMPTS: dict[str, str] = {
    "email":            "邮箱地址",
    "shopify_password": "Shopify 账号密码",
    "first_name":       "名（First Name）",
    "last_name":        "姓（Last Name）",
    "phone":            "手机号",
    "address":          "邮寄地址",
    "city":             "城市",
    "state":            "州 / 省",
    "zip":              "邮编",
    "store_name":       "店铺名",
    "ssn":              "SSN（社会安全号）",
}


# =============================================================
# 响应对象
# =============================================================

@dataclass
class ConversationResponse:
    """对话引擎统一输出。

    action 取值：
      - "ask"     : 信息缺失，需要追问用户（reply 是问题，missing_fields 列出缺什么）
      - "confirm" : phases 已生成，等待用户说"开始/确认/执行"
      - "execute" : 用户已确认，可立即调用 task_runner（phases 与 profile_id 必填）
      - "chat"    : 闲聊 / 信息查询 / 引导，无需生成 phases
    """
    action: str
    reply: str
    intent: Optional[str] = None
    profile_id: Optional[str] = None
    missing_fields: list[str] = field(default_factory=list)
    extracted: dict = field(default_factory=dict)
    phases: list[dict] = field(default_factory=list)
    task_id: Optional[str] = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# =============================================================
# ConversationEngine
# =============================================================

class ConversationEngine:
    """对话主控器：意图分析 → 信息抽取 → 缺失追问 → phases 生成。"""

    DEFAULT_SESSION = "main"

    def __init__(
        self,
        data_service: AbstractDataService,
        adspower_profiles: Optional[list[str]] = None,
    ) -> None:
        self.ds = data_service
        # 可用的 AdsPower profiles —— 由外部注入；未注入则空列表
        self.adspower_profiles = adspower_profiles or []
        # DeepSeek API key 延迟加载
        self._api_key: Optional[str] = None

    # ── DeepSeek API key 懒加载 ─────────────────────────────

    def _get_api_key(self) -> str:
        if self._api_key:
            return self._api_key
        from lib.config import load_feishu_secrets
        cfg = load_feishu_secrets()
        key = cfg.get("deepseek", {}).get("api_key", "")
        if not key:
            raise RuntimeError("DeepSeek API key 未配置（飞书 81b55a 表）")
        self._api_key = key
        return key

    # ── 系统 prompt 构造 ─────────────────────────────────────

    def _build_intent_prompt(
        self,
        user_data: dict,
        recent_history: list[dict],
    ) -> str:
        """构造 DeepSeek 意图分析的 system prompt。

        包含：
          - 可用的 AdsPower profile 列表
          - 用户已有的字段（脱敏后）
          - 输出 JSON schema
        """
        # 脱敏：敏感字段只展示是否有值
        safe_user_data = {}
        for k, v in user_data.items():
            if not v:
                continue
            kl = k.lower()
            if any(s in kl for s in ("password", "ssn", "secret", "token", "card")):
                safe_user_data[k] = "***(已配置)"
            else:
                safe_user_data[k] = v

        profiles_str = (
            ", ".join(self.adspower_profiles)
            if self.adspower_profiles
            else "(未提供，请用户指定)"
        )

        history_brief = ""
        if recent_history:
            tail = recent_history[-6:]  # 只取最近 6 条
            lines = []
            for m in tail:
                role = m.get("role", "user")
                content = (m.get("content") or "")[:120]
                lines.append(f"- {role}: {content}")
            history_brief = "\n".join(lines)

        return f"""你是一个浏览器自动化系统的意图分析助手。任务：将用户的自然语言请求转换为结构化 JSON。

# 可用的 AdsPower 浏览器配置（profile_id）
{profiles_str}

# 用户已有的资料（已脱敏）
{json.dumps(safe_user_data, ensure_ascii=False, indent=2) if safe_user_data else "(空，新用户)"}

# 最近对话历史
{history_brief or "(无)"}

# 输出 JSON schema
{{
  "intent": "account_registration | shopify_setup | product_import | generic_browser | query | chat",
  "site": "目标站点，如 cursor.com / shopify.com，若无则空字符串",
  "profile_id": "用户指定的 AdsPower profile_id；未提到则空字符串",
  "task_description": "用一句中文总结用户要做的事，将用于后续 phase 生成",
  "extracted": {{
    "email": "...",
    "shopify_password": "...",
    "first_name": "...",
    "last_name": "...",
    "phone": "...",
    "store_name": "..."
  }},
  "is_confirmation": false,
  "reasoning": "你做出此判断的简要中文说明（一句）"
}}

# 规则
1. **intent**
   - account_registration: 用户要求注册某站点账号
   - shopify_setup: 已注册，要做 Shopify 后台配置
   - product_import: 导入商品到 Shopify
   - generic_browser: 其他浏览器自动化任务
   - query: 用户在查询历史/状态/已有信息
   - chat: 闲聊或无明确任务
2. **is_confirmation** = true 当且仅当用户消息是确认词（开始/确认/执行/go/yes/ok/好的/可以）。
3. **extracted** 中只放用户消息中**明确出现**的新信息，没有就留空字符串，不要瞎猜。
4. 若用户提到的 profile_id 不在可用列表里，仍按用户原样填入，由上层校验。
5. 输出**纯 JSON**，不要 markdown 代码块。
"""

    # ── DeepSeek 调用（异步包装）─────────────────────────────

    async def _call_llm(self, system: str, user_message: str) -> dict:
        """异步调用 DeepSeek，返回解析后的 JSON dict。"""
        from lib.prompt_builder import _call_deepseek  # 复用已实现的调用器

        api_key = self._get_api_key()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_message},
        ]

        # _call_deepseek 是同步 requests 调用，放到线程池避免阻塞 event loop
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(
            None, lambda: _call_deepseek(messages, api_key)
        )
        try:
            content = raw["choices"][0]["message"]["content"]
            return json.loads(content)
        except (KeyError, json.JSONDecodeError) as e:
            raise RuntimeError(f"DeepSeek 返回解析失败: {e}; raw={raw}")

    # ── 内部：缺失字段计算 ────────────────────────────────────

    def _compute_missing(self, intent: str, user_data: dict) -> list[str]:
        """根据 intent 查表，返回缺失字段名列表。"""
        required = _TASK_REQUIRED_FIELDS.get(intent, [])
        return [f for f in required if not user_data.get(f)]

    def _format_ask_prompt(self, missing: list[str]) -> str:
        """把缺失字段拼成对用户友好的中文追问。"""
        pretty = [_FIELD_PROMPTS.get(f, f) for f in missing]
        if len(pretty) == 1:
            return f"还差一个信息：**{pretty[0]}**，请告诉我。"
        listed = "、".join(pretty)
        return (
            f"还差以下信息我才能开始：{listed}。\n"
            f"你可以一次性告诉我，例如：邮箱 a@b.com，密码 Abc@123，姓名 John Doe。"
        )

    # ── 信息持久化 ────────────────────────────────────────────

    def _persist_extracted(self, user_id: str, extracted: dict) -> None:
        """把 LLM 抽取出来的非空字段写回 DataService。"""
        if not extracted:
            return
        for k, v in extracted.items():
            if v and isinstance(v, str) and v.strip():
                self.ds.set_user_data(user_id, k, v.strip())

    # ── 主入口 ────────────────────────────────────────────────

    async def process(
        self,
        user_id: str,
        user_message: str,
        session_id: Optional[str] = None,
    ) -> ConversationResponse:
        """处理一条用户输入，返回统一响应对象。

        流程：
          ① 追加 user 消息到对话历史
          ② 拉取已有 user_data + 最近 N 条历史
          ③ 调用 DeepSeek 做意图分析 + 信息抽取
          ④ 把抽取到的字段写回 DataService
          ⑤ 根据 intent 判断：
             - chat / query  → 直接返回闲聊回复
             - is_confirmation=true → 找最近 pending 任务 → 返回 execute
             - 信息缺失      → 返回 ask
             - 信息齐全      → 调 prompt_builder 生成 phases → 写 tasks → 返回 confirm
        """
        session_id = session_id or self.DEFAULT_SESSION

        # ① 写入用户消息
        self.ds.add_message(user_id, session_id, "user", user_message)

        # ② 读已有状态
        user_data = self.ds.get_all_user_data(user_id)
        history = self.ds.get_recent_messages(user_id, session_id, limit=10)

        # ③ DeepSeek 意图分析
        system_prompt = self._build_intent_prompt(user_data, history)
        try:
            parsed = await self._call_llm(system_prompt, user_message)
        except Exception as e:
            reply = f"抱歉，意图分析出错：{e}。请稍后重试或换种说法。"
            self.ds.add_message(user_id, session_id, "assistant", reply)
            return ConversationResponse(action="chat", reply=reply, raw={"error": str(e)})

        intent      = (parsed.get("intent") or "chat").strip()
        site        = (parsed.get("site") or "").strip()
        profile_id  = (parsed.get("profile_id") or "").strip()
        task_desc   = (parsed.get("task_description") or user_message).strip()
        extracted   = parsed.get("extracted") or {}
        is_confirm  = bool(parsed.get("is_confirmation", False))

        # ④ 抽取信息持久化
        self._persist_extracted(user_id, extracted)
        # 重新读一次，确保最新
        user_data = self.ds.get_all_user_data(user_id)

        # ⑤a — 用户在确认上一次的任务
        if is_confirm:
            pending = self._find_pending_task(user_id)
            if pending:
                self.ds.update_task_status(pending["id"], "running")
                reply = f"好的，开始执行任务（{len(pending['phases'])} 个阶段）。"
                self.ds.add_message(user_id, session_id, "assistant", reply)
                return ConversationResponse(
                    action="execute",
                    reply=reply,
                    intent=pending.get("task_type"),
                    profile_id=pending.get("ads_profile"),
                    phases=pending.get("phases", []),
                    task_id=pending["id"],
                    raw=parsed,
                )
            # 没有 pending 任务，退化为 chat
            reply = "目前没有待执行的任务，请先告诉我你要做什么。"
            self.ds.add_message(user_id, session_id, "assistant", reply)
            return ConversationResponse(action="chat", reply=reply, raw=parsed)

        # ⑤b — 闲聊 / 查询
        if intent in ("chat", "query"):
            if intent == "query":
                reply = self._reply_query(user_data)
            else:
                reply = "收到。如果你要执行任务，告诉我目标（例如「在 cursor.com 注册」）和 AdsPower profile_id。"
            self.ds.add_message(user_id, session_id, "assistant", reply)
            return ConversationResponse(
                action="chat", reply=reply, intent=intent,
                extracted=extracted, raw=parsed,
            )

        # ⑤c — 任务类意图：先检查必需字段
        missing = self._compute_missing(intent, user_data)
        if missing:
            reply = self._format_ask_prompt(missing)
            self.ds.add_message(
                user_id, session_id, "assistant", reply,
                metadata={"missing_fields": missing, "intent": intent},
            )
            return ConversationResponse(
                action="ask", reply=reply, intent=intent,
                profile_id=profile_id, missing_fields=missing,
                extracted=extracted, raw=parsed,
            )

        # ⑤d — profile_id 未指定时也要追问
        if not profile_id and intent != "generic_browser":
            reply = (
                "请告诉我要使用的 AdsPower 配置 ID（profile_id）。"
                + (f"\n可选：{', '.join(self.adspower_profiles)}" if self.adspower_profiles else "")
            )
            self.ds.add_message(user_id, session_id, "assistant", reply,
                                metadata={"missing_fields": ["profile_id"], "intent": intent})
            return ConversationResponse(
                action="ask", reply=reply, intent=intent,
                missing_fields=["profile_id"], extracted=extracted, raw=parsed,
            )

        # ⑤e — 信息齐全：生成 phases
        try:
            phases = await self._build_phases(task_desc, site)
        except Exception as e:
            reply = f"生成任务步骤失败：{e}"
            self.ds.add_message(user_id, session_id, "assistant", reply)
            return ConversationResponse(action="chat", reply=reply, raw={"error": str(e)})

        task_id = self.ds.create_task(
            user_id=user_id,
            task_type=intent,
            description=task_desc,
            phases=phases,
            ads_profile=profile_id,
        )

        phase_names = [p.get("name", f"step-{i}") for i, p in enumerate(phases)]
        reply = (
            f"已规划完毕，共 {len(phases)} 个阶段：{phase_names}。\n"
            f"目标：{task_desc}\n"
            f"AdsPower：{profile_id or '(未指定)'}\n"
            f"回复「开始 / 确认 / 执行」即可启动。"
        )
        self.ds.add_message(
            user_id, session_id, "assistant", reply,
            metadata={"task_id": task_id, "phase_names": phase_names},
        )

        return ConversationResponse(
            action="confirm",
            reply=reply,
            intent=intent,
            profile_id=profile_id,
            phases=phases,
            task_id=task_id,
            extracted=extracted,
            raw=parsed,
        )

    # ── 辅助：调用 prompt_builder 生成 phases ────────────────

    async def _build_phases(self, task_description: str, site: str) -> list[dict]:
        """异步调 build_phases（同步函数放线程池）。site 非空时拼到描述里。"""
        from lib.prompt_builder import build_phases

        full_desc = task_description
        if site and site not in task_description:
            full_desc = f"{task_description}（目标站点：{site}）"

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, build_phases, full_desc)

    # ── 辅助：找最近的 pending/confirm 任务 ──────────────────

    def _find_pending_task(self, user_id: str) -> Optional[dict]:
        """找最近一条 status='pending' 的任务作为本次确认对象。"""
        tasks = self.ds.get_user_tasks(user_id, limit=5)
        for t in tasks:
            if t.get("status") == "pending":
                return t
        return None

    # ── 辅助：query 意图的简单回复 ───────────────────────────

    def _reply_query(self, user_data: dict) -> str:
        if not user_data:
            return "目前还没有保存任何资料，告诉我你的邮箱、密码、姓名即可开始建档。"
        lines = ["你已保存的资料："]
        for k, v in user_data.items():
            kl = k.lower()
            if any(s in kl for s in ("password", "ssn", "secret", "token", "card")):
                lines.append(f"  - {k}: ***(已配置)")
            else:
                lines.append(f"  - {k}: {v}")
        return "\n".join(lines)


# =============================================================
# CLI 调试入口
# =============================================================

async def _cli_main():
    """简单交互测试：python3 -m lib.conversation_engine"""
    from lib.data_service import SQLiteDataService

    ds = SQLiteDataService()
    engine = ConversationEngine(ds)
    user_id = "default_user"
    print("ConversationEngine 调试模式（输入 'exit' 退出）")
    while True:
        try:
            msg = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not msg or msg.lower() in ("exit", "quit"):
            break
        resp = await engine.process(user_id, msg)
        print(f"\n[{resp.action}] {resp.reply}")
        if resp.missing_fields:
            print(f"  缺失字段: {resp.missing_fields}")
        if resp.phases:
            print(f"  phases: {[p.get('name') for p in resp.phases]}")


if __name__ == "__main__":
    asyncio.run(_cli_main())
