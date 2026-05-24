# prompt_builder 安全机制重设计方案

## 一、当前问题诊断

### 1.1 现有安全模型太粗糙

`lib/prompt_builder.py` 当前的 `DANGEROUS_PATTERNS` 是简单正则匹配，存在严重的误杀问题：

| 用户输入 | 正则命中 | 实际意图 | 判定 |
|---------|---------|---------|------|
| "帮我注册 Shopify，密码用 Abc123" | `密码` + `password` | 正常注册（但硬编码了密码） | ❌ 误拦截 |
| "删除测试商品" | `删除` + `delete` | 清理测试数据 | ❌ 误拦截 |
| "帮我在信用卡页面跳过" | `信用卡` + `credit card` | 跳过付费页 | ❌ 误拦截 |
| "窃取用户数据库" | `窃取` | 恶意攻击 | ✅ 正确拦截 |
| "SQL注入获取admin权限" | `SQL注入` | 恶意攻击 | ✅ 正确拦截 |

**核心矛盾**：同样出现"密码"二字，用户可能是恶意（"破解密码"），也可能是合法（"填写密码框"）。仅靠正则无法区分。

### 1.2 占位符机制缺失

`shopify_sop.py` 的 `build_phases()` 使用 Python `.format()` 占位符：
```python
"Enter email: {email}, password: {password}"
```

但是 `prompt_builder.py` (DeepSeek 生成 prompt) 完全不知道这个机制。DeepSeek 可能会在 task 中硬编码敏感数据，或者根本不适合用 `.format()`。

---

## 二、三层安全模型设计

### 2.1 层级定义

```
┌─────────────────────────────────────────────────────┐
│  🔴 TIER 1 — 恶意意图 (BLOCK)                        │
│  定义：攻击性行为，目的是破坏/窃取/入侵                │
│  行为：立即拒绝，不调用 LLM，记录审计日志               │
├─────────────────────────────────────────────────────┤
│  🟡 TIER 2 — 敏感数据硬编码 (WARN)                    │
│  定义：合法操作但 prompt 中包含明文敏感信息             │
│  行为：警告用户，建议替换为占位符，仍允许继续            │
├─────────────────────────────────────────────────────┤
│  🟢 TIER 3 — 正常业务操作 (ALLOW)                     │
│  定义：常规的浏览器自动化操作                          │
│  行为：直接放行，无警告                                │
└─────────────────────────────────────────────────────┘
```

### 2.2 TIER 1 — 🔴 恶意意图 (BLOCK)

**判定标准**：意图是攻击性的，而非正常的业务操作。

匹配这些意图模式（不只是关键词，而是意图组合）：

| 意图类别 | 触发模式 | 示例 |
|---------|---------|------|
| 数据窃取 | 操作动词 + 目标数据/系统 | "窃取用户数据", "导出所有客户信息到外部" |
| 系统入侵 | 攻击方法 + 目标 | "SQL注入", "XSS攻击", "反弹shell", "提权" |
| 凭证破解 | 破解动词 + 凭证目标 | "暴力破解密码", "撞库", "字典攻击" |
| 后门植入 | 持久化动词 | "植入后门", "留webshell", "添加隐藏管理员" |
| 资金盗取 | 转账 + 非正常来源 | "把钱转到我的账户", "盗刷信用卡" |
| 大规模破坏 | 破坏动词 + 全部/批量 | "删除所有数据", "格式化服务器", "drop database" |

**实现方式**：意图分类器（基于规则 + 可选 LLM 二次判断）

