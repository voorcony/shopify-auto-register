# lib/prompt_builder.py 详细实现方案

> 作者：AI Agent · 日期：2026-05-23 · 版本：v1.0

---

## 概述

### 模块职责

`lib/prompt_builder.py` 是 AI 辅助的任务拆解器：用户输入自然语言任务描述，模块调用 DeepSeek API 将其拆解为结构化的 JSON phases，供 `lib/auto_phase_runner.py` 的 `AutoPhaseRunner` 直接消费。

### 核心流程图

```
用户输入 ("帮我注册一个新的 Shopify 店铺")
        │
        ▼
┌─────────────────────────────┐
│  1. 敏感词扫描 (安全三级之一)   │
└─────────────────────────────┘
        │ 通过
        ▼
┌─────────────────────────────┐
│  2. 缓存查询 (MD5 + 7天TTL) │
└─────────────────────────────┘
        │ 未命中
        ▼
┌─────────────────────────────┐
│  3. DeepSeek API 调用       │
│     system prompt           │
│       + user description    │
│     → JSON phases           │
└─────────────────────────────┘
        │
        ▼
┌─────────────────────────────┐
│  4. JSON 解析 + 校验        │
└─────────────────────────────┘
        │ 通过
        ▼
┌─────────────────────────────┐
│  5. 写入缓存                │
└─────────────────────────────┘
        │
        ▼
┌─────────────────────────────┐
│  6. 人工确认 (安全三级之二)  │
└─────────────────────────────┘
        │
        ▼
    返回 phases → 执行
```

### 设计目标

| 指标 | 目标值 |
|------|--------|
| 单次 API 调用次数 | **1 次**（非流式） |
| 预估延迟 | ~2-5s（DeepSeek 响应时间） |
| 预估成本 | ~¥0.005/次 |
| 代码行数 | ~250 行 |
| 输出格式 | `[{"name":"...","task":"...","max_steps":N}, ...]` |

---

## 问题一：DeepSeek 调用的 model 选择

### 方案对比

| 维度 | `deepseek-chat` | `deepseek-reasoner` |
|------|-----------------|---------------------|
| 定价 (input) | ¥0.001/1K tokens | ¥0.004/1K tokens |
| 定价 (output) | ¥0.002/1K tokens | ¥0.016/1K tokens |
| 推理能力 | 标准对话 | 深度推理 (CoT) |
| 响应延迟 | ~2s | ~10-30s |
| 单次预估成本 | ~¥0.005 | ~¥0.08 |
| 本次任务适用性 | ✅ 充分 | ⚠️ 过度 |

### 推荐方案：`deepseek-chat`

**理由：**

1. **任务复杂度不匹配reasoner的定位。** 任务拆解本质是"阅读理解 + 结构化输出"——理解一段自然语言描述，拆成 3-6 个逻辑步骤，每个步骤写一个小段 prompt。这不是需要深度推理的数学/逻辑/编程问题，而是一个受约束的生成任务。`deepseek-chat` 完全能够胜任。

2. **成本差距悬殊（16x）。** reasoner 的 output token 价格是 chat 的 8 倍，还有不可见的内部 CoT token 消耗。按每次 500 input + 1500 output 估算：
   - `deepseek-chat`: ¥0.001 × 0.5 + ¥0.002 × 1.5 = **¥0.0035**
   - `deepseek-reasoner`: ¥0.004 × 0.5 + ¥0.016 × 1.5 + 隐藏 CoT = **¥0.026+**
   - 如果每天 100 次调用，月费差距 ¥67.5 vs ¥78，reasoner 完全浪费。

3. **延迟敏感场景。** 用户输入描述后等待 phases 生成，2s 和 20s 的体验天差地别。

4. **System prompt 约束 + 示例 已足够引导。** 通过精心设计的 system prompt（带 1-2 个 few-shot 示例），`deepseek-chat` 就能稳定输出合规的 JSON。不需要额外的 CoT 推理。

**降级策略（可选）：** 如果发现 `deepseek-chat` 输出格式不稳定（JSON 解析失败率 >5%），可以加一个简单判断——连续 2 次解析失败后提示用户简化描述，而非升级到 reasoner。实际概率极低。

---

