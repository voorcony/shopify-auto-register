"""配置加载器 — 读取 .env + config.yaml，解析 ${VAR} 占位符 + 校验完整性。

变更要点（P0-3 修复）:
- 启动时先用 python-dotenv 加载 /home/agentuser/.env 到 os.environ
- 加载 config.yaml 原始文本后，先做 os.path.expandvars() 占位符替换
- 校验阶段额外拒绝任何残留的 ${VAR_NAME}（说明 .env 缺该字段）
"""

import os
import re
import sys
import yaml

try:
    # 优先用 python-dotenv 加载 .env；若未安装则降级到手工解析
    from dotenv import load_dotenv as _load_dotenv
except ImportError:  # pragma: no cover — 依赖缺失时的回退
    _load_dotenv = None

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_PATH = os.path.join(_ROOT, "config.yaml")
_ENV_PATH = os.path.join(_ROOT, ".env")

_cache = None
_env_loaded = False


def _ensure_env_loaded() -> None:
    """加载 .env（仅一次）。若 python-dotenv 不可用则手工解析 KEY=VALUE。"""
    global _env_loaded
    if _env_loaded:
        return
    if os.path.exists(_ENV_PATH):
        if _load_dotenv is not None:
            _load_dotenv(_ENV_PATH, override=False)
        else:
            # 手工解析：兼容 # 注释 + KEY=VALUE 形式
            with open(_ENV_PATH) as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    os.environ.setdefault(k, v)
    _env_loaded = True


def load() -> dict:
    """加载配置（带缓存）。会做 .env 加载和 ${VAR} 展开。"""
    global _cache
    if _cache is not None:
        return _cache
    _ensure_env_loaded()
    with open(_CONFIG_PATH) as f:
        raw = f.read()
    # 展开 ${VAR_NAME}（仅匹配未解析的环境变量占位符）
    expanded = os.path.expandvars(raw)
    cfg = yaml.safe_load(expanded)
    _validate(cfg)
    _cache = cfg
    return cfg


def reload() -> dict:
    """强制重新加载（同时重新读 .env）。"""
    global _cache, _env_loaded
    _cache = None
    _env_loaded = False
    return load()


_UNEXPANDED_PATTERN = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def _validate(cfg: dict):
    """校验密钥完整性 + 必需字段 + 拒绝残留占位符。"""
    errors = []

    # 飞书相关
    feishu = cfg.get("feishu", {})
    for key in ["app_secret", "sheet_token"]:
        val = str(feishu.get(key, ""))
        if "..." in val:
            errors.append(f"feishu.{key} 包含 '...' 占位符！请替换为完整密钥")
        if _UNEXPANDED_PATTERN.search(val):
            errors.append(f"feishu.{key} 未解析的占位符: {val}（检查 .env）")

    # AdsPower api_key
    api_key = str(cfg.get("adspower", {}).get("api_key", ""))
    if _UNEXPANDED_PATTERN.search(api_key):
        errors.append(f"adspower.api_key 未解析的占位符: {api_key}（检查 .env）")
    elif "..." in api_key or len(api_key) < 20:
        errors.append(f"adspower.api_key 不完整 ({len(api_key)} chars)")

    # 必需字段
    required = [
        ("feishu.app_id", feishu.get("app_id")),
        ("feishu.app_secret", feishu.get("app_secret")),
        ("adspower.base_url", cfg.get("adspower", {}).get("base_url")),
        ("llm.base_url", cfg.get("llm", {}).get("base_url")),
        ("proxy.host", cfg.get("proxy", {}).get("host")),
    ]
    for path, val in required:
        if not val:
            errors.append(f"缺少必需配置: {path}")

    if errors:
        print("❌ 配置校验失败:", flush=True)
        for e in errors:
            print(f"   • {e}", flush=True)
        sys.exit(1)


# ── 飞书配置表 81b55a 加载 ──────────────────────────────────────────
# 键名映射：飞书表第一列 → config 内部路径（点号分隔）
_FEISHU_KEY_MAP: dict[str, str] = {
    "老薛主机密码": "laoxue.password",
    "面板/FTP账号": "cpanel.user",
    "面板/FTP密码：": "cpanel.password",
    "面板/FTP密码": "cpanel.password",
    "namesiloAPI": "namesilo.api_key",
    "deepseek": "deepseek.api_key",
    "Claude API": "claude.api_key",
    "browseruse": "browseruse.api_key",
}