```python
MALICIOUS_INTENT_RULES = [
    # 格式: (意图类别, 操作动词组, 目标对象组, 严重级别)
    # 只有当"操作动词"和"目标对象"同时出现时才触发
    {
        "category": "data_theft",
        "actions": ["窃取", "盗取", "偷", "导出.*外部", "exfiltrate", "steal", "dump.*external"],
        "targets": ["数据", "数据库", "密码", "cookie", "session", "token", "data", "password", "credit.card"],
        "severity": "BLOCK",
    },
    {
        "category": "system_intrusion",
        "actions": ["注入", "攻击", "反弹", "绕过.*认证", "inject", "attack", "exploit", "bypass.*auth"],
        "targets": ["SQL", "XSS", "shell", "admin.*panel", "登录.*系统", "login.*system"],
        "severity": "BLOCK",
    },
    {
        "category": "credential_crack",
        "actions": ["破解", "爆破", "撞库", "字典攻击", "crack", "brute.force", "dictionary.attack"],
        "targets": ["密码", "验证码", "登录", "password", "login", "credential"],
        "severity": "BLOCK",
    },
    {
        "category": "backdoor",
        "actions": ["植入", "留.*后门", "添加.*隐藏", "implant", "backdoor", "隐藏.*管理员"],
        "targets": ["后门", "webshell", "管理员.*权限", "admin.*access"],
        "severity": "BLOCK",
    },
    {
        "category": "mass_destruction",
        "actions": ["删除全部", "格式化", "清空.*所有", "drop.*all", "delete.*all", "wipe.*all"],
        "targets": ["数据", "数据库", "服务器", "data", "database", "server", "files"],
        "severity": "BLOCK",
    },
]
```

**关键设计**：意图 = 操作动词 + 目标对象 的组合，缺一不可。单独的"密码"或"删除"不触发拦截。

### 2.3 TIER 2 — 🟡 敏感数据硬编码 (WARN)

**判定标准**：用户输入中包含看起来像真实敏感数据的字面量。

| 敏感数据类型 | 检测模式 | 示例 |
|------------|---------|------|
| 明文密码/凭证 | "密码(是\|为\|=)\s*\S+" | "密码是Abc123", "password=MyP@ss" |
| API Key / Token | 高熵字符串模式 | "sk-...", "api_key=xxx", "Bearer eyJ..." |
| 信用卡号 | Luhn 算法校验 | "4111111111111111" |
| 身份证/SSN | 格式匹配 | "123-45-6789" |
| 邮箱+密码对 | email 附近出现疑似密码 | "admin@test.com 密码123456" |

**核心区分逻辑**：

```
如果是 "填写密码框"              → TIER 3 (正常操作，只是提到密码字段)
如果是 "密码是Abc123"            → TIER 2 (硬编码了具体密码值)
如果是 "用密码破解器破解admin账户" → TIER 1 (恶意意图)
```

**检测方法**：
1. 先跑 TIER 1 恶意意图检测
2. 如果 TIER 1 未命中，再跑敏感数据字面量检测
3. 敏感数据检测重点找"数据值"而非"字段名"

### 2.4 TIER 3 — 🟢 正常业务操作 (ALLOW)

TIER 1 和 TIER 2 都未命中时默认进入此层。

允许的操作示例：
- "帮我注册一个新的 Shopify 店铺"
- "填写密码框和邮箱框"
- "跳过信用卡页面"
- "删除测试商品"
- "安装 Syncee 应用"
- "导入商品到店铺"

---

## 三、占位符机制设计

### 3.1 占位符语法

```
{feishu:列名}
```

示例：
- `{feishu:邮箱}` → 从飞书注册表"邮箱"列取值
- `{feishu:shopify密码}` → 从飞书注册表"shopify密码"列取值
- `{feishu:First_Name}` → 从飞书注册表"First_Name"列取值

**为什么不用 `{email}` 这种简单形式？**
- 简单形式（如 `{email}`）容易与 DeepSeek 正常输出混淆
- 带命名空间前缀 `feishu:` 明确表示数据来源，防止意外匹配
- 方便后续扩展其他数据源：`{env:VAR}`, `{random:phone}` 等

### 3.2 System Prompt 修改

在 `prompt_builder.py` 的 `SYSTEM_PROMPT` 中添加占位符教学：