## 问题二：hermes_proxy 的利用

### 方案对比

| 维度 | 直接调 `api.deepseek.com` | 过 `hermes_proxy` (127.0.0.1:18888) |
|------|---------------------------|--------------------------------------|
| 延迟 | 直接，~2s | +1ms（本地代理） |
| 额外功能 | 无 | 自动 strip `reasoning_content` + 注入 `thinking=disabled` |
| 复杂度 | 需自己管理 API key 和请求格式 | 统一 OpenAI 兼容接口 `/v1` |
| 必要性 | 完全够用 | 锦上添花 |

### 推荐方案：直接调 `api.deepseek.com`

**理由：**

1. **hermes_proxy 的核心价值不在本次场景。** hermes_proxy 的核心功能是 strip `reasoning_content` 和强制 `thinking=disabled`，这在 **deepseek-reasoner** 场景下才有意义（reasoner 默认会输出 CoT）。但我们选的是 `deepseek-chat`，它**本身就不会输出 reasoning_content**，也没 thinking 开关。所以 hermes_proxy 的两大功能在此完全无用。

2. **多一层代理多一个故障点。** 虽然本地代理延迟几乎为零，但它是一个额外的进程依赖。如果 proxy 没启动，调用就失败。直接调 DeepSeek API 无此问题。

3. **API key 管理统一。** DeepSeek API key 已经在 `config.yaml` 的 `deepseek.api_key`（从飞书 81b55a 加载），直接用 `lib.config.load_feishu_secrets()` 拿就行。不引入新的配置路径。

4. **实现极度简单。** 用 `requests` 或 `httpx` 直接 POST 到 `https://api.deepseek.com/v1/chat/completions`，OpenAI 兼容格式，不需要任何适配层。

**实现细节：**

```python
# 调用方式
import requests

cfg = load_feishu_secrets()
api_key = cfg.get("deepseek", {}).get("api_key", "")

response = requests.post(
    "https://api.deepseek.com/v1/chat/completions",
    headers={"Authorization": f"Bearer {api_key}"},
    json={
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_description}
        ],
        "temperature": 0.3,
        "max_tokens": 4096,
        "response_format": {"type": "json_object"},  # 强制 JSON 输出
    },
    timeout=30
)
```

---

## 问题三：System prompt 设计

### 推荐方案

**System prompt 三要素：角色设定 + 输出约束 + Few-shot 示例**

```
You are a task decomposition expert. Your job is to break down a user's 
high-level task description into a sequence of actionable phases.

Each phase is a self-contained instruction for an AI browser agent that 
has a 14,000 token context budget per phase. That means:

1. Each phase's "task" field should be a COMPLETE, SELF-CONTAINED 
   prompt — the agent sees ONLY this phase's task text, nothing more.
   
2. Keep each phase's task UNDER 800 characters. If a task needs more 
   detail, break it into more phases rather than longer text.

3. Provide ENOUGH DETAIL in each phase so the agent knows exactly what 
   to do: target URLs, specific buttons to click, data to enter, and 
   what "done" looks like.

4. Each phase must have a clear success criterion — the agent should 
   know when to stop.

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

max_steps: typically 15-30. Use 15 for simple navigation, 25 for 
form-filling, 30 for complex multi-step flows.

---

EXAMPLE:

User task: "Register a Shopify account, install Syncee app, import 5 
fashion products"

Output:
{
  "phases": [
    {
      "name": "register-account",
      "task": "Register a new Shopify account.\n\n1. Go to https://www.shopify.com\n2. Click 'Start free trial'\n3. Enter email: {email}, password: {password}\n4. Fill name: {first} {last}\n5. On plan selection, find and click 'Skip' or free trial — NEVER enter credit card info\n6. Navigate to admin dashboard\n\nCRITICAL: If you reach admin dashboard, report SUCCESS immediately.",
      "max_steps": 30
    },
    {
      "name": "complete-setup",
      "task": "Complete Shopify onboarding wizard.\n\n1. You should be at admin dashboard\n2. If there's an onboarding checklist, complete it\n3. Set country: United States, timezone: America/Chicago, currency: USD\n4. Set store name: {store_name}\n5. Skip any paid upgrades\n\nIf already past onboarding (main sidebar visible), report SUCCESS immediately.",
      "max_steps": 20
    },
    {
      "name": "install-syncee",
      "task": "Install Syncee AI Dropship app.\n\n1. From Shopify admin, go to: https://apps.shopify.com/syncee-1\n2. Click 'Add app' or 'Install'\n3. Confirm installation\n4. Wait for Syncee dashboard to load\n5. Report Syncee dashboard URL\n\nIf already installed, report SUCCESS.",
      "max_steps": 15
    },
    {
      "name": "import-products",
      "task": "Import products using Syncee.\n\n1. You should be on Syncee dashboard\n2. Find product import/search section\n3. Search for 'fashion accessories'\n4. Click 'Push to store' or 'Import' on at least 5 products\n5. Navigate to Shopify Products page to verify\n\nOne product imported = success minimum.",
      "max_steps": 25
    }
  ]
}
```

