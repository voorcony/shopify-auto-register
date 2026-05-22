"""
RegistrationManager — 飞书注册资料表统一读写封装

控制论意义：「被控系统的状态观测器 + 执行器」
封装 T8Za6f 表（注册资料+状态）的所有读写，提供列名映射和幂等写入。

用法::

    from lib.registration_manager import RegistrationManager
    rm = RegistrationManager()
    record = rm.get_registration("k1cl8tvk")
    rm.update_status("k1cl8tvk", "registered")
"""

from __future__ import annotations

import re
import traceback

from lib import feishu

# 飞书实际列名 → 标准化字段名
COLUMN_MAP: dict[str, str] = {
    "使用状态": "status",
    "配置文件名称": "config_name",
    "邮箱": "email",
    "邮箱密码": "email_password",
    "shopify密码": "shopify_password",
    "店铺名": "shop_name",
    "店铺域名": "store_domain",
    "仪表盘链接": "dashboard_url",
    "First_Name": "first_name",
    "Last_Name": "last_name",
    "Address": "address",
    "City": "city",
    "State": "state",
    "ZIP": "zip",
    "TEL": "phone",
    "SSN": "ssn",
}

# 表头顺序 → 列字母（用于写入新行）
HEADER_ORDER: list[str] = [
    "使用状态",      # A
    "配置文件名称",   # B
    "邮箱",          # C
    "邮箱密码",       # D
    "shopify密码",    # E
    "店铺名",         # F
    "店铺域名",       # G
    "仪表盘链接",     # H
    "First_Name",    # I
    "Last_Name",     # J
    "Address",       # K
    "City",          # L
    "State",         # M
    "ZIP",           # N
    "TEL",           # O
    "SSN",           # P
]

# 标准化字段名 → 飞书列名（COLUMN_MAP 的反向映射）
FIELD_TO_HEADER: dict[str, str] = {v: k for k, v in COLUMN_MAP.items()}


