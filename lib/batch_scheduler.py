"""批量调度器 — 多 Profile 并发执行。

控制论意义（第3轮·自组织控制）：
    系统在没有外部指令的情况下，通过内部相互作用形成有序结构。
    多个 Profile 通过共享状态表（飞书 + 本地 checkpoint）自主协调，
    调度器只做资源分配（隧道数、并发槽位），各个 Worker 独立运行、
    失败不互相影响。

设计要点：
    1. Worker 用 subprocess.run 启动 shopify_controller.py 子进程，
       复用现有 CLI，崩溃完全隔离。
    2. TunnelSemaphore 限制并发 SSH 隧道数，防止 AdsPower 服务器过载。
    3. 主循环从飞书读取"待执行"profile，动态分配到 worker。
    4. 每个 worker 完成时：写 checkpoint（由子进程自己写）+
       主进程聚合统计 + notifier 推送告警。
    5. loop=True 时持续轮询新任务，形成"自循环控制系统"。
"""

from __future__ import annotations

import multiprocessing
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

# ── 路径常量 ─────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_CONTROLLER = _ROOT / "shopify_controller.py"

# Worker 退出码约定
EXIT_SUCCESS = 0
EXIT_FAILED = 1
EXIT_TIMEOUT = 2


# ── 资源仲裁器（控制论：资源分配规则）────────────────────────────────────

class TunnelSemaphore:
    """SSH 隧道资源信号量。限制同时活动的隧道数。

    控制论意义：自组织系统需要"共享资源的分配规则"，否则多个 worker
    会同时抢占同一组隧道端口 / 同一台 AdsPower 服务器的资源，
    引发碰撞。本信号量 = 资源仲裁器。

    默认 max_active = 3（AdsPower 服务器负载经验值，可调）。
    """

    def __init__(self, max_active: int = 3) -> None:
        if max_active < 1:
            max_active = 1
        self.max_active = max_active
        self._sem = threading.BoundedSemaphore(max_active)

    def acquire(self, timeout: float | None = None) -> bool:
        return self._sem.acquire(timeout=timeout)

    def release(self) -> None:
        try:
            self._sem.release()
        except ValueError:
            # 已超过 max_active 次 release —— 容错，不抛
            pass


# ── 数据结构 ─────────────────────────────────────────────────────────────

@dataclass
class WorkerResult:
    profile_id: str
    phase: str
    exit_code: int
    duration_sec: float
    status: str = "unknown"   # success / failed / timeout
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass
class BatchStats:
    phase: str
    total: int = 0
    success: int = 0
    failed: int = 0
    timeout: int = 0
    duration_sec: float = 0.0
    details: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "total": self.total,
            "success": self.success,
            "failed": self.failed,
            "timeout": self.timeout,
            "duration_sec": self.duration_sec,
            "details": self.details,
        }


# ── Worker：执行单个 profile 的 phase ────────────────────────────────────