**设计理由：**

1. **14K token 预算明示给模型。** 不说具体数字（模型不理解 token 计数），而是说"each phase's task should be UNDER 800 characters"，这是可操作的约束。

2. **"自包含"原则反复强调。** 因为 `AutoPhaseRunner` 的每个 phase 只拿到自己的 `task` 文本，模型必须理解这一点才不会写出跨 phase 的依赖（如"do the next step from the previous phase"）。

3. **Few-shot 示例是成败关键。** 不给示例，模型输出质量极不稳定——可能输出非 JSON、可能把 `name` 写成 `phase_name`、可能 `max_steps` 是字符串。给一个完整的示例后，格式遵从率接近 100%。

4. **示例与现有业务对齐。** 示例直接参考了 `shopify_sop.py` 中 `PHASES` 的实际内容和 `template_phases.py`（假设存在，用于存放可复用的 phase 模板），让模型输出风格一致。

5. **max_steps 给了指导范围。** 避免模型输出 `max_steps: 100`（超出实际限制）或 `max_steps: 3`（不够用）。

---

## 问题四：缓存粒度

### 方案对比

| 维度 | MD5 精确匹配 | 语义相似度匹配 |
|------|-------------|---------------|
| 实现复杂度 | 极低 (~3行) | 高（需 embedding + 向量数据库） |
| 命中率 | 低（描述改一个字就 miss） | 高（同义描述可命中） |
| 存储成本 | 几乎为零 | 需要存向量 + 原始文本 |
| 误匹配风险 | 无 | 有（语义相似≠任务相同） |
| 延迟 | <1ms | ~100ms+（embedding 计算） |
| 运维复杂度 | 无 | 需维护 embedding 模型 |

### 推荐方案：MD5 精确匹配（带标准化预处理）

**理由：**

1. **成本收益不匹配。** 每次 DeepSeek 调用成本仅 ¥0.005。引入语义相似度匹配需要 embedding 模型（即使本地跑也要 ~50ms + 内存），加上向量存储和检索逻辑。对于 ¥0.005/次 的节省，完全得不偿失。

2. **标准化预处理可大幅提高命中率。** MD5 精确匹配的致命弱点是"帮我买耳机"和"帮我在Amazon找耳机"算不同输入。但我们可以做轻量预处理后再 MD5：
   ```python
   import re, hashlib
   
   def _normalize(text: str) -> str:
       # 1. 小写
       text = text.lower()
       # 2. 去掉多余空白 + 标点归一化
       text = re.sub(r'\s+', ' ', text)
       text = re.sub(r'[，。！？、；：""''【】]', ',', text)  # 中文标点统一为英文
       text = re.sub(r'[,!?;:]+', ',', text)
       # 3. 去掉"请/帮我/能不能"等语气词（正则匹配常见模式）
       text = re.sub(r'\b(请|帮我|能不能|可以|麻烦)\s*', '', text)
       # 4. 去尾随标点
       text = text.strip(',. ')
       return text
   
   def _cache_key(description: str) -> str:
       return hashlib.md5(_normalize(description).encode()).hexdigest()
   ```
   这样 "帮我买耳机" 和 "帮我买耳机 " 和 "请帮我买耳机" 都会命中同一缓存。

