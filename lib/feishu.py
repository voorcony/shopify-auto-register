"""
Feishu operations module — unified for Shopify automation system.

变更要点:
- P1-4: 配置字段改为懒加载（首次访问时才读 config.load()），避免模块导入即崩
- P1-4: 提供 module-level __getattr__ 兼容 `feishu.FEISHU_APP_ID` 等旧用法
- P1-4: 所有 HTTP 请求加指数退避重试（1s/2s/4s，最多 3 次）
- P1-9: customer pool 缓存改到 ~/customer_pool_cache.json + 0o600 权限
- P1-10: customer pool 不再独立 shuffle 各列，按整行抽样保证地址一致

Exported functions:
    read_registration_data()      → list[dict]
    read_prompt()                 → str
    fetch_customer_pool()         → list[dict] (15 整行抽样)
    sync_experience_to_local()    → int (patch count)
    update_feishu_status(idx, s)  → bool
    update_dashboard_url(idx, u)  → bool
    upload_experience()           → bool
"""

from __future__ import annotations

import json
import os
import random
import stat
import time
import traceback

import requests

from lib import config as _cfg

# ── 常量 ────────────────────────────────────────────────────────────────
API_BASE = "https://open.feishu.cn/open-apis"
LOCAL_EXPERIENCE_FILE = "/home/agentuser/shopify_experience.json"
# P1-9: 缓存挪出 /tmp（防止 PII 泄漏到所有用户可读位置）
CUSTOMER_CACHE_FILE = os.path.join(
    os.path.expanduser("~"), "customer_pool_cache.json"
)

# 重试参数：1s, 2s, 4s 指数退避
_HTTP_RETRY_SLEEPS = (1, 2, 4)


# ── 懒加载配置访问 ─────────────────────────────────────────────────────


def _conf() -> dict:
    """每次取最新 config（已带缓存，几乎零成本）"""
    return _cfg.load()


def _feishu_conf() -> dict:
    return _conf().get("feishu", {})


# P1-4: 把原本模块顶层的常量改为延迟解析的属性。
# 通过 PEP-562 module-level __getattr__ 暴露 FEISHU_APP_ID / FEISHU_APP_SECRET /
# SHEET_TOKEN / SHEETS，保持向后兼容。
def __getattr__(name: str):
    if name == "FEISHU_APP_ID":
        return _feishu_conf()["app_id"]
    if name == "FEISHU_APP_SECRET":
        return _feishu_conf()["app_secret"]
    if name == "SHEET_TOKEN":
        return _feishu_conf()["sheet_token"]
    if name == "SHEETS":
        return _feishu_conf()["sheets"]
    raise AttributeError(name)


# ── Token 缓存 ──────────────────────────────────────────────────────────
_token: dict = {"token": None, "expires_at": 0}


def _http_request(method: str, url: str, **kwargs) -> requests.Response:
    """统一封装 HTTP 请求 + 指数退避重试。

    成功条件：requests 调用未抛异常且 status_code 不是 5xx。
    失败将按 1s/2s/4s 重试，最后一次仍失败抛原异常。
    """
    last_exc: Exception | None = None
    for i, sleep in enumerate((0, *_HTTP_RETRY_SLEEPS)):
        if sleep:
            time.sleep(sleep)
        try:
            r = requests.request(method, url, **kwargs)
            # 5xx 触发重试；4xx 由调用方解析 JSON 时处理
            if 500 <= r.status_code < 600 and i < len(_HTTP_RETRY_SLEEPS):
                last_exc = RuntimeError(
                    f"Feishu HTTP {r.status_code} for {url}: {r.text[:200]}"
                )
                continue
            return r
        except requests.RequestException as e:
            last_exc = e
            if i >= len(_HTTP_RETRY_SLEEPS):
                raise
    # 不应到达
    if last_exc:
        raise last_exc
    raise RuntimeError(f"_http_request fell through for {url}")


def _get_token() -> str:
    """Get a valid tenant_access_token, refreshing if expired."""
    now = time.time()
    if _token["token"] and now < _token["expires_at"] - 60:
        return _token["token"]

    fc = _feishu_conf()
    r = _http_request(
        "POST",
        f"{API_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": fc["app_id"], "app_secret": fc["app_secret"]},
        timeout=10,
    )
    data = r.json()
    # 校验 token 响应包含 'tenant_access_token'
    if "tenant_access_token" not in data:
        raise RuntimeError(
            f"Feishu token response missing 'tenant_access_token': {data}"
        )
    _token["token"] = data["tenant_access_token"]
    _token["expires_at"] = now + data.get("expire", 7200)
    return _token["token"]


def _headers() -> dict[str, str]:
    """Headers with Bearer token (no Content-Type for GET)."""
    return {"Authorization": f"Bearer {_get_token()}"}


