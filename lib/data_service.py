"""
DataService — 数据访问抽象层
=============================
控制论意义：「系统状态的持久化接口」。
上层业务（对话引擎、任务编排）只依赖 AbstractDataService 接口，
底层存储可插拔（SQLite / PostgreSQL / 飞书）。

当前实现：SQLiteDataService（单文件数据库，零依赖）。

用法::

    from lib.data_service import SQLiteDataService
    ds = SQLiteDataService()                       # 默认 data/conversations.db
    ds.set_user_data("u1", "email", "a@b.com")
    print(ds.get_user_data("u1", "email"))         # 'a@b.com'
    ds.add_message("u1", "s1", "user", "hello")
    print(ds.get_recent_messages("u1", "s1", 10))

设计要点:
  - 自动建表（首次使用即创建）
  - ON CONFLICT upsert（key-value 表幂等写入）
  - is_sensitive 标记密码/SSN 等敏感字段
  - 所有 SQL 使用参数化绑定，避免注入
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# ── 路径常量 ───────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent  # /home/ubuntu
_DEFAULT_DB_PATH = _REPO_ROOT / "data" / "conversations.db"
_SCHEMA_PATH = Path(__file__).resolve().parent / "db_schema.sql"

# ── 敏感字段（默认标记 is_sensitive=1）─────────────────────────
_SENSITIVE_KEYS = {
    "password",
    "shopify_password",
    "email_password",
    "ssn",
    "credit_card",
    "card_number",
    "api_key",
    "secret",
    "token",
}


# =============================================================
# AbstractDataService — 接口定义
# =============================================================

class AbstractDataService(ABC):
    """数据服务抽象基类，定义所有可插拔存储后端必须实现的接口。"""

    # ── 用户资料（替代飞书 T8Za6f）────────────────────────────
    @abstractmethod
    def get_user_data(self, user_id: str, key: str) -> Optional[str]:
        """读取用户单个字段，不存在返回 None。"""

    @abstractmethod
    def set_user_data(
        self,
        user_id: str,
        key: str,
        value: str,
        category: str = "general",
        is_sensitive: Optional[bool] = None,
    ) -> None:
        """写入/更新用户字段（upsert）。is_sensitive=None 时按 _SENSITIVE_KEYS 自动判断。"""

    @abstractmethod
    def get_all_user_data(self, user_id: str) -> dict:
        """读取用户全部字段，返回 {key: value} 字典。"""

    # ── 对话历史 ──────────────────────────────────────────────
    @abstractmethod
    def add_message(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> None:
        """追加一条对话消息。"""

    @abstractmethod
    def get_recent_messages(
        self, user_id: str, session_id: str, limit: int = 20
    ) -> list[dict]:
        """读取最近 N 条消息（按时间正序）。"""

    # ── 任务记录 ──────────────────────────────────────────────
    @abstractmethod
    def create_task(
        self,
        user_id: str,
        task_type: str,
        description: str,
        phases: list,
        ads_profile: str = "",
    ) -> str:
        """创建任务记录，返回 task_id（UUID4）。"""

    @abstractmethod
    def update_task_status(
        self,
        task_id: str,
        status: str,
        result: Optional[str] = None,
        token_usage: Optional[dict] = None,
    ) -> None:
        """更新任务状态。status='done'/'failed' 时同时写入 completed_at。"""

    @abstractmethod
    def get_user_tasks(self, user_id: str, limit: int = 20) -> list[dict]:
        """读取用户最近的任务列表（按 created_at 降序）。"""

    # ── 用户记忆 ──────────────────────────────────────────────
    @abstractmethod
    def remember(
        self,
        user_id: str,
        key: str,
        value: str,
        category: str = "general",
        priority: int = 0,
    ) -> None:
        """持久化一条偏好/习惯记忆（upsert）。"""

    @abstractmethod
    def recall(self, user_id: str, key: str) -> Optional[str]:
        """读取一条记忆，不存在返回 None。"""


# =============================================================
# SQLiteDataService — 具体实现
# =============================================================

class SQLiteDataService(AbstractDataService):
    """基于 SQLite 的数据服务实现。

    特点：
      - 零依赖（标准库 sqlite3）
      - 自动建表（构造时执行 db_schema.sql）
      - 线程安全（每次操作独立 connection；SQLite 文件锁负责并发）
      - upsert 全部使用 ON CONFLICT 语法（SQLite ≥ 3.24）
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = _DEFAULT_DB_PATH
        self.db_path = Path(db_path)
        # 确保父目录存在
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # 线程锁：保护多线程并发写入时的连接重入
        self._lock = threading.Lock()

        # 初始化建表
        self._init_schema()

    # ── 内部工具 ─────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """打开数据库连接，开启外键 + 返回 Row 字典风格。"""
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30,
            isolation_level=None,  # 自动提交模式
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")  # 提升并发读
        return conn

    def _init_schema(self) -> None:
        """读取 db_schema.sql 并执行（CREATE IF NOT EXISTS 幂等）。"""
        if not _SCHEMA_PATH.exists():
            raise FileNotFoundError(f"Schema 文件不存在: {_SCHEMA_PATH}")
        schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        with self._lock, self._connect() as conn:
            conn.executescript(schema_sql)

    @staticmethod
    def _is_sensitive(key: str, explicit: Optional[bool]) -> int:
        """决定 is_sensitive 字段的最终值。"""
        if explicit is not None:
            return 1 if explicit else 0
        # 自动判定：键名包含敏感词
        key_lower = key.lower()
        for token in _SENSITIVE_KEYS:
            if token in key_lower:
                return 1
        return 0

    # ── 用户资料 ─────────────────────────────────────────────

    def get_user_data(self, user_id: str, key: str) -> Optional[str]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM user_data WHERE user_id=? AND key=?",
                (user_id, key),
            ).fetchone()
            return row["value"] if row else None

    def set_user_data(
        self,
        user_id: str,
        key: str,
        value: str,
        category: str = "general",
        is_sensitive: Optional[bool] = None,
    ) -> None:
        sensitive = self._is_sensitive(key, is_sensitive)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_data (user_id, key, value, category, is_sensitive, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, key) DO UPDATE SET
                    value        = excluded.value,
                    category     = excluded.category,
                    is_sensitive = excluded.is_sensitive,
                    updated_at   = CURRENT_TIMESTAMP
                """,
                (user_id, key, value, category, sensitive),
            )

    def get_all_user_data(self, user_id: str) -> dict:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM user_data WHERE user_id=?",
                (user_id,),
            ).fetchall()
            return {r["key"]: r["value"] for r in rows}

    def delete_user_data(self, user_id: str, key: str) -> None:
        """删除用户单个字段（非接口必需，但方便测试和清理）。"""
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM user_data WHERE user_id=? AND key=?",
                (user_id, key),
            )

    # ── 对话历史 ─────────────────────────────────────────────

    def add_message(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> None:
        meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO conversations (user_id, session_id, role, content, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, session_id, role, content, meta_json),
            )

    def get_recent_messages(
        self, user_id: str, session_id: str, limit: int = 20
    ) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, role, content, metadata, created_at
                FROM conversations
                WHERE user_id=? AND session_id=?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, session_id, limit),
            ).fetchall()
        # 反转为时间正序（最旧的在前），便于直接喂给 LLM
        messages = []
        for r in reversed(rows):
            messages.append({
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else None,
                "created_at": r["created_at"],
            })
        return messages

    # ── 任务记录 ─────────────────────────────────────────────

    def create_task(
        self,
        user_id: str,
        task_type: str,
        description: str,
        phases: list,
        ads_profile: str = "",
    ) -> str:
        task_id = str(uuid.uuid4())
        phases_json = json.dumps(phases, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks
                    (id, user_id, task_type, description, phases, status, ads_profile)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (task_id, user_id, task_type, description, phases_json, ads_profile),
            )
        return task_id

    def update_task_status(
        self,
        task_id: str,
        status: str,
        result: Optional[str] = None,
        token_usage: Optional[dict] = None,
    ) -> None:
        token_json = json.dumps(token_usage, ensure_ascii=False) if token_usage else None
        completed_at = (
            datetime.now().isoformat(timespec="seconds")
            if status in ("done", "failed", "completed")
            else None
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status       = ?,
                    result       = COALESCE(?, result),
                    token_usage  = COALESCE(?, token_usage),
                    completed_at = COALESCE(?, completed_at)
                WHERE id = ?
                """,
                (status, result, token_json, completed_at, task_id),
            )

    def get_user_tasks(self, user_id: str, limit: int = 20) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, task_type, description, phases, status, result,
                       ads_profile, token_usage, created_at, completed_at
                FROM tasks
                WHERE user_id=?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        tasks = []
        for r in rows:
            tasks.append({
                "id": r["id"],
                "task_type": r["task_type"],
                "description": r["description"],
                "phases": json.loads(r["phases"]) if r["phases"] else [],
                "status": r["status"],
                "result": r["result"],
                "ads_profile": r["ads_profile"],
                "token_usage": json.loads(r["token_usage"]) if r["token_usage"] else None,
                "created_at": r["created_at"],
                "completed_at": r["completed_at"],
            })
        return tasks

    def get_task(self, task_id: str) -> Optional[dict]:
        """读取单个任务（非接口必需，便于调试）。"""
        with self._lock, self._connect() as conn:
            r = conn.execute(
                """
                SELECT id, user_id, task_type, description, phases, status, result,
                       ads_profile, token_usage, created_at, completed_at
                FROM tasks WHERE id=?
                """,
                (task_id,),
            ).fetchone()
        if not r:
            return None
        return {
            "id": r["id"],
            "user_id": r["user_id"],
            "task_type": r["task_type"],
            "description": r["description"],
            "phases": json.loads(r["phases"]) if r["phases"] else [],
            "status": r["status"],
            "result": r["result"],
            "ads_profile": r["ads_profile"],
            "token_usage": json.loads(r["token_usage"]) if r["token_usage"] else None,
            "created_at": r["created_at"],
            "completed_at": r["completed_at"],
        }

    # ── 用户记忆 ─────────────────────────────────────────────

    def remember(
        self,
        user_id: str,
        key: str,
        value: str,
        category: str = "general",
        priority: int = 0,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_memories (user_id, key, value, category, priority)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, key) DO UPDATE SET
                    value      = excluded.value,
                    category   = excluded.category,
                    priority   = excluded.priority,
                    created_at = CURRENT_TIMESTAMP
                """,
                (user_id, key, value, category, priority),
            )

    def recall(self, user_id: str, key: str) -> Optional[str]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM user_memories WHERE user_id=? AND key=?",
                (user_id, key),
            ).fetchone()
            return row["value"] if row else None

    def list_memories(self, user_id: str, category: Optional[str] = None) -> list[dict]:
        """列出某分类（或全部）的记忆，按 priority 降序。"""
        with self._lock, self._connect() as conn:
            if category:
                rows = conn.execute(
                    """
                    SELECT key, value, category, priority, created_at
                    FROM user_memories
                    WHERE user_id=? AND category=?
                    ORDER BY priority DESC, created_at DESC
                    """,
                    (user_id, category),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT key, value, category, priority, created_at
                    FROM user_memories
                    WHERE user_id=?
                    ORDER BY priority DESC, created_at DESC
                    """,
                    (user_id,),
                ).fetchall()
        return [dict(r) for r in rows]

    # ── 任务结果（key-value 形式，便于扩展）──────────────────

    def add_task_result(
        self, task_id: str, user_id: str, key: str, value: str
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_results (task_id, user_id, key, value)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, user_id, key, value),
            )

    def get_task_results(self, task_id: str) -> dict:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM task_results WHERE task_id=?",
                (task_id,),
            ).fetchall()
        return {r["key"]: r["value"] for r in rows}


# ── 模块级单例（可选便利）─────────────────────────────────────
_default_instance: Optional[SQLiteDataService] = None


def get_default_data_service() -> SQLiteDataService:
    """获取默认单例 SQLiteDataService。"""
    global _default_instance
    if _default_instance is None:
        _default_instance = SQLiteDataService()
    return _default_instance