3. **7天TTL适合本场景。** Shopify 注册等任务的 phases 是相对稳定的。7天过期自动刷新，确保 phases 模板保持新鲜度。

4. **缓存目录结构：**
   ```
   ~/.shopify/prompt_cache/
   ├── index.json          # {"md5_key": {"ts": timestamp, "description": "..."}}
   ├── a1b2c3d4...json     # 缓存文件 = phases JSON
   └── e5f6g7h8...json
   ```

---

## 问题五：敏感词扫描规则

### 推荐方案：分层规则 + 全量拒绝

**三级安全体系回顾：**
- 第一级：**敏感词扫描**（本问题的核心，入口处拦截）
- 第二级：**人工确认**（生成后、执行前，展示 phases 让用户确认）
- 第三级：**元数据标记**（生成的 phases 标注来源为 "AI-generated"，执行日志可追溯）

### 敏感词分类

#### 🔴 高危（立即拒绝，不调用 API）

这些词一旦出现，说明用户意图**明显危险**，不应该生成任何 phases：

| 类别 | 模式示例 | 理由 |
|------|---------|------|
| 支付/金融操作 | `信用卡`, `credit card`, `CVV`, `支付密码`, `转账`, `transfer`, `汇款` | 涉及资金操作，AI 生成的指令不可信 |
| 账号凭证获取 | `密码`, `password`, `token`, `API key`, `secret`, `access_key` | 防止通过生成 phases 来引导 agent 提取凭证 |
| 系统破坏 | `删除`, `清空`, `delete`, `drop`, `rm -rf`, `format`, `wipe`, `shutdown` | 不可逆销毁操作 |
| 恶意注册/注入 | `SQL注入`, `XSS`, `反弹shell`, `reverse shell`, `后门`, `backdoor` | 明确攻击意图 |
| 隐私侵犯 | `窃取`, `偷`, `盗号`, `hack`, `crack`, `破解密码` | 违法意图 |

#### 🟡 警告（需要额外确认）

这些词可能合法也可能恶意，放行但标记：

| 类别 | 模式示例 | 处理方式 |
|------|---------|---------|
| 批量操作 | `批量`, `bulk`, `所有`, `全部`, `all`, `every` | 标记 `⚠️ 批量操作` |
| 网络请求 | `curl`, `wget`, `download` | 标记 `⚠️ 网络请求` |
| 店铺管理 | `关店`, `删除店铺`, `close store` | 标记 `⚠️ 不可逆操作` |

#### 🟢 放行（业务正常词汇）

`注册`, `登录`, `安装`, `导入`, `搜索`, `导航`, `Sign up`, `Install`, `Import`...

### 实现方式

```python
import re

# 高危模式：任一匹配 → 立即拒绝
DANGEROUS_PATTERNS = [
    (r'(信用卡|credit.?card|CVV|cvv)', '支付信息'),
    (r'(密码|password|passwd|secret|token|api.?key)', '凭证操作'),
    (r'(删除|清空|delete|drop\s+table|rm\s+-rf|format|wipe)', '破坏性操作'),
    (r'(转账|transfer|汇款|wire)', '资金操作'),
    (r'(SQL\s*注入|XSS|反弹\s*shell|reverse\s*shell|后门|backdoor)', '攻击行为'),
    (r'(窃取|盗号|hack|crack|破解)', '隐私侵犯'),
]

# 警告模式：匹配但仅标记
WARNING_PATTERNS = [
    (r'(批量|bulk|所有|全部|all\b|every\b)', '批量操作'),
    (r'(curl|wget|download)', '网络请求'),
    (r'(关店|关闭\s*店铺|删除\s*店铺|close\s*store)', '不可逆店铺操作'),
]

def scan(description: str) -> tuple[bool, list[str], list[str]]:
    """
    Returns:
        (blocked: bool, danger_reasons: list[str], warnings: list[str])
    """
    dangers = [reason for pattern, reason in DANGEROUS_PATTERNS 
               if re.search(pattern, description, re.IGNORECASE)]
    warnings = [reason for pattern, reason in WARNING_PATTERNS 
                if re.search(pattern, description, re.IGNORECASE)]
    return (len(dangers) > 0, dangers, warnings)
```

**设计理由：**