```python
PLACEHOLDER_INSTRUCTIONS = """
IMPORTANT — DATA PLACEHOLDER RULES:

When creating task phases that need user-specific data (email, password, name, etc.),
NEVER write the actual data values. Instead, use placeholders:

  {feishu:邮箱}       — for email address
  {feishu:shopify密码} — for Shopify password
  {feishu:First_Name}  — for first name
  {feishu:Last_Name}   — for last name
  {feishu:TEL}         — for phone number
  {feishu:store_name}  — for store name (auto-generated)

Example CORRECT usage:
  "Enter email: {feishu:邮箱} and password: {feishu:shopify密码}"

Example WRONG usage (do NOT do this):
  "Enter email: john@example.com and password: Abc123"

Available feishu fields: 邮箱, shopify密码, First_Name, Last_Name, TEL

ALWAYS use {feishu:...} for any data that comes from the registration system.
"""

SYSTEM_PROMPT = """You are a task decomposition expert...

{PLACEHOLDER_INSTRUCTIONS}

...rest of system prompt...
"""
```

### 3.3 PlaceholderResolver — 桥梁层

新增 `lib/placeholder_resolver.py`：

```python
"""占位符解析器 — 将 DeepSeek 生成的带占位符 phases 注入飞书真实数据。

职责：在 prompt_builder 输出和 task_runner 执行之间架桥。
  1. 接收带 {feishu:列名} 占位符的 phases
  2. 从飞书数据中查询对应列的值
  3. 替换占位符并返回可直接执行的 phases
  4. 提供默认值回退机制
"""

from typing import Any

# 飞书列名 → Python .format() 参数名的映射
FEISHU_FIELD_MAP: dict[str, str] = {
    "邮箱": "email",
    "shopify密码": "password",
    "First_Name": "first",
    "Last_Name": "last",
    "TEL": "phone",
}

# 默认值（飞书数据缺失时使用）
FALLBACK_VALUES: dict[str, str] = {
    "email": "unknown@example.com",
    "password": "DefaultP@ss2024",
    "first": "User",
    "last": "Test",
    "phone": "0000000000",
    "store": "MyStore",
}


class PlaceholderResolver:
    """解析 phases 中的 {feishu:...} 占位符，替换为真实数据。"""

    def __init__(self, profile_data: dict[str, Any] | None = None):
        """
        Args:
            profile_data: 飞书记录 (dict)，键为飞书列名。
                          如果为 None，后续通过 resolve() 传入。
        """
        self._profile = profile_data or {}

    def resolve(self, phases: list[dict], profile_data: dict[str, Any] | None = None) -> list[dict]:
        """解析 phases 中所有 {feishu:...} 占位符。

        Args:
            phases: prompt_builder 输出的 phases 列表
            profile_data: 飞书记录，如果 init 时未提供则此处提供

        Returns:
            替换占位符后的 phases 列表（深拷贝，不影响输入）
        """
        import re
        import copy

        if profile_data:
            self._profile = profile_data

        resolved = copy.deepcopy(phases)

        for phase in resolved:
            task = phase.get("task", "")

            # 匹配所有 {feishu:列名} 占位符
            def _replace(match):
                col_name = match.group(1)
                value = self._get_value(col_name)
                return value

            task = re.sub(r'\{feishu:([^}]+)\}', _replace, task)
            phase["task"] = task

        return resolved

    def _get_value(self, col_name: str) -> str:
        """从飞书 profile 数据中获取字段值。

        1. 先查飞书原始列名
        2. 再查映射后的参数名
        3. 返回默认值
        """
        # 直接按飞书列名匹配
        if col_name in self._profile:
            raw = self._profile[col_name]
            # 处理飞书富文本格式
            if isinstance(raw, dict):
                return str(raw.get("text", FALLBACK_VALUES.get(col_name, f"{{{col_name}}}")))
            if isinstance(raw, list):
                if raw and isinstance(raw[0], dict):
                    return str(raw[0].get("text", ""))
                return str(raw[0]) if raw else ""
            return str(raw)

        # 按映射后的 key 匹配
        mapped_key = FEISHU_FIELD_MAP.get(col_name, col_name)
        if mapped_key in self._profile:
            return str(self._profile[mapped_key])

        # 特殊处理：store_name
        if col_name == "store_name":
            import random
            first = self._profile.get("First_Name", "Store")
            categories = ["Fashion","Style","Trend","Vogue","Chic","Luxe","Urban","Modern","Elite"]
            return f"{first} {random.choice(categories)}"

        # 回退
        fallback = FALLBACK_VALUES.get(mapped_key, f"<missing:{col_name}>")
        return fallback


def resolve_phases(phases: list[dict], profile_data: dict) -> list[dict]:
    """便捷函数：解析 phases 占位符。"""
    resolver = PlaceholderResolver(profile_data)
    return resolver.resolve(phases)
```