def _worker_entry(profile_id: str, phase_name: str,
                  profile_timeout: int,
                  tunnel_sem: TunnelSemaphore | None = None) -> WorkerResult:
    """运行 shopify_controller.py 的指定命令。

    实现方式：subprocess.run(["python3", "shopify_controller.py",
        phase_name, "--profile", profile_id], timeout=profile_timeout)

    Returns: WorkerResult，含 exit_code 与 status。
    """
    acquired = False
    if tunnel_sem is not None:
        # 等待资源；若 5 分钟仍拿不到，标记失败避免无限阻塞
        acquired = tunnel_sem.acquire(timeout=300)
        if not acquired:
            return WorkerResult(
                profile_id=profile_id,
                phase=phase_name,
                exit_code=EXIT_FAILED,
                duration_sec=0.0,
                status="failed",
                stderr_tail="tunnel semaphore acquire timeout (300s)",
            )

    started = time.time()
    cmd = [
        sys.executable,
        str(_CONTROLLER),
        phase_name,
        "--profile", profile_id,
    ]

    out_tail = ""
    err_tail = ""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=profile_timeout,
            cwd=str(_ROOT),
        )
        # 保留每路 4KB 末尾，便于排错与告警推送
        out_tail = (proc.stdout or "")[-4096:]
        err_tail = (proc.stderr or "")[-4096:]
        rc = proc.returncode
        if rc == 0:
            status = "success"
        else:
            status = "failed"
    except subprocess.TimeoutExpired as e:
        rc = EXIT_TIMEOUT
        status = "timeout"
        out_tail = (e.stdout or b"")[-4096:].decode("utf-8", errors="replace") \
            if isinstance(e.stdout, bytes) else (e.stdout or "")[-4096:]
        err_tail = (
            f"TimeoutExpired after {profile_timeout}s\n"
            + ((e.stderr or b"")[-4000:].decode("utf-8", errors="replace")
               if isinstance(e.stderr, bytes) else (e.stderr or "")[-4000:])
        )
    except Exception as e:
        rc = EXIT_FAILED
        status = "failed"
        err_tail = f"subprocess raised: {e}\n{traceback.format_exc()[-3000:]}"
    finally:
        if acquired and tunnel_sem is not None:
            tunnel_sem.release()

    return WorkerResult(
        profile_id=profile_id,
        phase=phase_name,
        exit_code=rc,
        duration_sec=time.time() - started,
        status=status,
        stdout_tail=out_tail,
        stderr_tail=err_tail,
    )


# ── 主调度循环 ───────────────────────────────────────────────────────────

def _log(msg: str, level: str = "INFO") -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    prefix = {"INFO": "ℹ️ ", "OK": "✅", "WARN": "⚠️ ", "ERR": "❌"}.get(
        level, "  "
    )
    print(f"[{ts}] [batch] {prefix} {msg}", flush=True)


def _discover_tasks(phase_name: str) -> list[dict]:
    """从飞书读取需要执行 phase_name 的所有 profile。"""
    from lib import feishu
    try:
        return feishu.read_pending_profiles(phase_name)
    except Exception as e:
        _log(f"feishu.read_pending_profiles failed: {e}", level="ERR")
        traceback.print_exc()
        return []


def _run_one_batch(phase_name: str, max_workers: int,
                   profile_timeout: int) -> BatchStats:
    """执行一轮批处理（不循环）。"""
    stats = BatchStats(phase=phase_name)
    started = time.time()

    tasks = _discover_tasks(phase_name)
    stats.total = len(tasks)
    if not tasks:
        _log(f"No pending profiles for phase '{phase_name}'", level="INFO")
        stats.duration_sec = time.time() - started
        return stats

    _log(f"Discovered {len(tasks)} profile(s) for phase '{phase_name}': "
         f"{[t['profile_id'] for t in tasks]}", level="INFO")

    # 资源仲裁器
    tunnel_sem = TunnelSemaphore(max_active=max_workers)

    # 用线程池 + subprocess（subprocess 自身是真并行）
    # 这样比 multiprocessing.Pool 更简单且不需 pickle worker
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: list[WorkerResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(
                _worker_entry,
                t["profile_id"], t["next_phase"],
                profile_timeout, tunnel_sem,
            ): t for t in tasks
        }
        for fut in as_completed(futures):
            t = futures[fut]
            pid = t["profile_id"]
            try:
                res = fut.result()
            except Exception as e:
                res = WorkerResult(
                    profile_id=pid,
                    phase=phase_name,
                    exit_code=EXIT_FAILED,
                    duration_sec=0.0,
                    status="failed",
                    stderr_tail=f"future raised: {e}",
                )
            results.append(res)

            # 汇总
            if res.status == "success":
                stats.success += 1
                _log(f"✅ {pid} {res.phase} OK in {res.duration_sec:.1f}s",
                     level="OK")
            elif res.status == "timeout":
                stats.timeout += 1
                _log(f"⏱ {pid} {res.phase} TIMEOUT after "
                     f"{profile_timeout}s", level="ERR")
            else:
                stats.failed += 1
                _log(f"❌ {pid} {res.phase} FAILED rc={res.exit_code} "
                     f"in {res.duration_sec:.1f}s", level="ERR")

            stats.details.append({
                "profile_id": pid,
                "phase": res.phase,
                "status": res.status,
                "exit_code": res.exit_code,
                "duration_sec": round(res.duration_sec, 1),
            })

            # 逐 profile 推送告警（综合集成的"系统→人"通道）
            try:
                from lib import notifier
                if res.status == "success":
                    from lib import scheduler as _sched
                    nxt = _sched.next_phase(res.phase)
                    delay = _sched.phase_delay_hours(res.phase)
                    notifier.alert_phase_success(
                        pid, res.phase,
                        next_phase=nxt, delay_hours=delay,
                    )
                else:
                    notifier.alert_phase_failed(
                        pid, res.phase,
                        error=(res.stderr_tail or res.stdout_tail or "")[-1500:],
                    )
            except Exception as e:
                _log(f"notifier failed for {pid}: {e}", level="WARN")

    stats.duration_sec = time.time() - started
    return stats