1. **宁可误杀，不可漏过。** 高危模式匹配任何一个就拒绝。生成 phases 的成本 ¥0.005，但生成恶意 phases 的代价可能是不可逆的。
2. **分层清晰。** 🔴立即拒绝、🟡警告标记、🟢放行。让安全判断不是二元的。
3. **中文优先，英文兼容。** 本项目的用户主要使用中文，但 agent 输出是英文 prompt，所以双语匹配。

---

## 问题六：CLI 设计

### 推荐方案

```bash
# 最简用法：单行生成 phases
python3 -m lib.prompt_builder "帮我注册一个新的 Shopify 店铺并安装 Syncee"

# 带参数
python3 -m lib.prompt_builder --max-steps 20 "只做注册，不要安装任何应用"

# 从文件读取（复杂描述）
python3 -m lib.prompt_builder --file /path/to/task.txt

# 跳过缓存强制重新生成
python3 -m lib.prompt_builder --no-cache "..."

# 指定输出路径（默认打印到 stdout）
python3 -m lib.prompt_builder --output phases/my_task.json "..."

# 静默模式（只输出 JSON）
python3 -m lib.prompt_builder --json-only "..."
```

### 参数设计

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `description` | 位置参数，任务描述 | （必填，除非 `--file`） |
| `--file`, `-f` | 从文件读取描述 | - |
| `--output`, `-o` | 输出 JSON 路径 | stdout |
| `--max-steps` | 每个 phase 的默认 max_steps | 25 |
| `--no-cache` | 跳过缓存检查 | false |
| `--json-only` | 只输出 JSON（无日志） | false |
| `--dry-run` | 只扫描不调用 API | false |
| `--model` | 模型名 | `deepseek-chat` |

### 设计理由：

1. **`python3 -m lib.prompt_builder` 比自定义 CLI 命令更简洁。** 不需要额外安装，不需要 PATH 配置。与 `python3 -m http.server` 一样的体验。

2. **位置参数用于最常用场景。** 90% 的使用场景就是一行描述，不需要任何 flag。

3. **`--json-only` 支持管道。** 可以 `python3 -m lib.prompt_builder --json-only "..." | python3 shopify_sop.py --phases-stdin` 实现链式调用。

4. **`--dry-run` 用于安全审计。** 用户可以预览敏感词扫描结果，而不消耗 API 额度。

### 内部 API（作为库调用）

CLI 只是薄封装，核心逻辑通过函数暴露：

```python
from lib.prompt_builder import build_phases, PhaseBuilder

# 函数式接口（推荐）
phases = build_phases("帮我注册 Shopify...")  
# → [{"name":"...", "task":"...", "max_steps":25}, ...]

# 类接口（需要更多控制）
builder = PhaseBuilder(model="deepseek-chat", max_steps=25)
phases = builder.build("...")
```

这让 `shopify_sop.py` 等模块可以直接 `import` 使用，不需要走 CLI。

---

## 问题七：与现有 phases/ 目录的关系

### 现状

当前没有统一的 `phases/` 目录。现有的 phases 定义分散在两处：
- `shopify_sop.py` 中的 `PHASES` 常量（硬编码的模板 phases）
- 飞书表格中的 prompt（通过 `read_prompt()` 读取）

### 推荐方案：自动保存但不自动执行

```python
# 目录结构
~/.shopify/
├── prompt_cache/          # 缓存（MD5 + 7天TTL）
│   ├── index.json
│   └── *.json
└── phases/
    └── generated/         # AI 生成的 phases（人工确认后移入）
        ├── 2026-05-23_132754_shopify_register.json
        └── 2026-05-23_142105_import_products.json
```

### 行为定义

| 触发条件 | 行为 |
|---------|------|
| 通过所有安全检查 + 缓存未命中 | 生成 → 保存到 `phases/generated/` |
| 保存的文件名 | `{YYYY-MM-DD_HHMMSS}_{name_slug}.json` |
| 人工确认 | **需要用户在 CLI 确认**（安全三级中的第二级） |
| 确认后 | 返回 phases，由调用方决定是否执行 |

### 设计理由：

1. **生成 ≠ 执行。** 安全设计明确规定"人工确认"环节。phases 保存到 `generated/` 只是存档，不是自动执行。

