"""通知推送模块 — 系统→人的主动告警通道。

控制论意义（第3轮·综合集成）：
    综合集成的人机结合需要双向畅通的反馈通道。当系统检测到异常时，
    应主动通知人，而不是等人来轮询日志。本模块实现"系统→人"方向的
    报警信号（Alarm Signal）。

支持目标：
    1. 飞书 Webhook（自定义机器人）
    2. QQ bot（通过 Hermes HTTP 接口转发）

配置（config.yaml）：
    notifications:
      enabled: true
      feishu_webhook_url: "https://open.feishu.cn/open-apis/bot/v2/hook/<key>"
      qq_hermes_url: "http://127.0.0.1:8080/send"   # 可选
      qq_target: "123456789"                         # 可选：群号或 QQ 号
      min_level: "WARN"      # 低于该级别的告警丢弃（节流）

公共 API：
    notify(title, message, level="INFO")     主入口；自动路由到已配置的通道
    notify_feishu(title, message, level)     直接发飞书
    notify_qq(title, message, level)         直接发 QQ（通过 Hermes）
    alert_phase_failed(profile_id, phase, error)   语义化封装
    alert_phase_success(profile_id, phase)
    alert_batch_summary(stats)
"""

from __future__ import annotations

import json
import os
import time
import traceback
from typing import Any

import requests
import yaml

# ── 配置加载（懒加载，避免模块导入时 IO 错误把整个调用链拉崩）─────────
_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config.yaml",
)

_cfg_cache: dict | None = None


def _cfg() -> dict:
    """懒加载并缓存 config.yaml 的 notifications 段。"""
    global _cfg_cache
    if _cfg_cache is not None:
        return _cfg_cache
    try:
        with open(_CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}
        _cfg_cache = data.get("notifications", {}) or {}
    except Exception:
        _cfg_cache = {}
    return _cfg_cache


# 级别排序（用于 min_level 过滤）
_LEVEL_ORDER = {"INFO": 0, "OK": 0, "WARN": 1, "ERR": 2, "FATAL": 3}


def _level_passes(level: str) -> bool:
    """检查 level 是否达到 min_level 阈值。"""
    cfg = _cfg()
    min_lv = str(cfg.get("min_level", "INFO")).upper()
    return _LEVEL_ORDER.get(level.upper(), 0) >= _LEVEL_ORDER.get(min_lv, 0)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── 飞书 Webhook 推送 ────────────────────────────────────────────────────

_COLOR_MAP = {
    "INFO": "green",
    "OK": "green",
    "WARN": "orange",
    "ERR": "red",
    "FATAL": "red",
}


def notify_feishu(title: str, message: str, level: str = "INFO") -> bool:
    """通过飞书 Webhook 发送告警卡片。

    配置路径: notifications.feishu_webhook_url
    Returns: True if HTTP 2xx and飞书返回 code == 0, else False.
    """
    cfg = _cfg()
    webhook = cfg.get("feishu_webhook_url", "")
    if not webhook:
        return False
    level = level.upper()
    payload: dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"[{level}] {title}",
                },
                "template": _COLOR_MAP.get(level, "green"),
            },
            "elements": [
                {"tag": "markdown", "content": message},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"Shopify Controller · {_now()}",
                        }
                    ],
                },
            ],
        },
    }
    try:
        r = requests.post(webhook, json=payload, timeout=8)
        if r.status_code // 100 != 2:
            return False
        try:
            return r.json().get("code", 0) == 0
        except ValueError:
            return True  # 部分自定义 webhook 不返回 JSON
    except requests.RequestException:
        return False


# ── QQ Bot 推送（经 Hermes 中转）─────────────────────────────────────────