def batch_run(phase_name: str, *, max_workers: int = 3,
              profile_timeout: int = 7200,
              loop: bool = False,
              loop_interval_sec: int = 60) -> int:
    """批量执行指定阶段。

    Args:
        phase_name: 阶段名 (register/nurture/import/payment/setup)
        max_workers: 最大并发数，默认 3
        profile_timeout: 单个 profile 超时秒数，默认 7200 (=2h)
        loop: 是否持续循环（每隔 loop_interval_sec 秒检查一次新任务）
        loop_interval_sec: loop 模式下的轮询间隔（默认 60s）

    调度逻辑：
        1. 读取飞书注册表，找出"下一阶段 == phase_name"的 profiles
        2. 用 ThreadPoolExecutor 启动 max_workers 个 worker 并行执行
        3. 每个 worker 通过 subprocess 调用 shopify_controller.py <phase>
           --profile <pid>，由子进程自己写 Feishu 状态 + checkpoint
        4. 所有 worker 完成 → 汇总报告 + 飞书告警
        5. 若 loop=True → 等待 loop_interval_sec → 回到步骤 1
        6. 若上一轮 0 任务 → 仍按节奏轮询（不退出）

    Returns:
        0 = 全部成功 / 无任务  ; 1 = 有失败 / 超时
    """
    if max_workers < 1:
        max_workers = 1
    overall_rc = 0
    round_idx = 0

    while True:
        round_idx += 1
        _log(f"═════ Batch round #{round_idx}  phase={phase_name}  "
             f"workers={max_workers}  timeout={profile_timeout}s ═════",
             level="INFO")
        stats = _run_one_batch(phase_name, max_workers, profile_timeout)

        # 单轮汇总告警
        _log(f"Round #{round_idx} done: total={stats.total} "
             f"OK={stats.success} FAIL={stats.failed} TO={stats.timeout} "
             f"dur={stats.duration_sec:.1f}s",
             level=("OK" if stats.failed == 0 and stats.timeout == 0
                    else "WARN"))
        if stats.total > 0:
            try:
                from lib import notifier
                notifier.alert_batch_summary(stats.to_dict())
            except Exception as e:
                _log(f"batch summary notifier failed: {e}", level="WARN")
            if stats.failed or stats.timeout:
                overall_rc = 1

        if not loop:
            break
        try:
            _log(f"Sleeping {loop_interval_sec}s before next round...",
                 level="INFO")
            time.sleep(loop_interval_sec)
        except KeyboardInterrupt:
            _log("Loop interrupted by user", level="WARN")
            break

    return overall_rc