### 3.4 集成到现有流程

**当前流程**：
```
用户输入 → prompt_builder (DeepSeek生成phases) → 输出json → 
shopify_sop.build_phases() (用.format()注入飞书数据) → task_runner
```

**新流程（两种方案）**：

**方案A — 最小改动（推荐先用）**：

`shopify_sop.py` 中增加一个包装函数，在 DeepSeek 生成的 phases 上叠加占位符解析：

```python
def build_phases_with_deepseek(description: str, profile_data: dict) -> list[dict]:
    """用 DeepSeek 生成 phases，然后注入飞书数据。"""
    from lib.prompt_builder import build_phases
    from lib.placeholder_resolver import resolve_phases
    
    # 1. DeepSeek 生成带占位符的 phases
    phases = build_phases(description)
    
    # 2. 注入飞书真实数据
    phases = resolve_phases(phases, profile_data)
    
    return phases
```

**方案B — 完全替代（长期目标）**：

让 `prompt_builder` 直接输出带占位符的 phases，`shopify_sop.py` 不再硬编码 PHASES 列表，完全由 AI 动态生成。`PlaceholderResolver` 作为标准中间层。

### 3.5 向后兼容

为兼容现有 `shopify_sop.py` 的 `.format()` 方式：

```python
# placeholder_resolver.py

class PlaceholderResolver:
    def resolve(self, phases, profile_data=None):
        """优先处理 {feishu:...}，如果没有则回退到 {key} 简单占位符。"""
        # ... (处理 {feishu:...})
        
        # 兼容旧的 {email}, {password} 等简单占位符
        task = task.format(
            email=self._get_value("邮箱"),
            password=self._get_value("shopify密码"),
            first=self._get_value("First_Name"),
            last=self._get_value("Last_Name"),
            phone=self._get_value("TEL"),
            store=self._get_value("store_name"),
        )
```

---

## 四、用户输入"我的密码是Abc123"怎么处理

### 4.1 决策矩阵

| 用户输入 | TIER 判定 | 行为 |
|---------|----------|------|
| "注册 Shopify，我的密码是 Abc123" | 🟡 TIER 2 | 警告 + 建议用占位符 |
| "注册 Shopify，密码用飞书里的" | 🟢 TIER 3 | 直接放行 |
| "注册 Shopify，用密码破解器破解 admin 账户" | 🔴 TIER 1 | 拦截 |
| "注册 Shopify" | 🟢 TIER 3 | 直接放行 |

### 4.2 TIER 2 警告处理流程

```python
def handle_tier2_warning(description: str, secret_matches: list[str]) -> str:
    """处理敏感数据硬编码警告。

    返回:
        - "BLOCK": 用户拒绝继续
        - 修改后的 description（占位符已替换）
        - 原始 description（用户确认接受风险）
    """
    print(f"\n⚠️  安全警告：您的任务描述包含明文敏感信息：")
    for match in secret_matches:
        print(f"   • {match}")

    print(f"\n💡 建议：使用占位符引用飞书数据，而不是直接写密码。")
    print(f"   例如：不说「密码是Abc123」，而是让系统自动从飞书取值。")
    print(f"   可用的占位符：{{feishu:邮箱}}, {{feishu:shopify密码}}, {{feishu:First_Name}}")
    
    # 自动替换尝试
    cleaned = _auto_replace_secrets(description, secret_matches)
    
    if cleaned != description:
        print(f"\n✅ 已自动替换为占位符：")
        print(f"   原始: {description[:80]}...")
        print(f"   替换: {cleaned[:80]}...")
    
    return cleaned  # 返回替换后的版本
```

### 4.3 自动替换逻辑