class RegistrationManager:
    """飞书注册资料表 T8Za6f 的统一读写封装。

    控制论意义：
        系统不需要知道飞书列名（"使用状态"）或行号（row_index），
        只需要知道 profile_id 和语义化的字段名（"status"）。
        RM 承担「状态观测器 + 执行器」双重角色：
            - 读：列名映射、自动提取 profile_id
            - 写：幂等（状态相同不重复写）、profile_id 直接索引
    """

    def __init__(self) -> None:
        fc = feishu._feishu_conf()  # noqa: SLF001
        self._sheet_token: str = fc["sheet_token"]
        self._sheet_id: str = fc["sheets"]["registration"]
        # 缓存 {profile_id: {"row_index": int, "record": dict}}
        self._row_cache: dict[str, dict] = {}
        self._all_records: list[dict] | None = None

    # ── 内部工具 ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_profile_id(config_name: str) -> str:
        """从"配置文件名称"列提取 AdsPower profile_id。

        值格式如 ``"Shopify-KYLEE-admin (k1cl8tvk)"`` → ``"k1cl8tvk"``。
        若无括号格式，回退为 config_name 本身（去空白）。
        """
        m = re.search(r"\(([^)]+)\)", config_name)
        return m.group(1) if m else config_name.strip()

    def _map_row(self, headers: list[str], row: list) -> dict:
        """将飞书原生行（按列索引）映射为标准化 dict。

        处理富文本/链接类型（list/dict → str），
        自动提取 profile_id 并注入到返回字典中。
        """
        record: dict[str, str] = {}
        for i, hdr in enumerate(headers):
            hdr = hdr.strip()
            value = row[i] if i < len(row) else ""
            # 飞书富文本/链接单元格可能是 dict 或 list
            if isinstance(value, dict):
                value = value.get("text", "")
            elif isinstance(value, list):
                value = "".join(
                    item.get("text", "") if isinstance(item, dict) else str(item)
                    for item in value
                )
            key = COLUMN_MAP.get(hdr, hdr)
            record[key] = str(value).strip() if value else ""
        # 自动提取 profile_id
        raw_name = record.get("config_name", "")
        pid = self._extract_profile_id(raw_name)
        if pid:
            record["profile_id"] = pid
        return record

    def _fetch_all(self) -> list[dict]:
        """获取全表记录并构建行号缓存。"""
        if self._all_records is not None:
            return self._all_records

        token = feishu._get_token()  # noqa: SLF001
        url = (
            f"{feishu.API_BASE}/sheets/v2/spreadsheets/{self._sheet_token}"
            f"/values/{self._sheet_id}!A1:P60"
        )
        try:
            r = feishu._http_request(  # noqa: SLF001
                "GET", url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        except Exception:
            traceback.print_exc()
            return self._all_records or []
        data = r.json()
        values = data.get("data", {}).get("valueRange", {}).get("values", [])

        if not values:
            return []

        headers = [str(c) for c in values[0]]
        records: list[dict] = []
        self._row_cache.clear()

        for i, row in enumerate(values[1:], start=1):
            if not row or not any(cell for cell in row if cell):
                continue
            rec = self._map_row(headers, row)
            pid = rec.get("profile_id", "")
            records.append(rec)
            if pid:
                data_idx = i - 1  # 0-based → update_feishu_status 语义
                self._row_cache[pid] = {"row_index": data_idx, "record": rec}

        self._all_records = records
        return records

    def _invalidate_cache(self) -> None:
        """写操作后清空缓存，下次读会重新拉飞书。"""
        self._all_records = None
        self._row_cache.clear()

    # ── 读接口 ─────────────────────────────────────────────────────────

    def get_registration(self, profile_id: str) -> dict | None:
        """获取指定 profile 的注册资料（标准化字段名）。"""
        if profile_id in self._row_cache:
            return self._row_cache[profile_id]["record"]
        self._fetch_all()
        entry = self._row_cache.get(profile_id)
        return entry["record"] if entry else None

    def get_all_registrations(self) -> list[dict]:
        """获取全部记录（标准化列名格式）。"""
        self._fetch_all()
        return self._all_records or []

    # ── 添加新记录 ─────────────────────────────────────────────────────

    def add_registration(self, data: dict) -> dict | None:
        """在表格末尾追加一条注册资料。

        Args:
            data: 标准化字段名的 dict，如::

                {
                    "config_name": "unfarmy-admin (k1ccivrq)",
                    "email": "admin@unfarmy.shop",
                    "email_password": "ZHOUjiahao1!",
                    "shopify_password": "ZJHhewly@2025",
                    "shop_name": "UNFARMY",
                }

            所有字段可选，未提供的字段留空。

        Returns:
            {"row_index": int, "record": dict} 若成功，None 若失败。
        """
        # 1. 读到最新数据，确定末尾行号
        old_records = self._fetch_all()
        # 飞书行号 = 数据行数 + 1（表头行）+ 1（新行）
        next_row = len(old_records) + 2

        # 2. 按 HEADER_ORDER 构建行数据
        row_values: list[str] = []
        for header in HEADER_ORDER:
            field = COLUMN_MAP.get(header, header)
            val = data.get(field, "")
            row_values.append(str(val) if val else "")

        # 3. 写入飞书
        col_letter = chr(ord("A") + len(HEADER_ORDER) - 1)  # 末列字母
        range_str = f"{self._sheet_id}!A{next_row}:{col_letter}{next_row}"

        token = feishu._get_token()  # noqa: SLF001
        url = f"{feishu.API_BASE}/sheets/v2/spreadsheets/{self._sheet_token}/values"
        body = {
            "valueRange": {
                "range": range_str,
                "values": [row_values],
            }
        }
        try:
            r = feishu._http_request(  # noqa: SLF001
                "PUT", url,
                headers={**feishu._write_headers()},  # noqa: SLF001
                json=body,
                timeout=15,
            )
        except Exception:
            traceback.print_exc()
            return None

        if r.json().get("code") != 0:
            return None

        # 4. 构建标准化 record 并加入缓存
        record = self._map_row(HEADER_ORDER, row_values)
        pid = record.get("profile_id", "")
        data_idx = next_row - 2
        self._invalidate_cache()
        if pid:
            self._row_cache[pid] = {"row_index": data_idx, "record": record}
        return {"row_index": data_idx, "record": record}

    # ── 写接口（幂等） ─────────────────────────────────────────────────

    def update_status(self, profile_id: str, status: str) -> bool:
        """更新指定 profile 的状态（幂等）。

        幂等逻辑：
            1. 当前状态已 == 目标状态 → 跳过，返回 True
            2. 写飞书 PUT（通过 feishu.update_feishu_status）
            3. 成功后更新本地缓存
        """
        if profile_id not in self._row_cache:
            self._fetch_all()
        entry = self._row_cache.get(profile_id)
        if not entry:
            return False

        row_idx = entry["row_index"]
        current = entry["record"].get("status", "").strip()

        if current == status.strip():
            return True

        ok = feishu.update_feishu_status(row_idx, status)
        if ok:
            entry["record"]["status"] = status
        return ok

    def update_dashboard(self, profile_id: str, url: str) -> bool:
        """更新仪表盘链接（幂等）。"""
        if not url:
            return True
        if profile_id not in self._row_cache:
            self._fetch_all()
        entry = self._row_cache.get(profile_id)
        if not entry:
            return False

        row_idx = entry["row_index"]
        current = entry["record"].get("dashboard_url", "").strip()

        if current == url.strip():
            return True

        ok = feishu.update_dashboard_url(row_idx, url)
        if ok:
            entry["record"]["dashboard_url"] = url
        return ok

    # ── 委派接口 ───────────────────────────────────────────────────────

    @staticmethod
    def get_customers(n: int = 15) -> list[dict]:
        """获取客户资料（委托给 fetch_customer_pool）。"""
        return feishu.fetch_customer_pool()

    def get_config_key(self, name: str) -> str | None:
        """从 81b55a 密钥表读取指定密钥的值。"""
        token = feishu._get_token()  # noqa: SLF001
        url = (
            f"{feishu.API_BASE}/sheets/v2/spreadsheets/{self._sheet_token}"
            "/values/81b55a!A1:B20"
        )
        try:
            r = feishu._http_request(  # noqa: SLF001
                "GET", url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        except Exception:
            traceback.print_exc()
            return None
        data = r.json()
        values = data.get("data", {}).get("valueRange", {}).get("values", [])
        for row in values:
            if not row:
                continue
            key = str(row[0]).strip().lower()
            if key == name.lower():
                val = row[1] if len(row) > 1 else ""
                if isinstance(val, dict):
                    val = val.get("text", "")
                elif isinstance(val, list):
                    val = "".join(
                        item.get("text", "") if isinstance(item, dict) else str(item)
                        for item in val
                    )
                return str(val).strip()
        return None
