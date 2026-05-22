"""
LLM 健康观测器 — GPU 显存退化检测 + 隧道健康探针。

控制论意义：
    用可观测量（token 生成速度、隧道可用性）估计不可观测的
    系统状态（VRAM 水位、VPN 连接）。当状态退化时触发恢复信号，
    通过飞书 81b55a 表通知 Windows watchdog 执行恢复操作。
"""

from __future__ import annotations

import time
import traceback
from collections import deque
from typing import Callable

import requests


# ── 飞书信号旗常量 ──────────────────────────────────────────────────────
# 写入 81b55a 表的特定行作为 watchdog 调度的信号
FLAG_ROW = 16       # B16: 重启标志（0=正常, 1=需要重启, 2=重启中, 3=重启失败）
REASON_ROW = 17     # B17: 重启原因（tunnel_down / gpu_degraded / unknown）
TIMESTAMP_ROW = 18  # B18: 最后重启时间戳
HEARTBEAT_ROW = 19  # B19: watchdog 心跳（每次轮询更新时间）


class GpuHealthMonitor:
    """GPU 显存退化观测器。

    原理：
        用 token 生成速度（t/s）作为显存水位的代理指标。
        当滑动平均速度持续低于基线的 30% 时，判定为显存退化。

    钱学森工程控制论：
        用可测量量（token 速度）估计不可测量量（VRAM 水位），
        这是典型的状态观测器设计。

    Usage::

        monitor = GpuHealthMonitor(baseline_tps=50.0)
        monitor.record_call(tokens=512, elapsed_s=6.8)
        if monitor.is_degraded:
            print("VRAM 退化！")
    """

    def __init__(self, baseline_tps: float = 50.0,
                 window_size: int = 5, threshold_ratio: float = 0.3):
        """
        Args:
            baseline_tps: 健康状态下的 token/秒（默认 50，基于 BU-30b Q4_K_M）
            window_size: 滑动窗口大小（最近 N 次调用的平均）
            threshold_ratio: 阈值比例（低于 baseline 的多少触发退化判定）
        """
        self.baseline_tps = baseline_tps
        self.window_size = window_size
        self.threshold_ratio = threshold_ratio
        self._history: deque[float] = deque(maxlen=window_size)
        self._last_call_time: float | None = None
        self._total_tokens = 0
        self._total_time = 0.0

    def record_call(self, tokens: int, elapsed_s: float) -> None:
        """记录一次 LLM 调用的速度和 token 数。

        Args:
            tokens: 本次调用生成的 token 数。
            elapsed_s: 本次调用的耗时（秒）。
        """
        if elapsed_s > 0:
            tps = tokens / elapsed_s
            self._history.append(tps)
            self._total_tokens += tokens
            self._total_time += elapsed_s
            self._last_call_time = time.time()

    @property
    def avg_tps(self) -> float:
        """滑动平均 token 速度。"""
        if not self._history:
            return 0.0
        return sum(self._history) / len(self._history)

    @property
    def overall_tps(self) -> float:
        """全局平均 token 速度。"""
        if self._total_time <= 0:
            return 0.0
        return self._total_tokens / self._total_time

    @property
    def is_degraded(self) -> bool:
        """判定是否显存退化。

        条件：
            1. 至少积累了 3 次调用的数据（窗口不足不判定）
            2. 滑动平均速度 < 基线的 threshold_ratio
            3. 最近一次调用在 5 分钟内（避免过期数据误判）
        """
        if len(self._history) < 3:
            return False
        if self._last_call_time is None:
            return False
        # 超过 5 分钟没调用 → 可能已经恢复了
        if time.time() - self._last_call_time > 300:
            return False
        return self.avg_tps < self.baseline_tps * self.threshold_ratio

    @property
    def degradation_ratio(self) -> float:
        """退化比例（0.0~1.0），1.0 = 完全退化，0.0 = 健康。"""
        if self.baseline_tps <= 0:
            return 0.0
        ratio = 1.0 - (self.avg_tps / self.baseline_tps)
        return max(0.0, min(1.0, ratio))

    def reset(self) -> None:
        """重置观测器（显存恢复后调用）。"""
        self._history.clear()
        self._last_call_time = None
        self._total_tokens = 0
        self._total_time = 0.0


def check_tunnel_health(base_url: str, timeout: float = 3.0) -> tuple[bool, str]:
    """快速探测 Cloudflare 隧道健康状态。

    钱学森前馈控制：在每次 LLM 调用前测量干扰量的实际值。

    Args:
        base_url: LLM 服务的 base URL（如 https://xxx.trycloudflare.com/v1）。
        timeout: HTTP 超时秒数。

    Returns:
        (is_alive, message) — is_alive=True 表示隧道可用。
    """
    if not base_url:
        return False, "no base_url configured"

    test_url = base_url.rstrip("/") + "/models"
    try:
        r = requests.get(test_url, timeout=timeout)
        if r.status_code == 200:
            return True, f"tunnel OK ({r.elapsed.total_seconds():.1f}s)"
        elif r.status_code == 502:
            return False, f"tunnel returned 502 (likely dead)"
        else:
            return False, f"tunnel returned HTTP {r.status_code}"
    except requests.ConnectionError:
        return False, "tunnel: connection refused"
    except requests.Timeout:
        return False, f"tunnel: timeout ({timeout}s)"
    except requests.RequestException as e:
        return False, f"tunnel: {e}"


