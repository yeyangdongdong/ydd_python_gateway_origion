"""SQLite 是 Agent、会话、运行记录和消息的唯一存储位置。

这里的方法是同步的；接口层通过线程池调用，不阻塞网络事件循环。
每个写操作使用一个短事务，调用模型期间不占用数据库事务。
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .errors import GatewayError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 数据库结构的版本记录。每项是 (版本号, 该版本要执行的语句列表)。
# 规则：只允许在末尾追加新版本，不要修改已发布的条目——旧数据库就是靠它逐级升级的。
MIGRATIONS: list[tuple[int, tuple[str, ...]]] = [
    (1, (
        """
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            system_prompt TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL REFERENCES agents(id),
            title TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            request_id TEXT NOT NULL,
            input TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('running', 'completed', 'failed', 'interrupted')
            ),
            reply TEXT,
            error_code TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            UNIQUE(session_id, request_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id),
            run_id TEXT NOT NULL REFERENCES runs(id),
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id)",
    )),
]


class Store:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """把数据库升级到 MIGRATIONS 里的最高版本。

        早期版本的数据库没有 schema_version 表，会被当作版本 0；因为第 1 版的
        语句都是 IF NOT EXISTS，对已有数据是安全的，升级不会丢东西。
        """
        db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = db.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            db.execute("INSERT INTO schema_version VALUES (0)")
            current = 0
        else:
            current = row["version"]
        for version, statements in MIGRATIONS:
            if version <= current:
                continue
            for statement in statements:
                db.execute(statement)
            db.execute("UPDATE schema_version SET version = ?", (version,))

    def initialize(self, default_model: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            self._migrate(db)
            # 本项目只支持一个进程。重启后，上一进程未完成的运行明确标为中断。
            db.execute("""
                UPDATE runs SET status = 'interrupted', error_code = 'interrupted',
                    error_message = '服务重启，本轮已中断；请使用新的 request_id 重试。',
                    finished_at = ? WHERE status = 'running'
            """, (now(),))
            db.execute("""
                INSERT OR IGNORE INTO agents VALUES ('default', ?, ?, ?, ?)
            """, ("默认助手", "你是一个友好、准确的中文助手。", default_model, now()))
            # 默认 Agent 跟随环境中的模型；用户创建的 Agent 保留各自配置。
            db.execute("UPDATE agents SET model = ? WHERE id = 'default'", (default_model,))

    def list_agents(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM agents ORDER BY created_at, id LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return [dict(row) for row in rows]

    def create_agent(self, name: str, system_prompt: str, model: str) -> dict:
        agent = dict(id=uuid4().hex, name=name, system_prompt=system_prompt,
                     model=model, created_at=now())
        with self.connect() as db:
            db.execute("INSERT INTO agents VALUES (:id, :name, :system_prompt, :model, :created_at)", agent)
        return agent

    def update_agent(self, agent_id: str, *, name: str | None = None,
                     system_prompt: str | None = None, model: str | None = None) -> dict:
        # 只有调用方明确提供的字段才更新，None 表示保持原值。
        changes = {column: value for column, value in
                   (("name", name), ("system_prompt", system_prompt), ("model", model))
                   if value is not None}
        with self.connect() as db:
            if self._row(db, "SELECT 1 FROM agents WHERE id = ?", agent_id) is None:
                raise GatewayError(404, "agent_not_found", "Agent 不存在。")
            if changes:
                # 列名来自上面的固定白名单，不会把用户输入拼进 SQL。
                assignments = ", ".join(f"{column} = :{column}" for column in changes)
                db.execute(f"UPDATE agents SET {assignments} WHERE id = :id",
                           {**changes, "id": agent_id})
            row = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return dict(row)

    def delete_agent(self, agent_id: str) -> None:
        with self.connect() as db:
            if self._row(db, "SELECT 1 FROM agents WHERE id = ?", agent_id) is None:
                raise GatewayError(404, "agent_not_found", "Agent 不存在。")
            if agent_id == "default":
                raise GatewayError(409, "default_agent", "默认助手由服务自动创建，不能删除。")
            if self._row(db, "SELECT 1 FROM sessions WHERE agent_id = ? LIMIT 1", agent_id):
                raise GatewayError(409, "agent_in_use", "该 Agent 仍有会话，请先删除这些会话。")
            db.execute("DELETE FROM agents WHERE id = ?", (agent_id,))

    def create_session(self, agent_id: str, title: str) -> dict:
        session = dict(id=uuid4().hex, agent_id=agent_id, title=title, created_at=now())
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone():
                raise GatewayError(404, "agent_not_found", "Agent 不存在，请先创建 Agent。")
            db.execute("INSERT INTO sessions VALUES (:id, :agent_id, :title, :created_at)", session)
        return session

    def list_sessions(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM sessions ORDER BY created_at, id LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return [dict(row) for row in rows]

    def update_session(self, session_id: str, *, title: str) -> dict:
        with self.connect() as db:
            if self._row(db, "SELECT 1 FROM sessions WHERE id = ?", session_id) is None:
                raise GatewayError(404, "session_not_found", "会话不存在。")
            db.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
            row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row)

    def delete_session(self, session_id: str) -> None:
        """删除会话及其消息和运行记录；正在生成时拒绝，避免删掉正在写入的轮次。"""
        with self.connect() as db:
            self._require_idle_session(db, session_id)
            db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            db.execute("DELETE FROM runs WHERE session_id = ?", (session_id,))
            db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def clear_messages(self, session_id: str) -> int:
        """清空对话历史，返回删除的消息条数。

        同时删除运行记录：否则旧的 request_id 仍会被当作“可重放”，
        而对应的消息已经不存在，查询结果会自相矛盾。
        """
        with self.connect() as db:
            self._require_idle_session(db, session_id)
            deleted = db.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            ).rowcount
            db.execute("DELETE FROM runs WHERE session_id = ?", (session_id,))
        return deleted

    def get_session(self, session_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("""
                SELECT sessions.*, agents.system_prompt, agents.model
                FROM sessions JOIN agents ON agents.id = sessions.agent_id
                WHERE sessions.id = ?
            """, (session_id,)).fetchone()
        if row is None:
            raise GatewayError(404, "session_not_found", "会话不存在，请先创建会话。")
        return dict(row)

    def messages(self, session_id: str, *, completed_only: bool = False,
                 limit: int | None = None, offset: int = 0) -> list[dict]:
        """按写入顺序读取消息。limit=None 表示读取全部（模型上下文需要完整历史）。"""
        query = """
            SELECT messages.*, runs.status AS run_status
            FROM messages JOIN runs ON runs.id = messages.run_id
            WHERE messages.session_id = ? AND (? = 0 OR runs.status = 'completed')
            ORDER BY messages.id
        """
        parameters: list[object] = [session_id, int(completed_only)]
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            parameters += [limit, offset]
        with self.connect() as db:
            rows = db.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def find_request(self, session_id: str, request_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE session_id = ? AND request_id = ?",
                             (session_id, request_id)).fetchone()
        return dict(row) if row else None

    def get_run(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise GatewayError(404, "run_not_found", "运行记录不存在。")
        return dict(row)

    def start_run(self, session_id: str, request_id: str, content: str) -> str:
        run_id = uuid4().hex
        timestamp = now()
        with self.connect() as db:
            db.execute("""
                INSERT INTO runs (id, session_id, request_id, input, status, created_at)
                VALUES (?, ?, ?, ?, 'running', ?)
            """, (run_id, session_id, request_id, content, timestamp))
            db.execute("""
                INSERT INTO messages (session_id, run_id, role, content, created_at)
                VALUES (?, ?, 'user', ?, ?)
            """, (session_id, run_id, content, timestamp))
        return run_id

    def complete_run(self, session_id: str, run_id: str, reply: str) -> None:
        timestamp = now()
        with self.connect() as db:
            db.execute("""
                INSERT INTO messages (session_id, run_id, role, content, created_at)
                VALUES (?, ?, 'assistant', ?, ?)
            """, (session_id, run_id, reply, timestamp))
            db.execute("""
                UPDATE runs SET status = 'completed', reply = ?, finished_at = ? WHERE id = ?
            """, (reply, timestamp, run_id))

    def fail_run(self, run_id: str, error: GatewayError, *, interrupted: bool = False) -> None:
        with self.connect() as db:
            db.execute("""
                UPDATE runs SET status = ?, error_code = ?, error_message = ?, finished_at = ?
                WHERE id = ? AND status = 'running'
            """, ("interrupted" if interrupted else "failed", error.code, error.message, now(), run_id))

    @staticmethod
    def _row(db: sqlite3.Connection, query: str, *parameters: object) -> sqlite3.Row | None:
        """查询单行；调用方只关心“有 / 没有”时比 fetchone() 更好读。"""
        return db.execute(query, parameters).fetchone()

    def _require_idle_session(self, db: sqlite3.Connection, session_id: str) -> None:
        """确认会话存在且没有正在生成的一轮，供删除和清空复用。"""
        if self._row(db, "SELECT 1 FROM sessions WHERE id = ?", session_id) is None:
            raise GatewayError(404, "session_not_found", "会话不存在。")
        if self._row(db, "SELECT 1 FROM runs WHERE session_id = ? AND status = 'running' LIMIT 1",
                     session_id):
            raise GatewayError(409, "session_busy", "会话正在处理一轮请求，请等这一轮结束后再操作。")
