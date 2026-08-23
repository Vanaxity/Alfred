"""
SQLite local database for Alfred Brain API.
Replaces Supabase tables: user_state, conversations.

Uses a single persistent connection with WAL mode for concurrency.
No threading locks needed — single connection, all access serialized.
"""

import sqlite3
import json
import threading
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).parent / "data" / "alfred.db"


class LocalDB:
    """SQLite database with single persistent connection + WAL mode."""

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=3000")
        return self._conn

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_state (
                id TEXT PRIMARY KEY DEFAULT 'default',
                mode TEXT DEFAULT 'FOUNDER',
                pc_telemetry TEXT DEFAULT '{}',
                location TEXT DEFAULT '',
                mood_score REAL DEFAULT 0,
                active_quest TEXT DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT UNIQUE NOT NULL,
                session_name TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                last_active_at TEXT DEFAULT (datetime('now')),
                is_active INTEGER DEFAULT 1
            );

            CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations(session_id);

            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                due_at TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                fired INTEGER DEFAULT 0,
                category TEXT DEFAULT 'general'
            );

            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task TEXT NOT NULL,
                cron_expr TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                active INTEGER DEFAULT 1,
                last_run TEXT
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT DEFAULT '',
                episode_path TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

            INSERT OR IGNORE INTO user_state (id, mode) VALUES ('default', 'FOUNDER');
        """)
        conn.commit()
        
        # Migration: add missing columns to existing tables
        self._migrate(conn)
    
    def _migrate(self, conn: sqlite3.Connection):
        """Add missing columns to existing tables (safe — ignores if already exists)."""
        migrations = [
            ("conversations", "summary", "TEXT DEFAULT ''"),
            ("messages", "episode_path", "TEXT"),
        ]
        for table, column, col_type in migrations:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                conn.commit()
                print(f"  [DB] Migration: added {table}.{column}")
            except sqlite3.OperationalError:
                pass  # Column already exists

    def import_json(self, table: str, data: List[Dict]):
        """Import data from JSON export (idempotent - checks for existing data)."""
        if not data:
            return
        conn = self._get_conn()
        with self._lock:
            # Check if already imported by seeing if table has data
            existing = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if existing > 0:
                return

            for row in data:
                if table == "user_state":
                    conn.execute("""
                        INSERT OR IGNORE INTO user_state (id, mode, pc_telemetry, location, mood_score, active_quest, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        row.get("id", "default"),
                        row.get("mode", "FOUNDER"),
                        json.dumps(row.get("pc_telemetry", {})),
                        row.get("location", ""),
                        row.get("mood_score", 0),
                        row.get("active_quest", ""),
                        row.get("updated_at", datetime.now().isoformat()),
                    ))
                elif table == "conversations":
                    conn.execute("""
                        INSERT OR IGNORE INTO conversations (session_id, session_name, last_active_at, is_active)
                        VALUES (?, ?, ?, ?)
                    """, (
                        row.get("session_id", ""),
                        row.get("session_name", ""),
                        row.get("last_active_at", datetime.now().isoformat()),
                        1 if row.get("is_active", True) else 0,
                    ))
            conn.commit()

    # --- user_state ---

    def get_user_state(self) -> Dict:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM user_state LIMIT 1").fetchone()
        if row:
            return {
                "id": row["id"],
                "mode": row["mode"],
                "pc_telemetry": json.loads(row["pc_telemetry"]),
                "location": row["location"],
                "mood_score": row["mood_score"],
                "active_quest": row["active_quest"],
                "updated_at": row["updated_at"],
            }
        return {"mode": "FOUNDER", "pc_telemetry": {}}

    def update_user_state(self, **kwargs) -> Dict:
        conn = self._get_conn()
        with self._lock:
            current = conn.execute("SELECT * FROM user_state LIMIT 1").fetchone()
            if not current:
                return {}

            fields = []
            values = []
            for key, value in kwargs.items():
                if key == "pc_telemetry" and isinstance(value, dict):
                    fields.append("pc_telemetry = ?")
                    values.append(json.dumps(value))
                elif key in ["mode", "location", "mood_score", "active_quest"]:
                    fields.append(f"{key} = ?")
                    values.append(value)

            if fields:
                fields.append("updated_at = datetime('now')")
                values.append(current["id"])
                conn.execute(
                    f"UPDATE user_state SET {', '.join(fields)} WHERE id = ?",
                    values
                )
                conn.commit()

        return self.get_user_state()

    # --- conversations ---

    def get_sessions(self, limit: int = 20, active_only: bool = True) -> List[Dict]:
        conn = self._get_conn()
        query = "SELECT * FROM conversations"
        params = []
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY last_active_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        return [
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "session_name": row["session_name"],
                "summary": row["summary"],
                "last_active_at": row["last_active_at"],
                "is_active": bool(row["is_active"]),
            }
            for row in rows
        ]

    def get_session(self, session_id: str) -> Optional[Dict]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM conversations WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "session_name": row["session_name"],
            "summary": row["summary"],
            "last_active_at": row["last_active_at"],
            "is_active": bool(row["is_active"]),
        }

    def create_session(self, session_id: str = None, session_name: str = "") -> str:
        if not session_id:
            session_id = f"session_{int(datetime.now().timestamp())}"
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                "INSERT OR REPLACE INTO conversations (session_id, session_name, summary, last_active_at, is_active) VALUES (?, ?, '', datetime('now'), 1)",
                (session_id, session_name)
            )
            conn.commit()
            return session_id

    def touch_session(self, session_id: str):
        """Update last_active_at to now for a session."""
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                "UPDATE conversations SET last_active_at = datetime('now') WHERE session_id = ?",
                (session_id,)
            )
            conn.commit()

    def update_session(self, session_id: str, **kwargs):
        conn = self._get_conn()
        with self._lock:
            fields = []
            values = []
            for key, value in kwargs.items():
                if key in ["session_name", "summary", "last_active_at", "is_active"]:
                    fields.append(f"{key} = ?")
                    # Only is_active is a boolean column. This was previously
                    # written as `1 if value else 0 if key == "is_active" else value`,
                    # which Python parses right-associatively as
                    # `1 if value else (0 if ... else ...)` -- so EVERY truthy
                    # value became literal 1, silently destroying every summary
                    # and session_name ever written.
                    if key == "is_active":
                        values.append(1 if value else 0)
                    else:
                        values.append(value)
            if fields:
                values.append(session_id)
                conn.execute(
                    f"UPDATE conversations SET {', '.join(fields)} WHERE session_id = ?",
                    values
                )
                conn.commit()


    # ============ MESSAGES ============

    def add_message(self, session_id: str, role: str, content: str) -> int:
        conn = self._get_conn()
        with self._lock:
            cur = conn.execute(
                "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
                (session_id, role, content)
            )
            conn.commit()
            return cur.lastrowid

    def get_messages(self, session_id: str, limit: int = 50, offset: int = 0) -> List[Dict]:
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT id, session_id, role, content, episode_path, created_at FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (session_id, limit, offset)
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def get_message_count(self, session_id: str) -> int:
        conn = self._get_conn()
        with self._lock:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM messages WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            return row["cnt"] if row else 0

    def update_message_episode_path(self, message_id: int, episode_path: str):
        conn = self._get_conn()
        with self._lock:
            conn.execute(
                "UPDATE messages SET episode_path = ? WHERE id = ?",
                (episode_path, message_id)
            )
            conn.commit()

    def get_recent_context(self, session_id: str, count: int = 10) -> List[Dict]:
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, count)
            ).fetchall()
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def get_session_episodes(self, session_id: str) -> List[Dict]:
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute(
                "SELECT id, episode_path, created_at FROM messages WHERE session_id = ? AND episode_path IS NOT NULL ORDER BY id DESC",
                (session_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_session_messages(self, session_id: str):
        conn = self._get_conn()
        with self._lock:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.commit()

    def delete_session(self, session_id: str):
        conn = self._get_conn()
        with self._lock:
            conn.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))
            conn.commit()

    # ============ SCHEDULED TASKS (legacy v1 path only — see local_db.py) ============

    def update_last_run(self, task_id: int):
        conn = self._get_conn()
        with self._lock:
            conn.execute("UPDATE scheduled_tasks SET last_run = datetime('now') WHERE id = ?", (task_id,))
            conn.commit()


# Singleton
_db_instance: Optional[LocalDB] = None


def get_local_db() -> LocalDB:
    global _db_instance
    if _db_instance is None:
        _db_instance = LocalDB()
    return _db_instance