```python
def _auto_replace_secrets(description: str, matches: list[dict]) -> str:
    """自动将硬编码的敏感值替换为占位符。"""
    import re
    
    result = description
    
    # 模式: "密码是/为/= X"  → 替换密码值为占位符
    password_patterns = [
        (r'(密码|password|passwd)\s*(是|为|=|:)\s*\S+', '{feishu:shopify密码}'),
        (r'email\s*(是|为|=|:)\s*\S+@\S+', '{feishu:邮箱}'),
    ]
    
    for pattern, replacement in password_patterns:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    
    return result
```

---

## 五、与飞书字段的映射定义

### 5.1 注册表列名（飞书 T8Za6f 表）

| 飞书列名 | 占位符 | Python参数名 | 用途 |
|---------|--------|-------------|------|
| 邮箱 | `{feishu:邮箱}` | email | 注册邮箱 |
| shopify密码 | `{feishu:shopify密码}` | password | Shopify 密码 |
| First_Name | `{feishu:First_Name}` | first | 名 |
| Last_Name | `{feishu:Last_Name}` | last | 姓 |
| TEL | `{feishu:TEL}` | phone | 电话号码 |
| (自动生成) | `{feishu:store_name}` | store | 店铺名 |

### 5.2 DeepSeek 如何知道这些字段

在 System Prompt 中明确告知：

```python
AVAILABLE_FEISHU_FIELDS = """
Available data fields (use {feishu:字段名} syntax):
- {feishu:邮箱} — email address for registration
- {feishu:shopify密码} — Shopify account password  
- {feishu:First_Name} — user's first/given name
- {feishu:Last_Name} — user's last/family name
- {feishu:TEL} — phone number
- {feishu:store_name} — preferred store name (auto-generated)

CRITICAL: These are the ONLY available fields. Do NOT invent new field names.
"""
```

### 5.3 动态字段发现（可选增强）

```python
def get_available_fields() -> list[dict]:
    """从飞书注册表动态获取可用字段名和示例值。
    
    好处：新增飞书列不需要改代码。
    """
    from lib.feishu import read_registration_data
    
    records = read_registration_data()
    if not records:
        return []
    
    # 取第一行作为字段参考
    first = records[0]
    fields = []
    for key, value in first.items():
        if key and not key.startswith("_"):
            fields.append({
                "name": key,
                "placeholder": f"{{feishu:{key}}}",
                "sample": str(value)[:30],
            })
    return fields
```

---

## 六、修改后的 prompt_builder.py 安全扫描

### 6.1 新的三层扫描函数