def _write_headers() -> dict[str, str]:
    """Headers with Bearer token + JSON content type for PUT."""
    h = _headers()
    h["Content-Type"] = "application/json"
    return h


def _extract_cell(cell) -> str:
    """Extract plain text from a Feishu cell (handles rich text lists)."""
    if isinstance(cell, str):
        return cell
    if isinstance(cell, list):
        return "".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in cell
        )
    return str(cell)


# ── Public API ──────────────────────────────────────────────────────────


def read_registration_data() -> list[dict]:
    """Read Feishu sheet T8Za6f, return list of profile dicts."""
    fc = _feishu_conf()
    sheet_token = fc["sheet_token"]
    sheet_id = fc["sheets"]["registration"]
    try:
        r = _http_request(
            "GET",
            f"{API_BASE}/sheets/v2/spreadsheets/{sheet_token}/values/{sheet_id}!A1:N50",
            headers=_headers(),
            timeout=15,
        )
        data = r.json()
        values = data.get("data", {}).get("valueRange", {}).get("values", [])
        if not values:
            return []
        headers = values[0]
        records = []
        for row in values[1:]:
            if not row:
                continue
            rec: dict[str, str] = {}
            for i, hdr in enumerate(headers):
                rec[hdr] = row[i] if i < len(row) else ""
            records.append(rec)
        return records
    except Exception:
        traceback.print_exc()
        return []


def read_prompt() -> str:
    """Read Feishu sheet 0Uq2PS (automation prompt). Returns cell A2 contents."""
    fc = _feishu_conf()
    sheet_token = fc["sheet_token"]
    sheet_id = fc["sheets"]["prompt"]
    try:
        r = _http_request(
            "GET",
            f"{API_BASE}/sheets/v2/spreadsheets/{sheet_token}/values/{sheet_id}!A1:A2",
            headers=_headers(),
            timeout=15,
        )
        data = r.json()
        values = data.get("data", {}).get("valueRange", {}).get("values", [])
        if len(values) >= 2 and len(values[1]) > 0:
            return values[1][0]
        return ""
    except Exception:
        traceback.print_exc()
        return ""


def fetch_customer_pool() -> list[dict]:
    """Read Feishu sheet 8jt5vs, return 15 randomly selected ROWS (not columns).

    P1-10: 不再独立 shuffle 各列！这会造假地址（如纽约+加州邮编）。
    现在的策略：保留每行完整数据，从 N 行中随机抽 15 行。
    手机号会做脱敏（去掉 +1 和 -），email 仍然随机生成。
    """
    fc = _feishu_conf()
    sheet_token = fc["sheet_token"]
    sheet_id = fc["sheets"]["customer_pool"]
    try:
        r = _http_request(
            "GET",
            f"{API_BASE}/sheets/v2/spreadsheets/{sheet_token}/values/{sheet_id}!A1:G400",
            headers=_headers(),
            timeout=15,
        )
        data = r.json()
        values = data.get("data", {}).get("valueRange", {}).get("values", [])

        if len(values) < 2:
            return _load_customer_cache()

        # 按行解析，保留每行内部的姓名/地址/城市/州/邮编一一对应
        rows: list[dict] = []
        for row in values[1:]:
            if len(row) < 7:
                continue
            first = str(row[0]).strip()
            last = str(row[1]).strip()
            addr = str(row[2]).strip()
            city = str(row[3]).strip()
            state = str(row[4]).strip()
            zipc = str(row[5]).strip()
            tel = str(row[6]).strip().replace("+1 ", "").replace("-", "")
            if not first:
                continue
            rows.append({
                "first": first,
                "last": last,
                "addr": addr,
                "city": city,
                "state": state,
                "zip": zipc,
                "tel": tel,
            })

        if not rows:
            return _load_customer_cache()

        # 随机抽取 15 行（保持行内字段对应）
        n = min(15, len(rows))
        picked = random.sample(rows, n)

        customers: list[dict] = []
        used_phones: set[str] = set()
        for p in picked:
            phone = p["tel"]
            # 简单去重；同号则附加随机后缀
            if phone in used_phones:
                phone = f"{phone}{random.randint(10, 99)}"
            used_phones.add(phone)
            email_name = (
                f"{p['first'].lower()}.{p['last'].lower()}"
                f"{random.randint(1000, 9999)}"
            )
            customers.append({
                "first": p["first"],
                "last": p["last"],
                "addr": p["addr"],
                "city": p["city"],
                "state": p["state"],
                "zip": p["zip"],
                "tel": phone,
                "email": f"{email_name}@example.com",
            })

        _save_customer_cache(customers)
        return customers

    except Exception:
        traceback.print_exc()
        return _load_customer_cache()


