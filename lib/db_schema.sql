-- ============================================================
-- DataService SQLite Schema
-- 控制论意义：本地化的「被控系统状态库」
-- 6 张表：用户资料 / 对话 / 任务 / 模板 / 记忆 / 任务结果
-- ============================================================

-- ① 用户资料（替代飞书 T8Za6f 的 key-value 抽象）
CREATE TABLE IF NOT EXISTS user_data (
    user_id      TEXT NOT NULL,
    key          TEXT NOT NULL,
    value        TEXT,
    category     TEXT DEFAULT 'general',
    is_sensitive INTEGER DEFAULT 0,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, key)
);

CREATE INDEX IF NOT EXISTS idx_user_data_category
    ON user_data(user_id, category);

-- ② 对话消息历史
CREATE TABLE IF NOT EXISTS conversations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,          -- user / assistant / system
    content    TEXT NOT NULL,
    metadata   TEXT,                   -- JSON
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_conv_session
    ON conversations(user_id, session_id, created_at);

-- ③ 任务记录（包括 phases / 状态 / token 用量）
CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,     -- UUID4
    user_id      TEXT NOT NULL,
    task_type    TEXT,
    description  TEXT,
    phases       TEXT,                 -- JSON
    status       TEXT DEFAULT 'pending',  -- pending / running / done / failed
    result       TEXT,
    ads_profile  TEXT,
    token_usage  TEXT,                 -- JSON
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_user
    ON tasks(user_id, created_at DESC);

-- ④ 任务模板（AI 从历史对话中学到的可复用 prompt 模板）
CREATE TABLE IF NOT EXISTS task_templates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type       TEXT NOT NULL,
    required_fields TEXT,              -- JSON: ["email","password","name"]
    prompt_template TEXT,
    is_global       INTEGER DEFAULT 0,
    created_by      TEXT,
    usage_count     INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ⑤ 用户记忆（偏好、习惯、长期偏好）
CREATE TABLE IF NOT EXISTS user_memories (
    user_id    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT,
    category   TEXT DEFAULT 'general',
    priority   INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, key)
);

-- ⑥ 任务结果（执行后的产出：dashboard URL、店铺信息、屏幕截图路径等）
CREATE TABLE IF NOT EXISTS task_results (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_task_results
    ON task_results(task_id);
