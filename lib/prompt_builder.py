"""
AI 辅助任务拆解器 — 调用 DeepSeek API 将自然语言任务拆解为结构化 phases。

用法:
  # 库调用
  from lib.prompt_builder import build_phases, PhaseBuilder
  phases = build_phases("帮我注册一个新的 Shopify 店铺")

  # CLI
  python3 -m lib.prompt_builder "帮我注册 Shopify"
  python3 -m lib.prompt_builder --dry-run "测试任务"
  python3 -m lib.prompt_builder --output phases/my_task.json "..."

输出格式与 lib/task_runner.py 的 run_agent(cdp_url, phases) 完全兼容。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── 路径常量 ────────────────────────────────────────────────
_HOME = Path.home()
_CACHE_DIR = _HOME / ".shopify" / "prompt_cache"
_PHASES_DIR = _HOME / ".shopify" / "phases" / "generated"
_CACHE_INDEX_FILE = _CACHE_DIR / "index.json"
_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 天

# ── 敏感词扫描规则 ─────────────────────────────────────────

# 🔴 高危：任一匹配 → 立即拒绝
DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"(信用卡|credit\s*card|CVV|cvv)", "支付信息"),
    (r"(密码|password|passwd|secret|token|api\s*key)", "凭证操作"),
    (r"(删除|清空|delete|drop\s+table|rm\s+-rf|format|wipe|shutdown)", "破坏性操作"),
    (r"(转账|transfer|汇款|wire)", "资金操作"),
    (r"(SQL\s*注入|XSS|反弹\s*shell|reverse\s*shell|后门|backdoor)", "攻击行为"),
    (r"(窃取|盗号|hack|crack|破解)", "隐私侵犯"),
]

# 🟡 警告：匹配但仅标记
WARNING_PATTERNS: list[tuple[str, str]] = [
    (r"(批量|bulk|所有|全部|all\b|every\b)", "批量操作"),
    (r"(curl|wget|download)", "网络请求"),
    (r"(关店|关闭\s*店铺|删除\s*店铺|close\s*store)", "不可逆店铺操作"),
]

# ── System Prompt ───────────────────────────────────────────

SYSTEM_PROMPT = """You are a task decomposition expert. Your job is to break down a user's high-level task description into a sequence of actionable phases.

Each phase is a self-contained instruction for an AI browser agent that has a 14,000 token context budget per phase. That means:

1. Each phase's "task" field should be a COMPLETE, SELF-CONTAINED prompt — the agent sees ONLY this phase's task text, nothing more.

2. Keep each phase's task UNDER 800 characters. If a task needs more detail, break it into more phases rather than longer text.

3. Provide ENOUGH DETAIL in each phase so the agent knows exactly what to do: target URLs, specific buttons to click, data to enter, and what "done" looks like.

4. Each phase must have a clear success criterion — the agent should know when to stop.

5. Number of phases: 3-6 for most tasks. Only use more if unavoidable.

OUTPUT FORMAT (JSON only, no markdown):
{
  "phases": [
    {
      "name": "short_kebab_case_name",
      "task": "Detailed English instruction for this phase...",
      "max_steps": 25
    }
  ]
}

max_steps: typically 15-30. Use 15 for simple navigation, 25 for form-filling, 30 for complex multi-step flows.

---

EXAMPLE:

User task: "Register a Shopify account, install Syncee app, import 5 fashion products"