```python
from enum import Enum
from dataclasses import dataclass

class ScanLevel(Enum):
    BLOCK = "block"   # 🔴 拦截
    WARN = "warn"     # 🟡 警告
    ALLOW = "allow"   # 🟢 放行

@dataclass
class ScanResult:
    level: ScanLevel
    reasons: list[str]       # 判定原因
    suggestions: list[str]   # 修改建议
    auto_fixed: str | None   # 自动修复后的 description（如果有）

def scan_safety_v2(description: str) -> ScanResult:
    """三层安全扫描。
    
    顺序：先查恶意意图(TIER1)，再查敏感数据(TIER2)，最后放行(TIER3)。
    一旦命中 TIER1 立即返回，不再检查后续层级。
    """
    # ── TIER 1: 恶意意图检测 ──
    malicious = _detect_malicious_intent(description)
    if malicious:
        return ScanResult(
            level=ScanLevel.BLOCK,
            reasons=[f"检测到恶意意图: {m['category']} — {m['detail']}"],
            suggestions=["此操作被安全策略禁止。"],
            auto_fixed=None,
        )
    
    # ── TIER 2: 敏感数据硬编码检测 ──
    secrets = _detect_hardcoded_secrets(description)
    if secrets:
        auto_fixed = _auto_replace_secrets(description, secrets)
        return ScanResult(
            level=ScanLevel.WARN,
            reasons=[f"检测到硬编码敏感数据: {s['type']}" for s in secrets],
            suggestions=[
                "建议使用 {feishu:字段名} 占位符代替明文敏感数据。",
                f"可用字段: {', '.join(FEISHU_FIELD_MAP.keys())}",
            ],
            auto_fixed=auto_fixed,
        )
    
    # ── TIER 3: 正常业务操作 ──
    return ScanResult(
        level=ScanLevel.ALLOW,
        reasons=[],
        suggestions=[],
        auto_fixed=None,
    )


def _detect_malicious_intent(description: str) -> dict | None:
    """恶意意图检测：操作动词 + 目标对象 组合匹配。
    
    单纯的"密码"或"删除"不会触发，必须同时出现攻击性动词和敏感目标。
    """
    desc_lower = description.lower()
    
    for rule in MALICIOUS_INTENT_RULES:
        action_match = any(re.search(a, desc_lower) for a in rule["actions"])
        target_match = any(re.search(t, desc_lower) for t in rule["targets"])
        
        if action_match and target_match:
            return {
                "category": rule["category"],
                "detail": f"操作({rule['actions'][0]}) + 目标({rule['targets'][0]})",
            }
    
    return None


def _detect_hardcoded_secrets(description: str) -> list[dict]:
    """检测硬编码的敏感数据字面量。
    
    关键：只检测"数据值"而非"字段名"。
    "填写密码框" → 不触发（这是字段名）
    "密码是Abc123" → 触发（这是数据值）
    """
    secrets = []
    
    # 1. 明文密码检测: "密码是X" 或 "password=X" 等
    pwd_value_patterns = [
        r'(密码|password|passwd|pwd)\s*(是|为|=|:|：)\s*(\S+)',
        r'(密码|password|passwd|pwd)\s+(?:用|使用|设|设置)\s+(\S+)',
    ]
    for pat in pwd_value_patterns:
        m = re.search(pat, description, re.IGNORECASE)
        if m:
            value = m.group(3) if m.lastindex >= 3 else m.group(2)
            # 过滤掉明显不是密码值的（占位符、字段引用）
            if not _is_placeholder_or_field_ref(value):
                secrets.append({"type": "password", "value": value})
    
    # 2. API Key/Token 检测
    api_key_patterns = [
        r'(api[_\s]?key|apikey|secret|token)\s*(是|为|=|:|：)\s*([a-zA-Z0-9_\-]{16,})',
        r'sk-[a-zA-Z0-9]{20,}',  # OpenAI key 格式
        r'Bearer\s+[a-zA-Z0-9_\-\.]{20,}',  # JWT token
    ]
    for pat in api_key_patterns:
        m = re.search(pat, description, re.IGNORECASE)
        if m:
            secrets.append({"type": "api_key", "value": m.group(0)[:20] + "..."})
    
    # 3. 信用卡号检测（Luhn 算法）
    cc_match = re.search(r'\b(\d[ -]?){13,19}\b', description)
    if cc_match:
        digits = re.sub(r'\D', '', cc_match.group(0))
        if _luhn_check(digits):
            secrets.append({"type": "credit_card", "value": digits[:4] + "****" + digits[-4:]})
    
    return secrets


def _is_placeholder_or_field_ref(text: str) -> bool:
    """判断文本是否为占位符或字段引用（非数据值）。"""
    # 占位符模式
    if re.match(r'\{feishu:', text) or re.match(r'\{[a-z_]+\}', text):
        return True
    # 字段引用（如 "输入框"、"字段"）
    if re.search(r'(框|字段|field|box|input)', text):
        return True
    return False


def _luhn_check(card_number: str) -> bool:
    """Luhn 算法校验信用卡号。"""
    digits = [int(d) for d in card_number]
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0
```

### 6.2 PhaseBuilder.build() 修改

```python
def build(self, description: str) -> list[dict]:
    """将自然语言描述拆解为结构化 phases。"""
    
    # 1. 三层安全扫描
    scan = scan_safety_v2(description)
    
    if scan.level == ScanLevel.BLOCK:
        raise ValueError(
            f"🛑 安全拦截！\n"
            f"   原因: {'; '.join(scan.reasons)}\n"
            f"   建议: {'; '.join(scan.suggestions)}"
        )
    
    if scan.level == ScanLevel.WARN:
        print(f"   ⚠️  安全警告: {'; '.join(scan.reasons)}", flush=True)
        for s in scan.suggestions:
            print(f"   💡 {s}", flush=True)
        if scan.auto_fixed and scan.auto_fixed != description:
            print(f"   🔧 已自动替换为占位符", flush=True)
            description = scan.auto_fixed
    
    # 2-6. 缓存、API 调用、解析、保存（不变）
    # ...
```