def _load_customer_cache() -> list[dict]:
    """Load cached customer pool from disk."""
    try:
        with open(CUSTOMER_CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_customer_cache(customers: list[dict]) -> None:
    """持久化客户池缓存到 ~/customer_pool_cache.json，权限 0o600。"""
    try:
        with open(CUSTOMER_CACHE_FILE, "w") as f:
            json.dump(customers, f, indent=2, ensure_ascii=False)
        # P1-9: 严格权限，避免 PII 泄漏给同机其他用户
        os.chmod(CUSTOMER_CACHE_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass  # non-critical


def sync_experience_to_local() -> int:
    """Pull experience patches from sheet 11xbRm, save to local JSON."""
    fc = _feishu_conf()
    sheet_token = fc["sheet_token"]
    sheet_id = fc["sheets"]["experience"]
    try:
        r = _http_request(
            "GET",
            f"{API_BASE}/sheets/v2/spreadsheets/{sheet_token}/values/{sheet_id}!A1:D100",
            headers=_headers(),
            timeout=15,
        )
        data = r.json()
        values = data.get("data", {}).get("valueRange", {}).get("values", [])

        if len(values) < 2:
            return 0

        patches: list[dict] = []
        for row in values[1:]:
            if not row or not row[0]:
                continue
            trigger = str(row[0])
            patch = _extract_cell(row[1]) if len(row) > 1 else ""
            hits = int(row[2]) if len(row) > 2 and str(row[2]).isdigit() else 0
            patches.append({"trigger": trigger, "patch": patch, "hits": hits})

        with open(LOCAL_EXPERIENCE_FILE, "w") as f:
            json.dump(patches, f, indent=2, ensure_ascii=False)

        return len(patches)

    except Exception:
        traceback.print_exc()
        return 0


def update_feishu_status(row_index: int, status: str) -> bool:
    """Update cell in T8Za6f with status code (column A)."""
    fc = _feishu_conf()
    sheet_token = fc["sheet_token"]
    sheet_id = fc["sheets"]["registration"]
    sheet_row = row_index + 2  # row 1 is header
    try:
        body = {
            "valueRange": {
                "range": f"{sheet_id}!A{sheet_row}:A{sheet_row}",
                "values": [[status]],
            }
        }
        r = _http_request(
            "PUT",
            f"{API_BASE}/sheets/v2/spreadsheets/{sheet_token}/values",
            headers=_write_headers(),
            json=body,
            timeout=10,
        )
        return r.json().get("code") == 0
    except Exception:
        traceback.print_exc()
        return False


def update_dashboard_url(row_index: int, url: str) -> bool:
    """Update the dashboard URL field (column H) in registration sheet T8Za6f."""
    fc = _feishu_conf()
    sheet_token = fc["sheet_token"]
    sheet_id = fc["sheets"]["registration"]
    sheet_row = row_index + 2
    try:
        body = {
            "valueRange": {
                "range": f"{sheet_id}!H{sheet_row}:H{sheet_row}",
                "values": [[url]],
            }
        }
        r = _http_request(
            "PUT",
            f"{API_BASE}/sheets/v2/spreadsheets/{sheet_token}/values",
            headers=_write_headers(),
            json=body,
            timeout=10,
        )
        return r.json().get("code") == 0
    except Exception:
        traceback.print_exc()
        return False


def add_experience_patch(trigger: str, patch: str,
                         category: str = "auto_generated") -> bool:
    """追加一条经验补丁到飞书经验表（自适应控制：从失败中学习）。

    控制论原理：当系统在某阶段反复失败，自动把失败模式归纳为一条经验记录
    写回知识库，下一轮启动时该经验会进入 prompt，引导 Agent 规避旧坑。
    人机共同维护这张表 —— 自动产物会被打 ``category=auto_generated``
    便于人工二次审查。

    实现细节：飞书表格无 append API，采用"先 GET 取当前行数 → PUT 到下一行"
    的增量方式。失败不抛异常，返回 False 即可（自动经验丢一条不致命）。

    Args:
        trigger: 触发条件，例如 ``"auto_nurture_failed_3x"``。
        patch: 经验描述（中文/英文均可）。
        category: 分类标签；``registration`` / ``nurturing`` / ``general``
            / ``auto_generated``。

    Returns:
        True if append succeeded, False otherwise.
    """
    fc = _feishu_conf()
    sheet_token = fc["sheet_token"]
    sheet_id = fc["sheets"]["experience"]

    if not trigger or not patch:
        return False

    # 1) GET 当前表格内容，确定下一空行
    try:
        r = _http_request(
            "GET",
            f"{API_BASE}/sheets/v2/spreadsheets/{sheet_token}/values/{sheet_id}!A1:D200",
            headers=_headers(),
            timeout=15,
        )
        data = r.json()
        values = data.get("data", {}).get("valueRange", {}).get("values", []) or []
        # 找最后一行非空行（按 trigger 列 A 是否非空判断）
        last_used_row = 1  # 第 1 行视为表头
        for i, row in enumerate(values, start=1):
            if row and str(row[0]).strip():
                last_used_row = i
        next_row = last_used_row + 1  # 下一空行
    except Exception:
        traceback.print_exc()
        return False

    # 2) PUT 到下一空行
    try:
        body = {
            "valueRange": {
                "range": f"{sheet_id}!A{next_row}:D{next_row}",
                "values": [[trigger, patch, "0", category]],
            }
        }
        r = _http_request(
            "PUT",
            f"{API_BASE}/sheets/v2/spreadsheets/{sheet_token}/values",
            headers=_write_headers(),
            json=body,
            timeout=15,
        )
        return r.json().get("code") == 0
    except Exception:
        traceback.print_exc()
        return False


def read_pending_profiles(target_phase: str | None = None) -> list[dict]:
    """读取注册表，筛选出"应当执行 target_phase"的所有 profile。

    控制论意义（第3轮·自组织控制）：
        batch_scheduler 通过此函数从共享状态表（飞书）拉取任务清单，
        无需人工逐个指定 --profile。多 worker 通过此清单自主分工。

    判定逻辑：
        1. 读取整张注册表
        2. 对每行：根据"状态/使用状态/Status"列推断已完成阶段
        3. 通过 scheduler.next_phase() 算出该 profile 的下一阶段
        4. 若 target_phase 指定，则只保留下一阶段 == target_phase 的行
        5. 跳过 status 含 "registering" / "in_progress" 等运行中标记的行
           （避免重复触发 / 资源竞争）

    Args:
        target_phase: 目标阶段名（register/nurture/import/payment/setup）。
            None 表示返回所有有下一阶段可跑的 profile。

    Returns:
        list of {"profile_id": ..., "next_phase": ..., "current_status": ...,
                 "record": <原始飞书记录>}
    """
    # 局部导入避免循环依赖（scheduler 不依赖 feishu，故安全）
    from lib import scheduler as _sched

    records = read_registration_data()
    if not records:
        return []

    running_markers = (
        "registering", "setting_up", "installing_syncee",
        "importing_products", "nurturing", "registering_payment",
        "running", "in_progress",
    )

    out: list[dict] = []
    for rec in records:
        pid = (rec.get("profile_id") or rec.get("AdsPower ID")
               or rec.get("adspower_id") or rec.get("AdsPower")
               or rec.get("profile") or "")
        pid = str(pid).strip()
        if not pid:
            continue

        status = (rec.get("status") or rec.get("Status")
                  or rec.get("使用状态") or rec.get("状态") or "")
        status = str(status).strip()

        # 跳过运行中状态
        if status.lower() in running_markers:
            continue

        completed = _sched.infer_completed_phase(status)
        nxt = _sched.next_phase(completed)
        if nxt is None:
            continue  # 终态
        if target_phase and nxt != target_phase:
            continue

        out.append({
            "profile_id": pid,
            "next_phase": nxt,
            "current_status": status,
            "completed_phase": completed,
            "record": rec,
        })
    return out


def upload_experience() -> bool:
    """Push local experience patches back to Feishu sheet 11xbRm."""
    fc = _feishu_conf()
    sheet_token = fc["sheet_token"]
    sheet_id = fc["sheets"]["experience"]

    # Read local
    try:
        with open(LOCAL_EXPERIENCE_FILE) as f:
            patches: list[dict] = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return False

    # Classify
    reg_triggers: set[str] = {
        "credit_card_or_payment_page",
        "existing_tabs",
        "blank_page_or_timeout",
        "cloudflare_verification",
        "registration_complete_check",
        "push_to_store_then_verify_products",
        "close_browser_on_success",
        "screenshot_format",
        "address_autocomplete_input",
    }

    header = ["trigger", "patch", "hits", "category"]
    rows: list[list[str]] = [header]

    for p in patches:
        trigger = p.get("trigger", "")
        cat = "general"
        if "nurturing" in trigger:
            cat = "nurturing"
        elif trigger in reg_triggers:
            cat = "registration"
        rows.append([
            trigger,
            p.get("patch", ""),
            str(p.get("hits", 0)),
            cat,
        ])

    try:
        body = {
            "valueRange": {
                "range": f"{sheet_id}!A1:D{len(rows)}",
                "values": rows,
            }
        }
        r = _http_request(
            "PUT",
            f"{API_BASE}/sheets/v2/spreadsheets/{sheet_token}/values",
            headers=_write_headers(),
            json=body,
            timeout=15,
        )
        return r.json().get("code") == 0
    except Exception:
        traceback.print_exc()
        return False
