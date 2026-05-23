"""
占位符解析器 — PlaceholderResolver
=================================
将 DeepSeek 生成的带 {feishu:列名} 占位符的 phases 注入飞书真实数据。

职责：在 prompt_builder 输出和 task_runner 执行之间架桥。
  1. 接收带 {feishu:列名} 占位符的 phases
  2. 从飞书 profile 数据中查询对应列的值
  3. 替换占位符并返回可直接执行的 phases
  4. 提供默认值回退机制 + 审计日志

用法:
  from lib.placeholder_resolver import PlaceholderResolver, resolve_phases

  # 方式 1: 便捷函数
  phases = resolve_phases(deepseek_phases, profile_data)

  # 方式 2: 实例（可复用）
  resolver = PlaceholderResolver()
  phases = resolver.resolve(deepseek_phases, profile_data)
"""

from __future__ import annotations

import copy
import re
from typing import Any

# ── 飞书列名 → Python 参数名映射 ──────────────────────────
# 飞书表 T8Za6f 的列名映射到内部使用的 key
FEISHU_FIELD_MAP: dict[str, str] = {
    # 飞书中文列名 → RegistrationManager 标准化字段名
    "邮箱": "email",
    "邮箱密码": "email_password",
    "shopify密码": "shopify_password",
    "First_Name": "first_name",
    "Last_Name": "last_name",
    "TEL": "phone",
    "地址": "address",
    "城市": "city",
    "州": "state",
    "邮编": "zip",
    "SSN": "ssn",
    "店铺名": "shop_name",
    "店铺域名": "store_domain",
}

# ── 默认值（飞书数据缺失时使用）────────────────────────
FALLBACK_VALUES: dict[str, str] = {
    "email": "unknown@example.com",
    "email_password": "",
    "shopify_password": "DefaultP@ss2024",
    "first_name": "User",
    "last_name": "Test",
    "phone": "0000000000",
    "shop_name": "MyStore",
}

# ── 占位符正则 ─────────────────────────────────────────
_PLACEHOLDER_RE = re.compile(r"\{feishu:([^}]+)\}")


class PlaceholderResolver:
    """解析 phases 中的 {feishu:...} 占位符，替换为真实数据。

    支持两种占位符格式：
      - {feishu:邮箱}      — 按飞书列名匹配（推荐）
      - {feishu:store_name} — 特殊占位符（自动生成店铺名）

    安全：
      - 占位符替换失败不会崩溃，使用默认值或保留占位符标记
      - 所有替换操作记录到 _audit_log
    """

    def __init__(self, profile_data: dict[str, Any] | None = None):
        """
        Args:
            profile_data: 飞书记录 (dict)，键为飞书列名（中文）。
                          如果为 None，后续通过 resolve() 传入。
        """
        self._profile = profile_data or {}
        self._audit_log: list[dict] = []

    # ── 主入口 ─────────────────────────────────────────

    def resolve(
        self,
        phases: list[dict],
        profile_data: dict[str, Any] | None = None,
    ) -> list[dict]:
        """解析 phases 中所有 {feishu:...} 占位符。

        Args:
            phases: prompt_builder 输出的 phases 列表
            profile_data: 飞书记录，如果 init 时未提供则此处提供

        Returns:
            替换占位符后的 phases 列表（深拷贝，不影响输入）
        """
        if profile_data:
            self._profile = profile_data
        self._audit_log = []

        resolved = copy.deepcopy(phases)

        for i, phase in enumerate(resolved):
            task = phase.get("task", "")
            new_task, replaced = self._resolve_text(task)
            phase["task"] = new_task

            for r in replaced:
                self._audit_log.append({
                    "phase_index": i,
                    "phase_name": phase.get("name", f"phase-{i}"),
                    "placeholder": r["placeholder"],
                    "col_name": r["col_name"],
                    "resolved_value": r["resolved_value"][:30],
                    "source": r["source"],
                })

        # 打印审计摘要
        if self._audit_log:
            print(f"   🔗 占位符解析: {len(self._audit_log)} 处替换", flush=True)
            for log in self._audit_log[:5]:  # 只显示前5条
                print(f"      {log['placeholder']} → {log['resolved_value']}", flush=True)
            if len(self._audit_log) > 5:
                print(f"      ... 等 {len(self._audit_log)} 处", flush=True)

        return resolved

    # ── 内部方法 ────────────────────────────────────────

    def _resolve_text(self, text: str) -> tuple[str, list[dict]]:
        """解析一段文本中的所有占位符。

        Returns:
            (替换后的文本, 替换记录列表)
        """
        replaced: list[dict] = []

        def _replace(match: re.Match) -> str:
            col_name = match.group(1).strip()
            value, source = self._get_value(col_name)
            replaced.append({
                "placeholder": match.group(0),
                "col_name": col_name,
                "resolved_value": value,
                "source": source,
            })
            return value

        result = _PLACEHOLDER_RE.sub(_replace, text)
        return result, replaced

    def _get_value(self, col_name: str) -> tuple[str, str]:
        """从飞书 profile 数据中获取字段值。

        查找顺序：
          1. 直接按飞书列名匹配（中文）
          2. 按 FEISHU_FIELD_MAP 映射后的参数名匹配
          3. 特殊占位符处理（store_name）
          4. FALLBACK_VALUES 默认值

        Returns:
            (value, source) — source 为 "profile", "special", "fallback", "missing"
        """
        # ── 1. 直接按飞书列名匹配 ──
        if col_name in self._profile:
            value = self._profile[col_name]
            return (self._extract_text(value), "profile")

        # ── 2. 按映射后的 key 匹配 ──
        mapped_key = FEISHU_FIELD_MAP.get(col_name, col_name)
        if mapped_key in self._profile:
            return (str(self._profile[mapped_key]), "profile")

        # ── 3. 特殊占位符 ──
        if col_name == "store_name":
            return (self._generate_store_name(), "special")

        # ── 4. 回退到默认值 ──
        fallback_key = FEISHU_FIELD_MAP.get(col_name, col_name)
        fallback = FALLBACK_VALUES.get(fallback_key)
        if fallback:
            return (fallback, "fallback")

        # ── 5. 无法解析 ──
        return (f"<missing:{col_name}>", "missing")

    def _extract_text(self, cell: Any) -> str:
        """从飞书单元格提取纯文本。
        
        飞书单元格可能是：
          - 纯字符串
          - dict: {"text": "value", "type": "text"}
          - list: [{"text": "part1"}, {"text": "part2"}]
        """
        if isinstance(cell, str):
            return cell
        if isinstance(cell, dict):
            return str(cell.get("text", str(cell)))
        if isinstance(cell, list):
            parts = []
            for item in cell:
                if isinstance(item, dict):
                    parts.append(item.get("text", ""))
                else:
                    parts.append(str(item))
            return "".join(parts)
        return str(cell)

    def _generate_store_name(self) -> str:
        """自动生成店铺名：FirstName + 随机风格词。"""
        import random

        first = ""
        for key in ("First_Name", "first"):
            val = self._profile.get(key)
            if val:
                first = self._extract_text(val)
                break

        if not first:
            first = "Store"

        categories = [
            "Fashion", "Style", "Trend", "Vogue", "Chic",
            "Luxe", "Street", "Urban", "Modern", "Elite",
            "Prime", "Noble", "Royal", "Icon", "Aura",
            "Glow", "Halo", "Vibe", "Zen",
        ]
        return f"{first} {random.choice(categories)}"

    # ── 审计 ───────────────────────────────────────────

    def get_audit_log(self) -> list[dict]:
        """获取最近一次 resolve() 的审计日志。"""
        return self._audit_log