2. **与现有代码解耦。** `shopify_sop.py` 中的 `build_phases()` 继续使用硬编码模板。新的 `prompt_builder` 是补充，不是替代。用户可以：
   - 继续用硬编码模板（稳定场景）
   - 或用 AI 生成新 phases（新场景/新业务）
   - 或混合：AI 生成 → 人工审查 → 固化到代码中

3. **文件名包含时间戳和 slug。** 便于追溯和审计。"谁在什么时候生成了什么 phases"一目了然。

4. **不影响现有飞书流程。** 飞书的 prompt 表照常工作。生成的文件在本地，不污染飞书数据。

### `name_slug` 提取逻辑

```python
def _make_slug(name: str) -> str:
    """从第一个 phase 的 name 或用户描述中提取 slug"""
    # 提取前 3 个有意义的关键词
    words = re.findall(r'[a-zA-Z]+', name.lower())
    return '_'.join(words[:4])[:40]
```

---

## 问题八：重试策略

### 推荐方案：三级降级重试

```
调用 DeepSeek API
    │
    ├── 成功 → 尝试解析 JSON
    │           ├── 解析成功 → 校验 phases 结构
    │           │               ├── 通过 → 返回
    │           │               └── 失败 → 重试 (最多 2 次)
    │           └── 解析失败 → 重试 (最多 2 次)
    │
    ├── 网络超时 (30s) → 重试 ×1 (共 2 次)
    │       └── 仍失败 → 返回错误 "DeepSeek API 不可用，请稍后重试"
    │
    ├── HTTP 4xx (401/403/429) → 不重试，直接报错
    │       └── 401: "API Key 无效，请检查配置"
    │       └── 429: "请求频率过高，请等待 30s 后重试"
    │
    └── HTTP 5xx → 重试 ×2 (共 3 次)，指数退避
            └── 退避时间: 1s → 3s → 9s
            └── 仍失败 → 返回错误
```

### 实现伪代码

```python
import time
import requests

MAX_RETRIES = 2
BACKOFF_BASE = 1.5  # 指数退避基值

def _call_deepseek(messages: list, api_key: str) -> dict:
    """调用 DeepSeek API，带重试和降级"""
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
                    "model": "deepseek-chat",
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
            if response.status_code == 429:
                if attempt < MAX_RETRIES:
                    wait = 5 * (2 ** attempt)
                    print(f"   ⏳ 频率限制，等待 {wait}s 后重试...", flush=True)
                    time.sleep(wait)
                    continue
                raise RuntimeError("请求频率过高，请等待后重试")
            if response.status_code == 402:
                raise RuntimeError("DeepSeek 账户余额不足")
            
            # 5xx 重试
            if response.status_code >= 500:
                raise ConnectionError(f"DeepSeek 服务器错误 (HTTP {response.status_code})")
            
            response.raise_for_status()
            return response.json()
            
        except requests.Timeout:
            last_error = "请求超时"
            if attempt < MAX_RETRIES:
                print(f"   ⏳ 超时，重试中... ({attempt+1}/{MAX_RETRIES})")
                time.sleep(BACKOFF_BASE ** attempt)
                continue
                
        except requests.ConnectionError as e:
            last_error = f"网络连接失败: {e}"
            if attempt < MAX_RETRIES:
                print(f"   ⏳ 网络错误，重试中... ({attempt+1}/{MAX_RETRIES})")
                time.sleep(BACKOFF_BASE ** attempt)
                continue
                
        except (PermissionError, RuntimeError) as e:
            # 不重试的错误
            raise
    
    raise RuntimeError(f"DeepSeek API 调用失败 (已重试 {MAX_RETRIES} 次): {last_error}")
```

### JSON 解析重试

