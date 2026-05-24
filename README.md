# AI 自动化 SaaS

> 基于 browser-use 的多租户网页自动化平台 —— 自然语言驱动，AI 自主执行

```
"帮我注册 Cursor"  ──→  对话引擎  ──→  Agent 自主操作浏览器  ──→  完成
```

## 架构

```
┌─────────────────────────────────────────────────────────┐
│ 🖥  入口层 (core/)                                        │
│   shopify_sop_runner  ·  new_checkout_api  ·  daemon    │
├─────────────────────────────────────────────────────────┤
│ 🧠 编排层                                                │
│   task_runner (通用任务引擎)  ·  batch_scheduler          │
│   conversation_engine (对话式任务入口)                    │
├─────────────────────────────────────────────────────────┤
│ 🤖 执行层                                                │
│   auto_phase_runner  ·  prompt_builder  ·  llm_client   │
│   captcha_detector  ·  user_interaction                  │
├─────────────────────────────────────────────────────────┤
│ 🔌 基础设施层                                             │
│   infra (AdsPower+SSH)  ·  feishu (飞书API)              │
│   llm_health  ·  notifier  ·  watchdog                   │
├─────────────────────────────────────────────────────────┤
│ 💾 数据层                                                │
│   data_service (SQLite/可插拔)  ·  config  ·  feishu     │
└─────────────────────────────────────────────────────────┘
```

## 目录结构

```
ai-automation-saas/
├── lib/          核心库（19 个模块，~6,800 行）
├── core/         主入口和服务
├── scripts/      运维和工具脚本
├── deploy/       部署配置（nginx/systemd/docker）
├── docs/         设计和架构文档
└── data/         本地数据（SQLite）
```

## 核心能力

- **自然语言驱动**：对话式定义任务，AI 自动拆解为可执行步骤
- **多模型支持**：BU-30b（本地免费）主执行 + DeepSeek（规划）+ Claude（复杂场景）
- **智能容错**：CAPTCHA 自动检测 + AdsPower 打码插件 + 用户交互通道
- **自适应控制**：Token 预算管理、GPU 健康观测、Vision 降级
- **多租户就绪**：DataService 抽象层，SQLite → PostgreSQL 一行切换

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 对话式创建任务
python3 -c "
from lib.data_service import SQLiteDataService
from lib.conversation_engine import ConversationEngine
import asyncio

ds = SQLiteDataService()
engine = ConversationEngine(ds)
resp = asyncio.run(engine.process('default_user', '帮我注册 Cursor'))
print(resp.action, resp.reply)
"
```

## 外部依赖

| 系统 | 用途 |
|------|------|
| AdsPower | 指纹浏览器管理 |
| BU-30b (llama.cpp) | 本地 LLM 执行 |
| DeepSeek API | 任务拆解 + 自然语言翻译 |
| Claude API | 复杂场景驱动 |
| 飞书 | 可选数据源（密钥/注册资料） |

## 设计原则

本项目遵循**钱学森工程控制论**六大原理：反馈控制 · 前馈控制 · 最优控制 · 自适应控制 · 自组织控制 · 综合集成