Output:
{
  "phases": [
    {
      "name": "register-account",
      "task": "Register a new Shopify account.\\n\\n1. Go to https://www.shopify.com\\n2. Click 'Start free trial'\\n3. Enter email: {email}, password: {password}\\n4. Fill name: {first} {last}\\n5. On plan selection, find and click 'Skip' or free trial — NEVER enter credit card info\\n6. Navigate to admin dashboard\\n\\nCRITICAL: If you reach admin dashboard, report SUCCESS immediately.",
      "max_steps": 30
    },
    {
      "name": "complete-setup",
      "task": "Complete Shopify onboarding wizard.\\n\\n1. You should be at admin dashboard\\n2. If there's an onboarding checklist, complete it\\n3. Set country: United States, timezone: America/Chicago, currency: USD\\n4. Set store name: {store_name}\\n5. Skip any paid upgrades\\n\\nIf already past onboarding (main sidebar visible), report SUCCESS immediately.",
      "max_steps": 20
    },
    {
      "name": "install-syncee",
      "task": "Install Syncee AI Dropship app.\\n\\n1. From Shopify admin, go to: https://apps.shopify.com/syncee-1\\n2. Click 'Add app' or 'Install'\\n3. Confirm installation\\n4. Wait for Syncee dashboard to load\\n5. Report Syncee dashboard URL\\n\\nIf already installed, report SUCCESS.",
      "max_steps": 15
    },
    {
      "name": "import-products",
      "task": "Import products using Syncee.\\n\\n1. You should be on Syncee dashboard\\n2. Find product import/search section\\n3. Search for 'fashion accessories'\\n4. Click 'Push to store' or 'Import' on at least 5 products\\n5. Navigate to Shopify Products page to verify\\n\\nOne product imported = success minimum.",
      "max_steps": 25
    }
  ]
}"""


# ── 文本标准化 + 缓存键 ──────────────────────────────────────

def _normalize(text: str) -> str:
    """标准化用户输入以提高缓存命中率。"""
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    # 中文标点统一
    text = re.sub(r"[，。！？、；：""''【】]", ",", text)
    text = re.sub(r"[,!?;:]+", ",", text)
    # 去掉语气词前缀（\b 对中文无效，改为行首匹配）
    text = re.sub(r"^(请|能不能|可以|麻烦|please|could you|can you)\s*", "", text)
    # 二次：去掉"请帮我"中的"帮我"（"帮我"本身太语义化，只在前面有"请"等词时才会连带移除）
    text = text.strip(",. ")
    return text


def _cache_key(description: str) -> str:
    """生成 MD5 缓存键。"""
    return hashlib.md5(_normalize(description).encode()).hexdigest()


# ── 缓存读写 ────────────────────────────────────────────────

def _ensure_cache_dir() -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _load_cache_index() -> dict:
    _ensure_cache_dir()
    if _CACHE_INDEX_FILE.exists():
        try:
            with open(_CACHE_INDEX_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def _save_cache_index(index: dict) -> None:
    _ensure_cache_dir()
    with open(_CACHE_INDEX_FILE, "w") as f:
        json.dump(index, f, indent=2)


def _cache_get(key: str) -> list[dict] | None:
    """读取缓存，若过期返回 None。"""
    index = _load_cache_index()
    entry = index.get(key)
    if not entry:
        return None
    # 检查 TTL
    ts = entry.get("ts", 0)
    if time.time() - ts > _CACHE_TTL_SECONDS:
        # 过期，清理
        cache_file = _CACHE_DIR / f"{key}.json"
        if cache_file.exists():
            cache_file.unlink()
        del index[key]
        _save_cache_index(index)
        return None
    cache_file = _CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def _cache_set(key: str, phases: list[dict], description: str) -> None:
    """写入缓存。"""
    _ensure_cache_dir()
    index = _load_cache_index()
    index[key] = {"ts": time.time(), "description": description[:200]}
    _save_cache_index(index)
    cache_file = _CACHE_DIR / f"{key}.json"
    with open(cache_file, "w") as f:
        json.dump(phases, f, indent=2, ensure_ascii=False)


# ── 安全扫描 ────────────────────────────────────────────────

def _scan_safety(description: str) -> tuple[bool, list[str], list[str]]:
    """
    返回:
        (blocked: bool, danger_reasons: list[str], warnings: list[str])
    """
    dangers = [reason for pattern, reason in DANGEROUS_PATTERNS
               if re.search(pattern, description, re.IGNORECASE)]
    warnings = [reason for pattern, reason in WARNING_PATTERNS
                if re.search(pattern, description, re.IGNORECASE)]
    return (len(dangers) > 0, dangers, warnings)


# ── DeepSeek API 调用 + 重试 ─────────────────────────────────

def _call_deepseek(messages: list[dict], api_key: str, model: str = "deepseek-chat") -> dict:
    """调用 DeepSeek API，带重试和降级。"""
    import requests

    MAX_RETRIES = 2
    last_error = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.3,
                    "max_tokens": 4096,
                    "response_format": {"type": "json_object"},
                },
                timeout=30,
            )

            # 4xx 不重试
            if response.status_code == 401:
                raise PermissionError("API Key 无效，请检查 deepseek.api_key 配置")
            if response.status_code == 402:
                raise RuntimeError("DeepSeek 账户余额不足")
            if response.status_code == 429:
                if attempt < MAX_RETRIES:
                    wait = 5 * (2 ** attempt)
                    print(f"   ⏳ 频率限制，等待 {wait}s 后重试...", flush=True)
                    time.sleep(wait)
                    continue
                raise RuntimeError("请求频率过高，请等待后重试")

            # 5xx 重试
            if response.status_code >= 500:
                raise ConnectionError(f"DeepSeek 服务器错误 (HTTP {response.status_code})")

            response.raise_for_status()
            return response.json()

        except requests.Timeout:
            last_error = "请求超时"
            if attempt < MAX_RETRIES:
                print(f"   ⏳ 超时，重试中... ({attempt + 1}/{MAX_RETRIES})", flush=True)
                time.sleep(1.5 ** attempt)
                continue

        except requests.ConnectionError as e:
            last_error = f"网络连接失败: {e}"
            if attempt < MAX_RETRIES:
                print(f"   ⏳ 网络错误，重试中... ({attempt + 1}/{MAX_RETRIES})", flush=True)
                time.sleep(1.5 ** attempt)
                continue

        except (PermissionError, RuntimeError) as e:
            # 不重试的错误
            raise

    raise RuntimeError(f"DeepSeek API 调用失败 (已重试 {MAX_RETRIES} 次): {last_error}")


# ── JSON 解析 + 校验 ────────────────────────────────────────

def _parse_and_validate(raw_response: dict, messages: list[dict], api_key: str,
                        default_max_steps: int = 25) -> list[dict]:
    """解析 JSON phases + 校验结构。解析失败则重试整个调用。"""
    for parse_attempt in range(2):  # 最多重试 1 次
        try:
            content = raw_response["choices"][0]["message"]["content"]
            data = json.loads(content)
            phases = data.get("phases", [])

            # 校验
            if not phases:
                raise ValueError("phases 不能为空")
            if not isinstance(phases, list):
                raise ValueError("phases 必须是数组")

            for i, p in enumerate(phases):
                if not isinstance(p, dict):
                    raise ValueError(f"phase[{i}] 必须是对象")
                if not isinstance(p.get("name"), str) or not p["name"].strip():
                    raise ValueError(f"phase[{i}] 缺少有效 name")
                if not isinstance(p.get("task"), str) or not p["task"].strip():
                    raise ValueError(f"phase[{i}] 缺少有效 task")
                if not isinstance(p.get("max_steps"), (int, float)):
                    p["max_steps"] = default_max_steps
                # 规范化 + 钳制 [5, 50]
                p["max_steps"] = int(p["max_steps"])
                p["max_steps"] = max(5, min(p["max_steps"], 50))

            return phases

        except (json.JSONDecodeError, ValueError, KeyError) as e:
            if parse_attempt == 0:
                print(f"   ⚠️  JSON 解析失败: {e}，重试调用...", flush=True)
                raw_response = _call_deepseek(messages, api_key)
            else:
                raise ValueError(f"DeepSeek 输出格式异常 (已重试): {e}")

    raise ValueError("DeepSeek 输出格式异常")


# ── 保存 phases ─────────────────────────────────────────────

def _make_slug(phases: list[dict]) -> str:
    """从 phases 中提取 slug。"""
    if phases:
        name = phases[0].get("name", "task")
        words = re.findall(r"[a-zA-Z]+", name.lower())
        if words:
            return "_".join(words[:4])[:40]
    return "generated"


def _save_phases(phases: list[dict]) -> Path:
    """保存生成的 phases 到 ~/.shopify/phases/generated/。"""
    _PHASES_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    slug = _make_slug(phases)
    filename = f"{timestamp}_{slug}.json"
    output_path = _PHASES_DIR / filename
    with open(output_path, "w") as f:
        json.dump(phases, f, indent=2, ensure_ascii=False)
    return output_path


# ── PhaseBuilder 类 ──────────────────────────────────────────

class PhaseBuilder:
    """任务拆解器。支持自定义 model、max_steps、temperature 等。

    用法:
        builder = PhaseBuilder(model="deepseek-chat", max_steps=25)
        phases = builder.build("帮我注册 Shopify")
    """

    def __init__(
        self,
        model: str = "deepseek-chat",
        max_steps: int = 25,
        temperature: float = 0.3,
        no_cache: bool = False,
    ):
        self.model = model
        self.max_steps = max_steps
        self.temperature = temperature
        self.no_cache = no_cache

        # 延迟加载 API key
        self._api_key: str | None = None

    def _get_api_key(self) -> str:
        if self._api_key is None:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from lib.config import load_feishu_secrets
            cfg = load_feishu_secrets()
            self._api_key = cfg.get("deepseek", {}).get("api_key", "")
            if not self._api_key:
                raise RuntimeError(
                    "DeepSeek API key 未配置，请检查飞书 81b55a 表中的 deepseek 条目"
                )
        return self._api_key

    def build(self, description: str) -> list[dict]:
        """将自然语言描述拆解为结构化 phases。

        Args:
            description: 用户任务描述

        Returns:
            [{"name": "...", "task": "...", "max_steps": N}, ...]

        Raises:
            ValueError: 安全扫描阻挡
            RuntimeError: API 调用失败
        """
        # 1. 安全扫描
        blocked, dangers, warnings = _scan_safety(description)
        if blocked:
            raise ValueError(
                f"🛑 安全扫描拦截！检测到高危词汇: {', '.join(dangers)}"
            )

        if warnings:
            print(f"   ⚠️  警告标记: {', '.join(warnings)}", flush=True)

        # 2. 缓存查询
        if not self.no_cache:
            key = _cache_key(description)
            cached = _cache_get(key)
            if cached:
                print(f"   ✅ 命中缓存 (key={key[:12]}...)", flush=True)
                return cached

        # 3. 构建消息 + 调用 API
        api_key = self._get_api_key()
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": description},
        ]
        print(f"   🤖 调用 DeepSeek API ({self.model})...", flush=True)
        t0 = time.time()
        raw = _call_deepseek(messages, api_key, model=self.model)
        elapsed = time.time() - t0
        print(f"   ✅ API 响应: {elapsed:.1f}s", flush=True)

        # 4. JSON 解析 + 校验
        phases = _parse_and_validate(raw, messages, api_key,
                                     default_max_steps=self.max_steps)
        print(f"   📋 生成 {len(phases)} 个阶段", flush=True)

        # 5. 写入缓存
        if not self.no_cache:
            key = _cache_key(description)
            _cache_set(key, phases, description)

        # 6. 自动保存
        saved_path = _save_phases(phases)
        print(f"   💾 保存到: {saved_path}", flush=True)

        return phases


# ── 便捷函数 ────────────────────────────────────────────────

def build_phases(
    description: str,
    model: str = "deepseek-chat",
    max_steps: int = 25,
    temperature: float = 0.3,
    no_cache: bool = False,
) -> list[dict]:
    """快速构建 phases 的便捷函数。

    Args:
        description: 用户任务描述
        model: DeepSeek 模型名
        max_steps: 默认 max_steps
        temperature: 生成温度
        no_cache: 是否跳过缓存

    Returns:
        [{"name": "...", "task": "...", "max_steps": N}, ...]
    """
    builder = PhaseBuilder(
        model=model,
        max_steps=max_steps,
        temperature=temperature,
        no_cache=no_cache,
    )
    return builder.build(description)


# ── CLI ─────────────────────────────────────────────────────

def _cli_main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python3 -m lib.prompt_builder",
        description="AI 辅助任务拆解器 — 将自然语言任务拆解为结构化 phases",
    )
    parser.add_argument(
        "description",
        nargs="?",
        help="任务描述（位置参数）",
    )
    parser.add_argument(
        "--file", "-f",
        help="从文件读取任务描述",
    )
    parser.add_argument(
        "--output", "-o",
        help="输出 JSON 路径（默认 stdout）",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=25,
        help="每个 phase 的默认 max_steps（默认 25）",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="跳过缓存，强制重新生成",
    )
    parser.add_argument(
        "--json-only",
        action="store_true",
        help="只输出 JSON（无日志）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只扫描不调用 API",
    )
    parser.add_argument(
        "--model",
        default="deepseek-chat",
        help="模型名（默认 deepseek-chat）",
    )

    args = parser.parse_args()

    # 获取描述
    if args.file:
        with open(args.file) as f:
            description = f.read().strip()
    elif args.description:
        description = args.description
    else:
        parser.print_help()
        print("\n❌ 请提供任务描述（位置参数或 --file）")
        sys.exit(1)

    if not description:
        print("❌ 任务描述为空")
        sys.exit(1)

    # Dry-run: 只扫描
    if args.dry_run:
        blocked, dangers, warnings = _scan_safety(description)
        if not args.json_only:
            print(f"📝 任务描述: {description[:100]}...")
            print(f"🔍 安全扫描结果:")
            if blocked:
                print(f"   🛑 拦截！高危词: {', '.join(dangers)}")
            else:
                print(f"   ✅ 未检测到高危词汇")
            if warnings:
                print(f"   ⚠️  警告: {', '.join(warnings)}")
        if blocked:
            sys.exit(1)
        print("✅ Dry-run 通过")
        return

    # 安全扫描
    blocked, dangers, warnings = _scan_safety(description)
    if not args.json_only:
        print(f"🔍 安全扫描...")
        if warnings:
            print(f"   ⚠️  警告标记: {', '.join(warnings)}")
    if blocked:
        print(f"🛑 安全扫描拦截！检测到高危词汇: {', '.join(dangers)}")
        sys.exit(1)

    # 检查缓存
    if not args.no_cache:
        key = _cache_key(description)
        cached = _cache_get(key)
        if cached:
            if not args.json_only:
                print(f"✅ 命中缓存 (key={key[:12]}...)")
            phases = cached
        else:
            builder = PhaseBuilder(
                model=args.model,
                max_steps=args.max_steps,
                no_cache=args.no_cache,
            )
            phases = builder.build(description)
    else:
        builder = PhaseBuilder(
            model=args.model,
            max_steps=args.max_steps,
            no_cache=args.no_cache,
        )
        phases = builder.build(description)

    # 人工确认
    if not args.json_only:
        print(f"\n{'─' * 52}")
        for i, p in enumerate(phases):
            print(f"  {i + 1}. [{p['name']}] ({p['max_steps']} steps)")
            preview = p["task"][:120].replace("\n", " ")
            print(f"     {preview}...")
        print(f"{'─' * 52}")

        try:
            confirm = input("\n⚡ 确认执行以上 phases？(y/n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n❌ 已取消")
            sys.exit(0)

        if confirm not in ("y", "yes"):
            print("❌ 已取消")
            sys.exit(0)
        print("✅ 已确认")

    # 输出
    output_json = json.dumps(phases, indent=2, ensure_ascii=False)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            f.write(output_json)
        if not args.json_only:
            print(f"📄 写入: {output_path}")
    elif args.json_only:
        print(output_json)
    else:
        print(f"\n📋 生成的 phases JSON:\n{output_json}")


if __name__ == "__main__":
    _cli_main()
