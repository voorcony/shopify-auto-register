"""
通用任务引擎 — AdsPower 启动 + SSH 隧道 + Agent 执行 + 飞书读写
==============================================================
提供与具体业务无关的底层能力：
  ① 密钥加载（从飞书 81b55a 表）
  ② 飞书读写（参数化 sheet ID）
  ③ AdsPower 浏览器启动/关闭
  ④ SSH 隧道建立
  ⑤ LLM 健康检查
  ⑥ 并行启动（LLM + AdsPower + 隧道）
  ⑦ Agent 执行（AutoPhaseRunner 封装）
  ⑧ 通用重试循环

用法:
  from lib.task_runner import (
      _init_config, read_registration_data, read_prompt, update_feishu_status,
      sync_experience_from_feishu, sync_experience_to_feishu,
      start_adsprofile, _stop_profile, build_tunnel, check_llm_alive,
      _startup_parallel, run_agent, run_with_retry, run_task,
  )
"""

import asyncio, json, socket, sys, time, traceback
from pathlib import Path

from lib.exceptions import UserAbortException

# ─── 路径适配 ────────────────────────────────────
_HERE = Path(__file__).resolve().parent.parent  # /home/ubuntu (repo root)
_HOME = Path.home()  # /home/ubuntu

# ─── 非敏感常量 ──────────────────────────────────
MAX_STEPS = 100
TUNNEL_WAIT = 4.0

# ─── 密钥加载（延迟到 _init_config）─────────────
_feishu_app_id = ""
_feishu_secret = ""
_feishu_sheet_token = ""
_adspower_api_key = ""
_adspower_base = ""
_llm_base_url = ""
_ads_server = ""  # AdsPower Windows 服务器 IP
_ads_ssh_pass = ""

# ─── 配置初始化 ──────────────────────────────────

def _init_config():
    """从飞书 81b55a 表 + config.yaml 加载所有密钥和配置。"""
    global _feishu_app_id, _feishu_secret, _feishu_sheet_token
    global _adspower_api_key, _adspower_base, _llm_base_url
    global _ads_server, _ads_ssh_pass

    sys.path.insert(0, str(_HERE))
    from lib import config

    cfg = config.load_feishu_secrets()
    feishu_cfg = cfg.get("feishu", {})
    _feishu_app_id = feishu_cfg.get("app_id", "")
    _feishu_secret = feishu_cfg.get("app_secret", "")
    _feishu_sheet_token = feishu_cfg.get("sheet_token", "")

    adspower_cfg = cfg.get("adspower", {})
    _adspower_api_key = adspower_cfg.get("api_key", "")
    _adspower_base = adspower_cfg.get("base_url", "")

    llm_cfg = cfg.get("llm", {})
    _llm_base_url = llm_cfg.get("base_url", "")

    # AdsPower Windows 服务器
    infra_cfg = cfg.get("infra", {})
    _ads_server = infra_cfg.get("ads_server", "43.155.1.195")
    _ads_ssh_pass = infra_cfg.get("ads_ssh_pass", "ZHOUjiahao1!")


# ─── Feishu 操作 ─────────────────────────────────

def _feishu_token() -> str:
    import requests
    r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                      json={"app_id": _feishu_app_id, "app_secret": _feishu_secret}, timeout=10)
    return r.json()["tenant_access_token"]

def _feishu_headers() -> dict:
    return {"Authorization": f"Bearer {_feishu_token()}"}

def read_registration_data(sheet_id: str) -> list[dict]:
    """读取注册资料表，返回待注册列表"""
    import requests
    h = _feishu_headers()
    r = requests.get(
        f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{_feishu_sheet_token}/values/{sheet_id}!A1:N20",
        headers=h, timeout=10)
    data = r.json()
    values = data.get("data", {}).get("valueRange", {}).get("values", [])
    if not values:
        return []
    headers_row = values[0]
    records = []
    for row in values[1:]:
        if not row:
            continue
        rec = {}
        for i, hdr in enumerate(headers_row):
            rec[hdr] = row[i] if i < len(row) else ""
        records.append(rec)
    return records