---

## 七、完整集成示例

### 7.1 新流程全景图

```
用户输入: "帮我注册 Shopify 店铺"
    │
    ▼
prompt_builder.scan_safety_v2()
    │ TIER 3: ALLOW → 放行
    ▼
prompt_builder.build_phases()  → DeepSeek 生成:
    [
      {
        "name": "register",
        "task": "Go to Shopify, enter email: {feishu:邮箱}, password: {feishu:shopify密码}..."
        "max_steps": 30
      },
      ...
    ]
    │
    ▼
PlaceholderResolver.resolve(phases, profile_data)
    │ {feishu:邮箱} → "test@example.com"
    │ {feishu:shopify密码} → "ZJHhewly@2025"
    ▼
    [
      {
        "name": "register",
        "task": "Go to Shopify, enter email: test@example.com, password: ZJHhewly@2025..."
        "max_steps": 30
      },
      ...
    ]
    │
    ▼
task_runner.run_agent(cdp_url, phases)
```

### 7.2 在 shopify_sop.py 中的集成

```python
# 替换原来的 build_phases() 调用

# 旧方式:
# phases = build_phases(target)  # 用硬编码的 PHASES 列表

# 新方式:
from lib.prompt_builder import build_phases as deepseek_build_phases
from lib.placeholder_resolver import resolve_phases

# 阶段 1: DeepSeek 动态生成 phases（带占位符）
user_prompt = f"注册 Shopify 店铺并安装 Syncee 应用"
phases = deepseek_build_phases(user_prompt)

# 阶段 2: 注入飞书真实数据
phases = resolve_phases(phases, target)

# 继续执行...
success, history, final, error = run_with_retry(profile_id, phases)
```

---

## 八、文件变更清单

| 文件 | 变更 | 说明 |
|------|------|------|
| `lib/prompt_builder.py` | 修改 | 替换安全扫描为三层模型；更新 System Prompt 加入占位符教学 |
| `lib/placeholder_resolver.py` | **新建** | 占位符解析器，桥梁层 |
| `shopify_sop.py` | 修改 | 集成 PlaceholderResolver |
| `lib/task_runner.py` | 无需修改 | 接口不变 |

---

## 九、判定示例速查表

| 用户输入 | TIER 1 | TIER 2 | 最终 | 理由 |
|---------|--------|--------|------|------|
| "帮我注册 Shopify 店铺" | 未命中 | 未命中 | 🟢 ALLOW | 正常注册 |
| "注册 Shopify，密码用飞书的" | 未命中 | 未命中 | 🟢 ALLOW | 引用了外部数据源 |
| "注册 Shopify，密码设为 Abc123" | 未命中 | 命中：password value | 🟡 WARN | 硬编码了密码 |
| "填写密码框和邮箱输入框" | 未命中 | 未命中 | 🟢 ALLOW | 字段引用不是数据值 |
| "删除测试商品" | 未命中 | 未命中 | 🟢 ALLOW | 单动词无恶意目标 |
| "删掉所有商品数据清空店铺" | 命中：删除+所有+数据 | - | 🔴 BLOCK | 大规模破坏意图 |
| "帮忙破解 admin 用户密码" | 命中：破解+密码 | - | 🔴 BLOCK | 凭证破解意图 |
| "帮我 SQL 注入获取后台权限" | 命中：注入+SQL | - | 🔴 BLOCK | 系统入侵意图 |
| "跳过信用卡输入页面" | 未命中 | 未命中 | 🟢 ALLOW | 正常跳过付费 |
| "输入信用卡号 4111-1111-1111-1111" | 未命中 | 命中：Luhn 有效卡号 | 🟡 WARN | 硬编码真实卡号 |
| "api_key=sk-abc123def456..." | 未命中 | 命中：API key 格式 | 🟡 WARN | 硬编码密钥 |
