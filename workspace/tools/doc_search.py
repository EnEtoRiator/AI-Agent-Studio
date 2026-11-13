import re
from pathlib import Path
import workspace.config as config
from workspace.core.database import Database


class DocSearchEngine:
    def __init__(self, db: Database, docs_dir: str = config.DOCS_DIR):
        self.db = db
        self.docs_dir = Path(docs_dir).expanduser().resolve()
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.reindex_docs()

    def reindex_docs(self) -> int:
        """Индексирует все .md и .txt файлы из папки документаций в SQLite FTS5."""
        files = list(self.docs_dir.rglob("*.md")) + list(self.docs_dir.rglob("*.txt"))
        with self.db._connect() as conn:
            conn.execute("DELETE FROM docs_fts")
            for f in files:
                try:
                    text = f.read_text(encoding="utf-8")
                    rel_path = str(f.relative_to(self.docs_dir))
                    conn.execute(
                        "INSERT INTO docs_fts (file_path, title, content) VALUES (?, ?, ?)",
                        (rel_path, f.stem, text),
                    )
                except Exception:
                    continue
        return len(files)

    def search_docs(self, query: str, limit: int = 3) -> str:
        """Поиск релевантных фрагментов по алгоритму BM25 без нагрузки на видеопамять."""
        tokens = re.findall(r"\w+", query.lower())
        if not tokens:
            return "Пустой поисковый запрос."

        fts_query = " OR ".join(tokens)
        with self.db._connect() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT file_path, title, snippet(docs_fts, 2, '[', ']', '...', 45) AS frag
                    FROM docs_fts
                    WHERE docs_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, limit),
                ).fetchall()
            except Exception as e:
                return f"Ошибка поиска по документации: {str(e)}"

        if not rows:
            return f"В локальной документации ({self.docs_dir}) ничего не найдено по запросу '{query}'."

        results = []
        for r in rows:
            results.append(f"📘 **{r['file_path']}**:\n{r['frag']}")
        return "\n\n".join(results)