# ── 便捷函数 ──────────────────────────────────────────


def resolve_phases(phases: list[dict], profile_data: dict) -> list[dict]:
    """便捷函数：解析 phases 中所有 {feishu:...} 占位符。

    Args:
        phases: prompt_builder 输出的 phases
        profile_data: 飞书记录 dict

    Returns:
        替换后的 phases
    """
    resolver = PlaceholderResolver(profile_data)
    return resolver.resolve(phases)


# ── 高级：从飞书动态发现可用字段 ───────────────────


def get_available_fields() -> list[dict]:
    """从飞书注册表动态获取可用字段名和示例值。

    用于在 System Prompt 中告知 DeepSeek 可用的占位符字段。
    新增飞书列时无需修改代码。

    Returns:
        [{"name": "邮箱", "placeholder": "{feishu:邮箱}", "sample": "test@..."}]
    """
    try:
        from lib.feishu import read_registration_data

        records = read_registration_data()
        if not records:
            return _static_fields()
    except Exception:
        return _static_fields()

    fields = []
    first = records[0]
    for key, value in first.items():
        if not key or key.startswith("_"):
            continue
        sample = ""
        if isinstance(value, str):
            sample = value[:30]
        elif isinstance(value, dict):
            sample = str(value.get("text", ""))[:30]
        elif isinstance(value, list) and value:
            if isinstance(value[0], dict):
                sample = str(value[0].get("text", ""))[:30]
            else:
                sample = str(value[0])[:30]

        fields.append({
            "name": key,
            "placeholder": f"{{feishu:{key}}}",
            "sample": sample,
        })
    return fields


def _static_fields() -> list[dict]:
    """静态字段列表（回退方案）。"""
    return [
        {"name": "邮箱", "placeholder": "{feishu:邮箱}", "sample": "user@example.com"},
        {"name": "邮箱密码", "placeholder": "{feishu:邮箱密码}", "sample": "********"},
        {"name": "shopify密码", "placeholder": "{feishu:shopify密码}", "sample": "********"},
        {"name": "First_Name", "placeholder": "{feishu:First_Name}", "sample": "John"},
        {"name": "Last_Name", "placeholder": "{feishu:Last_Name}", "sample": "Doe"},
        {"name": "TEL", "placeholder": "{feishu:TEL}", "sample": "7817455182"},
        {"name": "地址", "placeholder": "{feishu:地址}", "sample": "123 Main St"},
        {"name": "城市", "placeholder": "{feishu:城市}", "sample": "New York"},
        {"name": "州", "placeholder": "{feishu:州}", "sample": "NY"},
        {"name": "邮编", "placeholder": "{feishu:邮编}", "sample": "10001"},
        {"name": "SSN", "placeholder": "{feishu:SSN}", "sample": "***-**-****"},
        {"name": "store_name", "placeholder": "{feishu:store_name}", "sample": "John Fashion"},
    ]
