#!/usr/bin/env python3.12
"""
经验库同步工具 — 飞书 ↔ 本地 shopify_experience.json
=================================================
用法:
  python3 sync_experience.py pull     # 飞书→本地
  python3 sync_experience.py push     # 本地→飞书
  python3 sync_experience.py status   # 看状态
  python3 sync_experience.py          # 默认 pull
"""
import json, sys, requests

APP_ID = "cli_a9619830e2fadcd1"
APP_SECRET = "kXdwL8yJZCDo9kwych0npgZ5W078RRkK"
TOKEN = "IRFqsUM7Jh4Hybt96ZVc9e0Antc"
EXP_SHEET = "11xbRm"  # 经验库 sheetId
LOCAL_FILE = "/home/agentuser/shopify_experience.json"


def _token():
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10)
    return r.json()["tenant_access_token"]


def _headers():
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


def _extract(cell):
    if isinstance(cell, str):
        return cell
    if isinstance(cell, list):
        return "".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in cell)
    return str(cell)


def pull():
    """飞书 → 本地"""
    print("⬇️  从飞书拉取经验...", flush=True)
    h = _headers()
    r = requests.get(
        f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{TOKEN}/values/{EXP_SHEET}!A1:D100",
        headers=h, timeout=10)
    values = r.json().get("data", {}).get("valueRange", {}).get("values", [])
    if len(values) < 2:
        print("⚠️  飞书经验库为空", flush=True)
        return

    patches = []
    for row in values[1:]:
        if not row or not row[0]:
            continue
        patches.append({
            "trigger": str(row[0]),
            "patch": _extract(row[1]) if len(row) > 1 else "",
            "hits": int(row[2]) if len(row) > 2 and str(row[2]).isdigit() else 0
        })

    with open(LOCAL_FILE, "w") as f:
        json.dump(patches, f, indent=2, ensure_ascii=False)
    print(f"✅  已拉取 {len(patches)} 条经验 → {LOCAL_FILE}", flush=True)


def push():
    """本地 → 飞书"""
    print("⬆️  推送经验到飞书...", flush=True)
    try:
        with open(LOCAL_FILE) as f:
            patches = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print(f"❌ 本地文件 {LOCAL_FILE} 不存在或无效", flush=True)
        return

    reg_triggers = {
        "credit_card_or_payment_page", "existing_tabs", "blank_page_or_timeout",
        "cloudflare_verification", "registration_complete_check",
        "push_to_store_then_verify_products", "close_browser_on_success",
        "screenshot_format", "address_autocomplete_input"
    }

    rows = [["trigger", "patch", "hits", "category"]]
    for p in patches:
        cat = "nurturing" if "nurturing" in p["trigger"] else \
              ("registration" if p["trigger"] in reg_triggers else "general")
        rows.append([p["trigger"], p["patch"], str(p.get("hits", 0)), cat])

    h = _headers()
    range_str = f"{EXP_SHEET}!A1:D{len(rows)}"
    body = {
        "valueRange": {
            "range": range_str,
            "values": rows
        }
    }
    r = requests.put(
        f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{TOKEN}/values",
        headers=h, json=body, timeout=10)
    resp = r.json()
    if resp.get("code") == 0:
        print(f"✅  已推送 {len(patches)} 条经验 → 飞书", flush=True)
    else:
        print(f"❌ 推送失败: {resp.get('msg', r.text[:200])}", flush=True)


def status():
    """显示两边状态"""
    print("📊 经验库同步状态:\n", flush=True)

    # 本地
    try:
        with open(LOCAL_FILE) as f:
            local = json.load(f)
        print(f"  本地: {len(local)} 条经验", flush=True)
    except:
        print(f"  本地: ❌ 文件不存在", flush=True)
        local = []

    # 飞书
    remote_triggers = set()
    try:
        h = _headers()
        r = requests.get(
            f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{TOKEN}/values/{EXP_SHEET}!A1:D100",
            headers=h, timeout=10)
        values = r.json().get("data", {}).get("valueRange", {}).get("values", [])
        non_empty = sum(1 for v in values[1:] if v and v[0] and isinstance(v[0], str) and v[0].strip())
        print(f"  飞书: {non_empty} 条经验", flush=True)
        for row in values[1:]:
            if row and row[0] and isinstance(row[0], str) and row[0].strip():
                remote_triggers.add(str(row[0]))
    except Exception as e:
        print(f"  飞书: ❌ {e}", flush=True)

    # 对比
    local_triggers = {p["trigger"] for p in local}
    only_local = local_triggers - remote_triggers
    only_remote = remote_triggers - local_triggers
    if only_local:
        print(f"  ⚠️  仅本地有: {', '.join(only_local)}", flush=True)
    if only_remote:
        print(f"  ⚠️  仅飞书有: {', '.join(only_remote)}", flush=True)
    if not only_local and not only_remote:
        print(f"  ✅ 本地和飞书一致", flush=True)


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "pull"
    if action == "pull":
        pull()
    elif action == "push":
        push()
    elif action == "status":
        status()
    else:
        print(f"未知操作: {action}，支持: pull / push / status")