def notify_qq(title: str, message: str, level: str = "INFO") -> bool:
    """通过 Hermes HTTP 接口推送到 QQ。

    Hermes 接口契约假设（可在 config.yaml 调整）：
        POST {qq_hermes_url}
        body: {"target": "<qq_or_group>", "message": "<text>"}
    """
    cfg = _cfg()
    url = cfg.get("qq_hermes_url", "")
    target = str(cfg.get("qq_target", "")).strip()
    if not url or not target:
        return False
    body = f"[{level.upper()}] {title}\n{message}\n— {_now()}"
    try:
        r = requests.post(
            url,
            json={"target": target, "message": body},
            timeout=8,
        )
        return r.status_code // 100 == 2
    except requests.RequestException:
        return False


# ── 主入口：路由到所有已配置通道 ─────────────────────────────────────────

def notify(title: str, message: str, level: str = "INFO") -> dict:
    """主告警入口。自动路由到 config.yaml 中已配置的所有通道。

    Returns:
        {"feishu": bool, "qq": bool, "skipped": bool}
        skipped=True 表示因 enabled=False 或 level 低于 min_level 被丢弃。
    """
    result = {"feishu": False, "qq": False, "skipped": False}
    cfg = _cfg()
    if not cfg.get("enabled", True):
        result["skipped"] = True
        return result
    if not _level_passes(level):
        result["skipped"] = True
        return result

    # 容错：单通道失败不应阻塞另一通道
    try:
        result["feishu"] = notify_feishu(title, message, level)
    except Exception:
        traceback.print_exc()
    try:
        result["qq"] = notify_qq(title, message, level)
    except Exception:
        traceback.print_exc()
    return result


# ── 语义化封装（控制器代码调用更简洁）────────────────────────────────────

def alert_phase_failed(profile_id: str, phase: str,
                       error: str = "", attempts: int = 0) -> dict:
    """阶段失败告警。"""
    msg_lines = [
        f"**Profile**: `{profile_id}`",
        f"**Phase**: `{phase}`",
    ]
    if attempts:
        msg_lines.append(f"**Attempts**: {attempts}")
    if error:
        # 截断超长 traceback，飞书卡片有 30K 上限
        err_short = error if len(error) < 1500 else error[:1500] + "..."
        msg_lines.append(f"**Error**:\n```\n{err_short}\n```")
    return notify(
        title=f"阶段失败 · {phase}",
        message="\n".join(msg_lines),
        level="ERR",
    )


def alert_phase_success(profile_id: str, phase: str,
                        next_phase: str | None = None,
                        delay_hours: int = 0) -> dict:
    """阶段成功告警。"""
    msg_lines = [
        f"**Profile**: `{profile_id}`",
        f"**Phase**: `{phase}` ✅",
    ]
    if next_phase:
        msg_lines.append(
            f"**Next**: `{next_phase}` (in {delay_hours}h)"
            if delay_hours else f"**Next**: `{next_phase}`"
        )
    return notify(
        title=f"阶段完成 · {phase}",
        message="\n".join(msg_lines),
        level="INFO",
    )


def alert_batch_summary(stats: dict) -> dict:
    """批量调度汇总告警。

    Args:
        stats: {"phase": str, "total": int, "success": int,
                "failed": int, "timeout": int, "duration_sec": float,
                "details": [{"profile_id": ..., "status": ...}, ...]}
    """
    phase = stats.get("phase", "?")
    total = stats.get("total", 0)
    succ = stats.get("success", 0)
    fail = stats.get("failed", 0)
    to = stats.get("timeout", 0)
    dur = stats.get("duration_sec", 0)
    level = "ERR" if fail or to else "INFO"

    lines = [
        f"**Phase**: `{phase}`",
        f"**Total**: {total}  ✅ {succ}  ❌ {fail}  ⏱ {to}",
        f"**Duration**: {dur:.1f}s",
    ]
    details = stats.get("details", []) or []
    if details:
        lines.append("**Detail**:")
        for d in details[:20]:  # 防超长
            mark = {
                "success": "✅", "failed": "❌", "timeout": "⏱",
            }.get(str(d.get("status")), "•")
            lines.append(f"- {mark} `{d.get('profile_id')}`")
        if len(details) > 20:
            lines.append(f"... and {len(details) - 20} more")
    return notify(
        title=f"批量调度汇总 · {phase}",
        message="\n".join(lines),
        level=level,
    )