def read_prompt(sheet_id: str) -> str:
    """读取飞书自动化 prompt"""
    import requests
    h = _feishu_headers()
    r = requests.get(
        f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{_feishu_sheet_token}/values/{sheet_id}!A1:A2",
        headers=h, timeout=10)
    data = r.json()
    values = data.get("data", {}).get("valueRange", {}).get("values", [])
    if len(values) >= 2 and len(values[1]) > 0:
        return values[1][0]
    return ""

def update_feishu_status(row_index: int, status: str, sheet_id: str, extra: dict = None):
    """更新飞书注册状态"""
    import requests
    h = _feishu_headers()
    h["Content-Type"] = "application/json"
    # Row 1 = header, so data row 1 = sheet row 2
    sheet_row = row_index + 2
    url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{_feishu_sheet_token}/values"
    body = {
        "valueRange": {
            "range": f"{sheet_id}!A{sheet_row}:A{sheet_row}",
            "values": [[status]]
        }
    }
    requests.put(url, headers=h, json=body, timeout=10)


def _extract_patch_text(cell) -> str:
    """从 Feishu 单元格提取纯文本（处理富文本多片段嵌套）"""
    if isinstance(cell, str):
        return cell
    if isinstance(cell, list):
        # 富文本格式：[{'text': '...', 'type': 'text'}, {'text': '...', 'link': '...', 'type': 'url'}]
        parts = []
        for item in cell:
            if isinstance(item, dict):
                parts.append(item.get("text", ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(cell)


def sync_experience_from_feishu(sheet_id: str, experience_file: str) -> int:
    """
    从飞书经验库拉取→覆盖本地 JSON
    返回拉取到的经验条数
    """
    import requests
    h = _feishu_headers()
    r = requests.get(
        f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{_feishu_sheet_token}/values/{sheet_id}!A1:D100",
        headers=h, timeout=10)
    data = r.json()
    values = data.get("data", {}).get("valueRange", {}).get("values", [])
    if len(values) < 2:
        print("   ⚠️  飞书经验库为空", flush=True)
        return 0

    # Header row
    header = values[0]  # [trigger, patch, hits, category]
    patches = []
    for row in values[1:]:
        if not row or not row[0]:
            continue
        trigger = str(row[0])
        patch = _extract_patch_text(row[1]) if len(row) > 1 else ""
        hits = int(row[2]) if len(row) > 2 and str(row[2]).isdigit() else 0
        patches.append({"trigger": trigger, "patch": patch, "hits": hits})

    with open(experience_file, "w") as f:
        json.dump(patches, f, indent=2, ensure_ascii=False)
    print(f"   ✅ 拉取 {len(patches)} 条经验补丁到本地", flush=True)
    return len(patches)


def sync_experience_to_feishu(sheet_id: str, experience_file: str):
    """
    把本地 JSON 推送到飞书经验库
    """
    import requests
    try:
        with open(experience_file) as f:
            patches = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print("   ⚠️  本地经验文件不存在，跳过推送", flush=True)
        return

    h = _feishu_headers()
    h["Content-Type"] = "application/json"

    # 根据 trigger 分类
    reg_triggers = {
        "credit_card_or_payment_page", "existing_tabs", "blank_page_or_timeout",
        "cloudflare_verification", "registration_complete_check",
        "push_to_store_then_verify_products", "close_browser_on_success",
        "screenshot_format", "address_autocomplete_input"
    }

    header = ["trigger", "patch", "hits", "category"]
    rows = [header]
    for p in patches:
        cat = "nurturing" if "nurturing" in p["trigger"] else \
              ("registration" if p["trigger"] in reg_triggers else "general")
        rows.append([p["trigger"], p["patch"], str(p.get("hits", 0)), cat])

    num_rows = len(rows)
    range_str = f"{sheet_id}!A1:D{num_rows}"
    write_url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{_feishu_sheet_token}/values"
    body = {
        "valueRange": {
            "range": range_str,
            "values": rows
        }
    }
    r = requests.put(write_url, headers=h, json=body, timeout=10)
    if r.json().get("code") == 0:
        print(f"   ✅ 推送 {len(patches)} 条经验到飞书", flush=True)
    else:
        print(f"   ⚠️  推送失败: {r.text[:100]}", flush=True)

# ─── AdsPower + Tunnel ──────────────────────────

ADS_SYS_CONFIG = {
    "os": "win",
    "resolution": "1280x1080",
}

def fix_profile_sys(profile_id: str):
    """确保配置的系统设置正确 (OS=win, 1280×1080)"""
    import httpx
    try:
        c = httpx.Client(base_url=_adspower_base, timeout=10)
        c.post("/api/v1/user/update", json={
            "user_id": profile_id,
            "sys": ADS_SYS_CONFIG,
        })
        c.close()
    except Exception:
        pass  # 非关键，不阻塞启动

def start_adsprofile(profile_id: str) -> dict:
    """启动 AdsPower 配置，返回 CDP 信息"""
    import httpx
    c = httpx.Client(base_url=_adspower_base, timeout=30)
    try:
        r = c.get("/api/v1/browser/start", params={
            "user_id": profile_id, "open_tabs": "1", "api_key": _adspower_api_key
        })
        data = r.json()
        if data.get("code") != 0:
            raise Exception(f"AdsPower start failed: {data}")
        ws = data.get("data", {}).get("ws", {})
        cdp = ws.get("puppeteer", "")
        debug_port = data.get("data", {}).get("debug_port", "")
        return {"cdp_url": cdp, "debug_port": debug_port, "raw": data}
    finally:
        c.close()

def _stop_profile(profile_id, base_url, api_key):
    """关闭 AdsPower 浏览器（内部使用，需传入密钥）"""
    import httpx
    try:
        c = httpx.Client(base_url=base_url, timeout=10)
        c.get("/api/v1/browser/stop", params={"user_id": profile_id, "api_key": api_key})
        c.close()
        print("   ✅ 浏览器已关闭", flush=True)
    except Exception as e:
        print(f'   ⚠️ 关浏览器失败: {e}', flush=True)

def stop_profile(profile_id: str):
    """关闭 AdsPower 浏览器（使用已加载的全局密钥）"""
    _stop_profile(profile_id, _adspower_base, _adspower_api_key)

def build_tunnel(cdp_url: str) -> tuple[int, str, None]:
    """建 SSH 隧道到 AdsPower — 使用 autossh 自动重连，返回 (local_port, local_cdp, None)"""
    import re, urllib.parse
    parsed = urllib.parse.urlparse(cdp_url)
    remote_port = parsed.port
    if not remote_port:
        m = re.search(r":(\d+)(?:/|$)", cdp_url)
        if m:
            remote_port = int(m.group(1))
        else:
            raise Exception(f"Cannot parse port from CDP: {cdp_url}")

    # Use same port locally for simplicity
    local_port = remote_port

    # Kill any stale tunnel on this port
    import subprocess as _sp
    _sp.run(["fuser", "-k", f"{local_port}/tcp"], capture_output=True, timeout=5)
    time.sleep(2)  # wait for port release

    # Build SSH tunnel directly (simple ssh -L, no autossh — CDP tunnel is short-lived)
    ssh_cmd = [
        "sshpass", "-p", _ads_ssh_pass,
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=30",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
        "-o", "TCPKeepAlive=yes",
        "-L", f"{local_port}:127.0.0.1:{remote_port}",
        "-N", "-f",
        f"administrator@{_ads_server}",
    ]
    _sp.run(ssh_cmd, capture_output=True, text=True, timeout=20)
    time.sleep(2)

    # Build CDP URL with new local port
    local_cdp = cdp_url.replace(str(remote_port), str(local_port))
    # Also replace host if needed
    if ":51919" in local_cdp or cdp_url.startswith("ws://") and "127.0.0.1" not in local_cdp:
        local_cdp = f"ws://127.0.0.1:{local_port}/devtools/browser/{cdp_url.split('/devtools/browser/')[-1]}"

    # Verify tunnel works
    for _verify_try in range(3):
        import socket as _sock
        _s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        _s.settimeout(3)
        _result = _s.connect_ex(("127.0.0.1", local_port))
        _s.close()
        if _result == 0:
            break
        print(f"   ⏳ 隧道未就绪，重试中... ({_verify_try+1}/3)", flush=True)
        time.sleep(3)
    else:
        raise Exception(f"CDP tunnel verification failed: port {local_port} not reachable")

    return local_port, local_cdp, None

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]

def check_llm_alive() -> bool:
    """检查 LLM 服务 (BU-30b) 是否可用"""
    import httpx
    try:
        r = httpx.get(f"{_llm_base_url}/models", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


# ─── 并行启动 ⚡ ──────────────────────────────────

async def _startup_parallel(profile_id: str):
    """并行执行 LLM 检查 + AdsPower 启动 + SSH 隧道。

    LLM 健康检查独立，AdsPower 启动后立即触发隧道建立。
    预期：串行 6-11s → 并行 3-5s。

    返回: (llm_ok, cdp_info, local_port, local_cdp, tunnel_proc)
    任一步失败抛出异常 → 调用方终止本次 attempt。
    """
    import httpx

    async def _check_llm_async():
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"{_llm_base_url}/models")
                return r.status_code == 200
        except Exception:
            return False

    async def _start_ads_async():
        loop = asyncio.get_event_loop()
        fix_profile_sys(profile_id)
        return await loop.run_in_executor(None, start_adsprofile, profile_id)

    # ── Phase 1: LLM 检查 + AdsPower 启动 并行 ──
    llm_task = asyncio.create_task(_check_llm_async())
    ads_task = asyncio.create_task(_start_ads_async())

    llm_ok, cdp_info = await asyncio.gather(llm_task, ads_task)

    if not llm_ok:
        raise Exception("LLM 服务不可用 (BU-30b)")

    # ── Phase 2: SSH 隧道 (依赖 AdsPower 结果) ──
    loop = asyncio.get_event_loop()
    local_port, local_cdp, tunnel_proc = await loop.run_in_executor(
        None, build_tunnel, cdp_info["cdp_url"]
    )

    return llm_ok, cdp_info, local_port, local_cdp, tunnel_proc

# ─── Agent 执行 ─────────────────────────────────────

async def run_agent(cdp_url: str, phases: list[dict], task_id: str = ""):
    """按任务阶段运行 browser-use — 自动分阶段避免 16K 上下文溢出。

    使用 lib/auto_phase_runner.AutoPhaseRunner 自动在 ~18 步切阶段。
    保留原始参数签名以实现向后兼容。
    """
    from lib.auto_phase_runner import AutoPhaseRunner

    # 将多个 phase 的 task 合并为一个完整任务
    full_task_parts = []
    dashboard_url = None
    for i, phase in enumerate(phases):
        name = phase.get("name", f"step-{i}")
        task = phase.get("task", "")
        full_task_parts.append(f"=== {name.upper()} ===\n{task}")

    full_task = "\n\n".join(full_task_parts)
    print(f"   📄 合并任务: {len(full_task)} chars, {len(phases)} 阶段", flush=True)

    # LLM 配置 (从 config 读取) — 模型名必须匹配 llama-server 实际模型
    llm_config = dict(
        model="browser-use/bu-30b-a3b-preview-Q4_K_M.gguf",  # / 触发生成 1KB 精简 prompt
        base_url=_llm_base_url,
        api_key="not-needed",
        temperature=0.1,
        max_completion_tokens=8192,
        # add_schema_to_system_prompt / dont_force_structured_output 已不需要
        # browser-use/ 前缀自动加载 270 token 原生 prompt
    )

    runner = AutoPhaseRunner(
        llm_config=llm_config,
        cdp_url=cdp_url,
        viewport={"width": 1280, "height": 1080},
        max_steps_per_phase=18,
        max_phases=max(len(phases) * 2, 6),  # 每个阶段最多拆 2 次
        verbose=True,
        task_id=task_id,
    )

    result = await runner.run(full_task)

    # 向后兼容：从 result 中提取 history 和 final
    class CompatHistory:
        """包装 AutoPhaseRunner 返回值为兼容的 history 对象"""
        def __init__(self, runner_result):
            self._result = runner_result

        def final_result(self) -> str | None:
            return self._result.get("final_result") or None

        def is_done(self) -> bool:
            return self._result.get("success", False)

        def is_goal_achieved(self) -> bool:
            return self._result.get("success", False)

        def __len__(self) -> int:
            return self._result.get("total_steps", 0)

        def action_names(self) -> list:
            names = []
            for p in self._result.get("phases", []):
                if "history" in p:
                    try:
                        if hasattr(p["history"], "action_names"):
                            names.extend(p["history"].action_names())
                    except Exception:
                        pass
            return names

        @property
        def usage(self):
            u = self._result.get("total_tokens", {})
            return type('Usage', (), {
                "total_prompt_tokens": u.get("prompt", 0),
                "total_completion_tokens": u.get("completion", 0),
                "total_tokens": u.get("total", 0),
            })

    history = CompatHistory(result)
    final = result.get("final_result", "")

    # 提取 dashboard URL (兼容旧代码)
    if final:
        import re as _re
        url_match = _re.search(r'(https?://admin\.shopify\.com[^\s,)]+)', str(final))
        if url_match:
            dashboard_url = url_match.group(1)
            print(f"   🔗 Dashboard: {dashboard_url}", flush=True)

    total = result.get("total_tokens", {})
    print(f"\n{'='*52}", flush=True)
    print(f" 📊 总: {result.get('total_steps', 0)} 步, "
          f"{result.get('phases_completed', 0)} 阶段, "
          f"{result.get('total_elapsed', 0):.0f}s, "
          f"{total.get('total', 0):,} tokens", flush=True)
    print(f"{'='*52}", flush=True)

    return history, final


# ─── 通用重试循环 ──────────────────────────────────

def run_with_retry(profile_id: str, phases: list[dict], max_retries: int = 3):
    """通用重试循环：启动 AdsPower + SSH 隧道 → 运行 Agent → 失败自动重试。

    Args:
        profile_id: AdsPower profile ID
        phases: list of phase dicts (from build_phases)
        max_retries: 最大重试次数 (默认 3)

    Returns:
        (success: bool, history, final: str, error: Exception|None)
    """
    from pathlib import Path
    STATUS_FILE = Path("/tmp/.run_task_status")

    def _status(msg: str):
        STATUS_FILE.write_text(msg)
        print(msg, flush=True)

    last_error = None

    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            _status(f"🔄 第 {attempt}/{max_retries} 次重试...")

        # ── 并行启动: LLM 检查 + AdsPower + SSH 隧道 ⚡ ──
        _status(f"🔍 检查 LLM + 启动 AdsPower + 隧道... (第{attempt}次)")
        try:
            llm_ok, cdp_info, local_port, local_cdp, tunnel_proc = (
                asyncio.run(_startup_parallel(profile_id))
            )
            _status(f"✅ LLM 可达 | AdsPower 已启动 | 隧道: {local_port}")
        except Exception as e:
            _status(f"❌ 启动失败: {e}")
            try:
                _stop_profile(profile_id, _adspower_base, _adspower_api_key)
            except Exception:
                pass
            last_error = e
            continue

        # ── 组装 prompt + 跑 agent ──
        phase_names = [p['name'] for p in phases]
        _status(f"🤖 Agent 执行中 ({len(phases)} 阶段: {phase_names})...")

        try:
            history, final = asyncio.run(run_agent(local_cdp, phases, task_id=profile_id))
            result = final or "无结果"
            _status(f"✅ 全部阶段完成! {str(result)[:200]}")
            return (True, history, final, None)
        except UserAbortException:
            _status("🛑 用户中止任务")
            return (False, None, "用户中止", None)
        except Exception as e:
            last_error = e
            _status(f"❌ Agent 执行出错 (第{attempt}次): {e}")
            traceback.print_exc()

            if attempt < max_retries:
                _status(f"🔧 准备重试...")
                _stop_profile(profile_id, _adspower_base, _adspower_api_key)

    _status(f"❌ 重试耗尽: {last_error}")
    return (False, None, None, last_error)


# ─── 通用任务入口（SaaS 核心 API）────────────────────────

def run_task(task_description: str, profile_id: str,
             sheet_id: str = "T8Za6f") -> tuple:
    """通用任务入口：自然语言 → DeepSeek 拆解 → 飞书数据注入 → 执行。

    SaaS 架构的核心函数。用户只需一句话，系统全自动处理：
    ① DeepSeek 将 NL 拆解为结构化 phases
    ② PlaceholderResolver 从飞书注入真实注册数据
    ③ run_with_retry 接管 AdsPower 启动/隧道/Agent 执行/重试

    进度通过 /tmp/.run_task_status 文件对外输出，解决管道缓冲问题。

    Args:
        task_description: 自然语言任务描述，如 "帮我在 GOAT 注册买家号"
        profile_id: AdsPower profile ID，如 "k1csb91c"
        sheet_id: 飞书注册资料 sheetId（默认 T8Za6f）

    Returns:
        (success, history, final, error) — 同 run_with_retry
    """
    from lib.prompt_builder import build_phases
    from lib.placeholder_resolver import resolve_phases
    from pathlib import Path

    STATUS_FILE = Path("/tmp/.run_task_status")

    def _status(msg: str):
        """写状态到文件和 stdout"""
        STATUS_FILE.write_text(msg)
        print(msg, flush=True)

    # ── 1. 初始化配置 ──
    _status("🔑 加载配置...")
    _init_config()
    _status(f"✅ 配置加载完成 | profile={profile_id} | 任务={task_description[:40]}...")

    # ── 2. 读取飞书注册资料 ──
    _status("📋 读取飞书注册资料...")
    try:
        from lib.registration_manager import RegistrationManager
        rm = RegistrationManager()
        profile_data = rm.get_registration(profile_id) or {}
        if profile_data:
            email = profile_data.get("email", "N/A")
            _status(f"✅ 找到资料: {email}")
        else:
            _status(f"⚠️ 未找到 profile {profile_id} 的资料，使用默认值")
    except Exception as e:
        _status(f"⚠️ 读取飞书失败 ({e})，使用默认值")
        profile_data = {}

    # ── 3. DeepSeek 拆解 + 飞书数据注入 ──
    _status("🤖 DeepSeek 正在拆解任务...")
    try:
        phases = build_phases(task_description)
        phase_names = [p['name'] for p in phases]
        _status(f"✅ DeepSeek 生成 {len(phases)} 个阶段: {phase_names}")
    except Exception as e:
        _status(f"❌ DeepSeek 拆解失败: {e}")
        return (False, None, None, e)

    _status("🔗 注入飞书注册数据...")
    try:
        phases = resolve_phases(phases, profile_data)
        _status(f"✅ 占位符已替换")
    except Exception as e:
        _status(f"⚠️ 占位符注入失败 ({e})，使用原始 phases")

    # ── 4. 执行 ──
    _status("⚡ 启动 AdsPower + SSH 隧道...")
    return run_with_retry(profile_id, phases)