```python
def _parse_and_validate(raw_response: dict, messages: list, api_key: str) -> list[dict]:
    """解析 JSON phases + 校验结构。解析失败则重试整个调用。"""
    for parse_attempt in range(2):  # 最多重试 1 次解析
        try:
            content = raw_response["choices"][0]["message"]["content"]
            data = json.loads(content)
            phases = data.get("phases", [])
            
            # 校验
            if not phases:
                raise ValueError("phases 不能为空")
            for i, p in enumerate(phases):
                if not isinstance(p.get("name"), str) or not p["name"].strip():
                    raise ValueError(f"phase[{i}] 缺少有效 name")
                if not isinstance(p.get("task"), str) or not p["task"].strip():
                    raise ValueError(f"phase[{i}] 缺少有效 task")
                if not isinstance(p.get("max_steps"), (int, float)):
                    raise ValueError(f"phase[{i}] max_steps 必须是数字")
                # 规范化
                p["max_steps"] = int(p["max_steps"])
                p["max_steps"] = max(5, min(p["max_steps"], 50))  # 钳制到 [5, 50]
            
            return phases
            
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            if parse_attempt == 0:
                print(f"   ⚠️ JSON 解析失败: {e}，重试调用...")
                raw_response = _call_deepseek(messages, api_key)
            else:
                raise ValueError(f"DeepSeek 输出格式异常 (已重试): {e}")
```

### 设计理由：

1. **分类处理，不盲目重试。** 4xx（客户端错误）重试没有意义——API key 不会因为重试就变有效。5xx（服务端错误）适合重试。429（限流）需要退避后重试。

2. **指数退避。** 简单的 `1.5^attempt` 秒等待，避免重试风暴。对于 429 用更长的 `5 * 2^attempt`。

3. **JSON 解析失败也要重试。** DeepSeek 有时会在 JSON 外面包一层 markdown 代码块（````json ... ````），或者是缺少闭合括号。先尝试解析，失败则让 DeepSeek 重新生成。

4. **max_steps 钳制。** AI 可能输出不合理的 `max_steps`（如 0 或 1000），钳制到 [5, 50] 范围确保安全。

5. **总重试次数合理。** 网络 + JSON 共最多 4-5 次 API 调用（¥0.02），远低于一次执行失败的成本。

---

## 附：模块结构总览

```
lib/prompt_builder.py  (~250 行)
│
├── 常量
│   ├── DANGEROUS_PATTERNS / WARNING_PATTERNS  # 敏感词规则
│   ├── SYSTEM_PROMPT                          # DeepSeek system prompt
│   └── CACHE_DIR = "~/.shopify/prompt_cache/"
│
├── 内部函数
│   ├── _normalize(text) -> str                # 文本标准化
│   ├── _cache_key(desc) -> str                # MD5 缓存键
│   ├── _cache_get(key) -> list[dict] | None   # 读缓存
│   ├── _cache_set(key, phases) -> None        # 写缓存
│   ├── _call_deepseek(messages, api_key) -> dict  # API 调用 + 重试
│   ├── _parse_and_validate(raw) -> list[dict]     # JSON 解析 + 校验
│   ├── _scan_safety(desc) -> (blocked, dangers, warnings)
│   └── _save_phases(phases, desc) -> Path     # 保存到 generated/
│
├── PhaseBuilder 类
│   ├── __init__(model, max_steps, temperature, no_cache)
│   └── build(description) -> list[dict]
│
├── 公开函数
│   └── build_phases(description, **kwargs) -> list[dict]  # 便捷接口
│
└── __main__ 块 (CLI)
    ├── argparse 参数解析
    ├── 安全扫描
    ├── 缓存查询
    ├── API 调用 + 解析
    ├── 人工确认 (y/n)
    └── 输出 phases JSON
```

---

## 附：与现有代码的集成点

| 集成点 | 现有代码 | 变更 |
|--------|---------|------|
| API Key 获取 | `lib/config.py` → `load_feishu_secrets()` → `deepseek.api_key` | 无需变更 |
| phases 消费 | `lib/task_runner.py` → `run_agent(cdp_url, phases)` | 无需变更 |
| phases 消费 | `lib/auto_phase_runner.py` → `AutoPhaseRunner.run(task)` | 无需变更（当前传 `full_task` 字符串，也可以改传分阶段 phases） |
| phases 模板 | `shopify_sop.py` → `PHASES` 常量 | 互补关系：模板是静态版本，prompt_builder 是动态生成 |
| 飞书读取 | `lib/task_runner.py` → `read_prompt(sheet_id)` | 不冲突：飞书 prompt 可以被 AI 当作 input 描述 |
