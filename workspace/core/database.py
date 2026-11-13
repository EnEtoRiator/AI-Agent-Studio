import sqlite3
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

import workspace.config as config


class Database:
    def __init__(self, db_path: str = config.DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    rolling_summary TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS chats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
                    file_path,
                    title,
                    content,
                    tokenize='unicode61'
                );
                """
            )

    # --- Проекты ---

    def get_or_create_project(self, folder_path: str) -> Dict[str, Any]:
        resolved = str(Path(folder_path).expanduser().resolve())
        name = Path(resolved).name or resolved

        with self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE path = ?", (resolved,)).fetchone()
            if row:
                return dict(row)

            cur = conn.execute(
                "INSERT INTO projects (name, path, rolling_summary) VALUES (?, ?, '')",
                (name, resolved),
            )
            new_row = conn.execute("SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(new_row)

    def get_project_summary(self, project_id: int) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT rolling_summary FROM projects WHERE id = ?", (project_id,)).fetchone()
            return row["rolling_summary"] if row and row["rolling_summary"] else ""

    def update_project_summary(self, project_id: int, summary: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET rolling_summary = ? WHERE id = ?",
                (summary.strip(), project_id),
            )

    # --- Чаты ---

    def create_chat(self, project_id: int, title: str = "Новый диалог") -> Dict[str, Any]:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO chats (project_id, title) VALUES (?, ?)",
                (project_id, title),
            )
            row = conn.execute("SELECT * FROM chats WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row)

    def list_chats(self, project_id: int) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chats WHERE project_id = ? ORDER BY id DESC",
                (project_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_chat_title(self, chat_id: int, title: str) -> None:
        short_title = title.strip().replace("\n", " ")[:42]
        if len(title.strip()) > 42:
            short_title += "..."
        with self._connect() as conn:
            conn.execute("UPDATE chats SET title = ? WHERE id = ?", (short_title, chat_id))

    def delete_chat(self, chat_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))

    # --- Сообщения ---

    def add_message(self, chat_id: int, role: str, content: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)",
                (chat_id, role, content),
            )

    def get_chat_messages(self, chat_id: int) -> List[Tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id ASC",
                (chat_id,),
            ).fetchall()
            return [(r["role"], r["content"]) for r in rows]

    def get_recent_messages(self, chat_id: int, limit: int = config.MAX_HISTORY_MESSAGES) -> List[Tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
            return [(r["role"], r["content"]) for r in reversed(rows)]

    def count_chat_messages(self, chat_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as cnt FROM messages WHERE chat_id = ?", (chat_id,)).fetchone()
            return row["cnt"] if row else 0