def check_tunnel_with_fallback(base_url: str, timeout: float = 3.0
                               ) -> tuple[bool, str, str | None]:
    """检测隧道健康，若当前 URL 失效则从飞书 81b55a 表读取新 URL。

    Watchdog 重启 cloudflared 后会将新 tunnel URL 写入 81b55a!B21，
    此函数在健康检测失败时自动尝试读取该新 URL 并更新 config。

    Returns:
        (is_alive, message, working_url | None)
    """
    alive, msg = check_tunnel_health(base_url, timeout)
    if alive:
        return True, msg, base_url

    # 当前 URL 失效 → 尝试从飞书表读取新 URL
    try:
        from lib import feishu
        token = feishu._get_token()
        fc = feishu._feishu_conf()
        sheet_token = fc["sheet_token"]
        url = (
            f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets"
            f"/{sheet_token}/values/81b55a!B21:B21"
        )
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                         timeout=10)
        data = r.json()
        vals = data.get("data", {}).get("valueRange", {}).get("values", [])
        if vals and vals[0]:
            new_url = vals[0][0]
            if isinstance(new_url, dict):
                new_url = new_url.get("text", "")
            elif isinstance(new_url, list):
                new_url = "".join(
                    i.get("text", "") if isinstance(i, dict) else str(i)
                    for i in new_url)
            new_url = str(new_url).strip()
            if new_url:
                # 验证新 URL
                alive2, msg2 = check_tunnel_health(new_url, timeout)
                if alive2:
                    # 新 URL 可用 → 更新 config
                    try:
                        from lib import config as _cfg
                        c = _cfg.load()
                        if "llm" not in c:
                            c["llm"] = {}
                        c["llm"]["base_url"] = new_url
                        _cfg._save(c)
                    except Exception:
                        pass
                    return True, f"new tunnel URL: {new_url}", new_url
    except Exception:
        pass

    return False, msg, None


# ── 信号旗写入函数（通过 lib.feishu） ─────────────────────────────────

def _write_flag_cell(row: int, value: str) -> bool:
    """向 81b55a 表特定行 B 列写入一个值。

    使用已存在的 feishu.update_feishu_status 机制，
    因为 81b55a 和 T8Za6f 在同一个 spreadsheet 中。

    Args:
        row: 行号（1-indexed，含表头）。
        value: 要写入的值。

    Returns:
        True 若写入成功。
    """
    try:
        from lib import feishu
        fc = feishu._feishu_conf()
        token = feishu._get_token()
        sheet_token = fc["sheet_token"]
        url = (
            f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets"
            f"/{sheet_token}/values"
        )
        body = {
            "valueRange": {
                "range": f"81b55a!B{row}:B{row}",
                "values": [[value]],
            }
        }
        r = feishu._http_request(
            "PUT", url,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json=body,
            timeout=10,
        )
        return r.json().get("code") == 0
    except Exception:
        traceback.print_exc()
        return False


def signal_recovery_needed(reason: str) -> bool:
    """向飞书 81b55a 表写入恢复信号。

    Args:
        reason: 重启原因（tunnel_down / gpu_degraded）。

    Returns:
        True 若信号写入成功。
    """
    from lib import feishu
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    ok1 = _write_flag_cell(FLAG_ROW, "1")            # B16=1
    ok2 = _write_flag_cell(REASON_ROW, reason)        # B17=原因
    ok3 = _write_flag_cell(TIMESTAMP_ROW, timestamp)  # B18=时间戳
    return ok1 and ok2 and ok3


def signal_recovery_completed() -> bool:
    """清理恢复信号（watchdog 恢复成功后或手动确认后调用）。"""
    ok = _write_flag_cell(FLAG_ROW, "0")
    return ok


def signal_recovery_failed() -> bool:
    """标记恢复失败（watchdog 重试多次后）。"""
    return _write_flag_cell(FLAG_ROW, "3")


def signal_heartbeat() -> bool:
    """watchdog 心跳（每轮轮询时更新时间戳）。"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return _write_flag_cell(HEARTBEAT_ROW, timestamp)


# ── 全局单例 ───────────────────────────────────────────────────────────

# 系统全局唯一的 GpuHealthMonitor 实例
# shopify_controller 在启动时初始化并注入到各个 phase
_default_monitor: GpuHealthMonitor | None = None


def get_monitor() -> GpuHealthMonitor:
    """获取全局 GPU 健康观测器单例。"""
    global _default_monitor
    if _default_monitor is None:
        _default_monitor = GpuHealthMonitor()
    return _default_monitor


def reset_monitor() -> None:
    """重置全局观测器。"""
    global _default_monitor
    if _default_monitor is not None:
        _default_monitor.reset()