def load_feishu_secrets(cfg: dict | None = None) -> dict:
    """从飞书 81b55a 表加载密钥，合并到配置缓存。

    需要 config.yaml 中已有完整的 feishu.app_id / app_secret / sheet_token。
    将飞书表第一列当作键名、第二列当作值，按 _FEISHU_KEY_MAP 映射到 config 路径。

    Returns:
        合并后的完整配置 dict。
    """
    global _cache
    if cfg is None:
        cfg = load()

    feishu_cfg = cfg.get("feishu", {})
    app_id = feishu_cfg.get("app_id", "")
    app_secret = feishu_cfg.get("app_secret", "")
    sheet_token = feishu_cfg.get("sheet_token", "")
    if not app_id or not app_secret or not sheet_token:
        print("⚠️  飞书配置不完整，跳过 81b55a 密钥加载", flush=True)
        return cfg

    try:
        import requests as _req

        # 获取飞书 token
        r = _req.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        token_data = r.json()
        token = token_data.get("tenant_access_token", "")
        if not token:
            print(f"⚠️  获取飞书 token 失败: {token_data}", flush=True)
            return cfg

        # 读取 81b55a 表
        url = (
            f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/"
            f"{sheet_token}/values/81b55a!A1:B100"
        )
        r = _req.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        data = r.json()
        values = data.get("data", {}).get("valueRange", {}).get("values", [])
        if not values:
            print("⚠️  飞书 81b55a 表读取为空", flush=True)
            return cfg

        # 逐行匹配键名→config路径
        secrets_found = 0
        for row in values:
            if len(row) < 2:
                continue
            key_raw = str(row[0]).strip()
            val_raw = str(row[1]).strip()
            if not key_raw or not val_raw:
                continue
            # 去掉飞书邮件链接的包装
            if val_raw.startswith("[{"):
                import re as _re
                m = _re.search(r"'text':\s*'([^']+)'", val_raw)
                if m:
                    val_raw = m.group(1)

            config_path = _FEISHU_KEY_MAP.get(key_raw)
            if config_path:
                # 按点号路径设置到 cfg
                parts = config_path.split(".")
                target = cfg
                for p in parts[:-1]:
                    if p not in target:
                        target[p] = {}
                    target = target[p]
                if val_raw and val_raw != "None":
                    target[parts[-1]] = val_raw
                    secrets_found += 1

        if secrets_found:
            print(f"   ✅ 从飞书 81b55a 加载了 {secrets_found} 个密钥", flush=True)

    except Exception as e:
        print(f"   ⚠️  飞书 81b55a 加载失败 (非致命): {e}", flush=True)
        import traceback
        traceback.print_exc()

    _cache = cfg
    return cfg


def get_profile(profile_id: str) -> dict:
    """获取指定 profile 的配置"""
    cfg = load()
    profile = cfg.get("profiles", {}).get(profile_id, {})
    if not profile:
        print(f"⚠️  配置中未找到 profile {profile_id}，使用默认值", flush=True)
    return profile


def get_proxy_id(profile_id: str) -> str:
    """获取 profile 对应的代理唯一ID"""
    cfg = load()
    return cfg.get("proxy", {}).get("profile_proxy_ids", {}).get(profile_id, "")


def refresh_proxy(profile_id: str) -> bool:
    """刷新代理 IP"""
    proxy_id = get_proxy_id(profile_id)
    if not proxy_id:
        print(f"⚠️  未找到 {profile_id} 的代理ID，跳过刷新", flush=True)
        return False
    cfg = load()
    refresh_url = cfg["proxy"]["refresh_url"]
    import requests as _req
    try:
        url = f"{refresh_url}?user=gyd602_{proxy_id}&country=us&state=ca"
        r = _req.get(url, timeout=10)
        result = r.json()
        if result.get("code") == 0:
            print(f"   ✅ 代理 IP 已刷新 (gyd602_{proxy_id})", flush=True)
            return True
        else:
            print(f"   ⚠️  代理刷新返回: {result}", flush=True)
            return False
    except Exception as e:
        print(f"   ⚠️  代理刷新失败: {e}", flush=True)
        return False
