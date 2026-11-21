"""SQLite 是 Agent、会话、运行记录和消息的唯一存储位置。

这里的方法是同步的；接口层通过线程池调用，不阻塞网络事件循环。
每个写操作使用一个短事务，调用模型期间不占用数据库事务。
"""

import sqlite3
import threading
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
    (2, (
        """
        CREATE TABLE IF NOT EXISTS audit_log (
            id TEXT PRIMARY KEY,
            ts TEXT NOT NULL,
            trace_id TEXT,
            actor_type TEXT NOT NULL,
            actor_id TEXT,
            actor_name TEXT,
            action TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            result TEXT NOT NULL,
            reason TEXT,
            ip TEXT,
            details TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS audit_log_ts ON audit_log(ts)",
        "CREATE INDEX IF NOT EXISTS audit_log_action ON audit_log(action, ts)",
    )),
    (3, (
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
            created_at TEXT NOT NULL,
            last_seen_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS auth_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            token_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            ip TEXT,
            user_agent TEXT,
            revoked_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions(user_id)",
        "CREATE INDEX IF NOT EXISTS auth_sessions_token ON auth_sessions(token_hash)",
    )),
    (4, (
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            key_prefix TEXT NOT NULL,
            scopes TEXT NOT NULL,
            owner_id TEXT,
            expires_at TEXT,
            created_at TEXT NOT NULL,
            last_used_at TEXT,
            revoked_at TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS api_keys_owner ON api_keys(owner_id)",
    )),
    (5, (
        "ALTER TABLE sessions ADD COLUMN owner_id TEXT",
        "ALTER TABLE agents ADD COLUMN owner_id TEXT",
        "CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(owner_id)",
        "CREATE INDEX IF NOT EXISTS agents_owner ON agents(owner_id)",
    )),
    (6, (
        "ALTER TABLE api_keys ADD COLUMN allowed_cidrs TEXT",
    )),
]


class Store:
    def __init__(self, path: Path, *, pooled: bool = False, pool_size: int = 8):
        self.path = path
        self.pooled = pooled
        self.pool_size = pool_size
        self._pool: list[sqlite3.Connection] = []
        self._pool_lock = threading.Lock()

    def _new_connection(self) -> sqlite3.Connection:
        # pooled 连接会在 anyio 的不同线程间轮流使用，因此放开线程检查；
        # 同一时刻只会被一个调用方借用（由 _pool_lock 保证）。
        connection = sqlite3.connect(self.path, timeout=5,
                                     check_same_thread=not self.pooled)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def connect(self):
        if not self.pooled:
            connection = self._new_connection()
            try:
                with connection:
                    yield connection
            finally:
                connection.close()
            return

        connection = self._borrow()
        try:
            with connection:
                yield connection
        finally:
            self._release(connection)

    def _borrow(self) -> sqlite3.Connection:
        with self._pool_lock:
            if self._pool:
                return self._pool.pop()
        return self._new_connection()

    def _release(self, connection: sqlite3.Connection) -> None:
        with self._pool_lock:
            if len(self._pool) < self.pool_size:
                self._pool.append(connection)
                return
        connection.close()

    def close(self) -> None:
        """关闭池里的空闲连接；lifespan 结束时调用，避免 Windows 上占住文件。"""
        with self._pool_lock:
            pool, self._pool = self._pool, []
        for connection in pool:
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
                INSERT OR IGNORE INTO agents (id, name, system_prompt, model, created_at)
                VALUES ('default', ?, ?, ?, ?)
            """, ("默认助手", "你是一个友好、准确的中文助手。", default_model, now()))
            # 默认 Agent 跟随环境中的模型；用户创建的 Agent 保留各自配置。
            db.execute("UPDATE agents SET model = ? WHERE id = 'default'", (default_model,))

    def list_agents(self, limit: int = 100, offset: int = 0,
                    owner_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM agents"
        parameters: list[object] = []
        if owner_id:
            # 默认助手（owner 为空）对所有人可见，其余只看自己的。
            query += " WHERE owner_id IS NULL OR owner_id = ?"
            parameters.append(owner_id)
        query += " ORDER BY created_at, id LIMIT ? OFFSET ?"
        parameters += [limit, offset]
        with self.connect() as db:
            rows = db.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def get_agent(self, agent_id: str) -> dict:
        with self.connect() as db:
            row = self._row(db, "SELECT * FROM agents WHERE id = ?", agent_id)
        if row is None:
            raise GatewayError(404, "agent_not_found", "Agent 不存在。")
        return dict(row)

    def create_agent(self, name: str, system_prompt: str, model: str,
                     owner_id: str | None = None) -> dict:
        agent = dict(id=uuid4().hex, name=name, system_prompt=system_prompt,
                     model=model, created_at=now(), owner_id=owner_id)
        with self.connect() as db:
            db.execute("""
                INSERT INTO agents (id, name, system_prompt, model, created_at, owner_id)
                VALUES (:id, :name, :system_prompt, :model, :created_at, :owner_id)
            """, agent)
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

    def create_session(self, agent_id: str, title: str,
                       owner_id: str | None = None) -> dict:
        session = dict(id=uuid4().hex, agent_id=agent_id, title=title,
                       created_at=now(), owner_id=owner_id)
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone():
                raise GatewayError(404, "agent_not_found", "Agent 不存在，请先创建 Agent。")
            db.execute("""
                INSERT INTO sessions (id, agent_id, title, created_at, owner_id)
                VALUES (:id, :agent_id, :title, :created_at, :owner_id)
            """, session)
        return session

    def list_sessions(self, limit: int = 100, offset: int = 0,
                      owner_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM sessions"
        parameters: list[object] = []
        if owner_id:
            query += " WHERE owner_id = ?"
            parameters.append(owner_id)
        query += " ORDER BY created_at, id LIMIT ? OFFSET ?"
        parameters += [limit, offset]
        with self.connect() as db:
            rows = db.execute(query, parameters).fetchall()
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

    def get_owned_session(self, session_id: str, owner_id: str | None) -> dict:
        """owner_id 为 None 表示不限制（管理员/服务级 Key）；否则 owner 不匹配按 404。

        这是归属检查的唯一实现：HTTP 路由与 ChatService 都走这里，避免某个入口漏检。
        """
        session = self.get_session(session_id)
        if owner_id is not None and session.get("owner_id") != owner_id:
            raise GatewayError(404, "session_not_found", "会话不存在，请先创建会话。")
        return session

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

    # ------------------------------------------------------------ 审计

    def append_audit(self, event: dict) -> None:
        with self.connect() as db:
            db.execute("""
                INSERT INTO audit_log (id, ts, trace_id, actor_type, actor_id, actor_name,
                                       action, target_type, target_id, result, reason, ip, details)
                VALUES (:id, :ts, :trace_id, :actor_type, :actor_id, :actor_name,
                        :action, :target_type, :target_id, :result, :reason, :ip, :details)
            """, event)

    def list_audit(self, limit: int = 100, offset: int = 0, *, action: str | None = None,
                   result: str | None = None, actor_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM audit_log WHERE 1 = 1"
        parameters: list[object] = []
        for column, value in (("action", action), ("result", result), ("actor_id", actor_id)):
            if value:
                query += f" AND {column} = ?"
                parameters.append(value)
        query += " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?"
        parameters += [limit, offset]
        with self.connect() as db:
            rows = db.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------- 账号与会话

    def count_users(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def create_user(self, username: str, password_hash: str, role: str = "member") -> dict:
        user = dict(id=uuid4().hex, username=username, password_hash=password_hash,
                    role=role, status="active", created_at=now(), last_seen_at=None)
        with self.connect() as db:
            db.execute("""
                INSERT INTO users (id, username, password_hash, role, status, created_at, last_seen_at)
                VALUES (:id, :username, :password_hash, :role, :status, :created_at, :last_seen_at)
            """, user)
        return user

    def find_user(self, *, username: str | None = None,
                  user_id: str | None = None) -> dict | None:
        query, parameter = ("SELECT * FROM users WHERE username = ?", username) \
            if username is not None else ("SELECT * FROM users WHERE id = ?", user_id)
        with self.connect() as db:
            row = db.execute(query, (parameter,)).fetchone()
        return dict(row) if row else None

    def list_users(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, username, role, status, created_at, last_seen_at"
                " FROM users ORDER BY created_at LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return [dict(row) for row in rows]

    def update_user(self, user_id: str, *, password_hash: str | None = None,
                    role: str | None = None, status: str | None = None) -> dict:
        changes = {column: value for column, value in
                   (("password_hash", password_hash), ("role", role), ("status", status))
                   if value is not None}
        with self.connect() as db:
            if self._row(db, "SELECT 1 FROM users WHERE id = ?", user_id) is None:
                raise GatewayError(404, "user_not_found", "用户不存在。")
            if changes:
                assignments = ", ".join(f"{column} = :{column}" for column in changes)
                db.execute(f"UPDATE users SET {assignments} WHERE id = :id",
                           {**changes, "id": user_id})
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row)

    def create_auth_session(self, session: dict) -> None:
        with self.connect() as db:
            db.execute("""
                INSERT INTO auth_sessions (id, user_id, token_hash, created_at, expires_at,
                                           last_seen_at, ip, user_agent, revoked_at)
                VALUES (:id, :user_id, :token_hash, :created_at, :expires_at,
                        :last_seen_at, :ip, :user_agent, NULL)
            """, session)

    def find_auth_session(self, token_hash: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("""
                SELECT auth_sessions.*, users.username, users.role, users.status AS user_status
                FROM auth_sessions JOIN users ON users.id = auth_sessions.user_id
                WHERE auth_sessions.token_hash = ?
            """, (token_hash,)).fetchone()
        return dict(row) if row else None

    def touch_auth_session(self, session_id: str, last_seen: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE auth_sessions SET last_seen_at = ? WHERE id = ?",
                       (last_seen, session_id))

    def revoke_auth_session(self, session_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE auth_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                       (now(), session_id))

    def revoke_user_sessions(self, user_id: str, *, except_id: str | None = None) -> None:
        query = ("UPDATE auth_sessions SET revoked_at = ?"
                 " WHERE user_id = ? AND revoked_at IS NULL")
        parameters: list[object] = [now(), user_id]
        if except_id:
            query += " AND id != ?"
            parameters.append(except_id)
        with self.connect() as db:
            db.execute(query, parameters)

    def count_active_admins(self) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND status = 'active'"
            ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------ API Key

    def create_api_key(self, event: dict) -> None:
        with self.connect() as db:
            db.execute("""
                INSERT INTO api_keys (id, name, key_hash, key_prefix, scopes, owner_id,
                                      expires_at, created_at, last_used_at, revoked_at,
                                      allowed_cidrs)
                VALUES (:id, :name, :key_hash, :key_prefix, :scopes, :owner_id,
                        :expires_at, :created_at, NULL, NULL, :allowed_cidrs)
            """, event)

    def update_api_key(self, key_id: str, *, name: str | None = None,
                       allowed_cidrs: str | None = None) -> dict:
        changes = {column: value for column, value in
                   (("name", name), ("allowed_cidrs", allowed_cidrs))
                   if value is not None}
        with self.connect() as db:
            if self._row(db, "SELECT 1 FROM api_keys WHERE id = ?", key_id) is None:
                raise GatewayError(404, "api_key_not_found", "API Key 不存在。")
            if changes:
                assignments = ", ".join(f"{column} = :{column}" for column in changes)
                db.execute(f"UPDATE api_keys SET {assignments} WHERE id = :id",
                           {**changes, "id": key_id})
            row = db.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        return dict(row)

    def find_api_key(self, key_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        return dict(row) if row else None

    def list_api_keys(self, owner_id: str | None = None) -> list[dict]:
        query = ("SELECT id, name, key_prefix, scopes, owner_id, expires_at, created_at,"
                 " last_used_at, revoked_at, allowed_cidrs FROM api_keys")
        parameters: tuple = ()
        if owner_id:
            query += " WHERE owner_id = ?"
            parameters = (owner_id,)
        query += " ORDER BY created_at DESC"
        with self.connect() as db:
            rows = db.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def revoke_api_key(self, key_id: str) -> bool:
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now(), key_id))
            return cursor.rowcount > 0

    def touch_api_key(self, key_id: str, timestamp: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (timestamp, key_id))

    def count_audit_before(self, before: str) -> int:
        with self.connect() as db:
            row = db.execute("SELECT COUNT(*) FROM audit_log WHERE ts < ?",
                             (before,)).fetchone()
        return int(row[0])

    def prune_audit(self, before: str, batch: int = 1000) -> int:
        """删除 ts 早于 before 的审计行；分批提交，避免长时间持有写锁。"""
        deleted = 0
        while True:
            with self.connect() as db:
                rows = db.execute(
                    "SELECT id FROM audit_log WHERE ts < ? LIMIT ?", (before, batch)
                ).fetchall()
                if not rows:
                    break
                db.executemany("DELETE FROM audit_log WHERE id = ?",
                               [(row["id"],) for row in rows])
                deleted += len(rows)
            if len(rows) < batch:
                break
        return deleted

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
