"""笔记数据存储：支持保存对话内容、提取引文及用户自定义笔记。"""
import os
import sqlite3
import time
import uuid


class NotesStore:
    def __init__(self, data_root: str):
        self.db_path = os.path.join(data_root, "学术引文索引", "notes.sqlite3")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            title TEXT,
            content TEXT,
            sources_json TEXT,
            tags_json TEXT,
            created_at REAL,
            updated_at REAL
        );
        """)
        self.db.commit()

    def list_notes(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM notes ORDER BY updated_at DESC").fetchall()
        import json
        out = []
        for r in rows:
            d = dict(r)
            d["sources"] = json.loads(d["sources_json"] or "[]")
            d["tags"] = json.loads(d["tags_json"] or "[]")
            out.append(d)
        return out

    def get_note(self, note_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
        if not r:
            return None
        import json
        d = dict(r)
        d["sources"] = json.loads(d["sources_json"] or "[]")
        d["tags"] = json.loads(d["tags_json"] or "[]")
        return d

    def create_note(self, title: str, content: str, sources: list = None, tags: list = None) -> dict:
        import json
        nid = "note-" + uuid.uuid4().hex[:12]
        now = time.time()
        s_json = json.dumps(sources or [], ensure_ascii=False)
        t_json = json.dumps(tags or [], ensure_ascii=False)
        self.db.execute(
            "INSERT INTO notes (id, title, content, sources_json, tags_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (nid, title or "未命名笔记", content, s_json, t_json, now, now)
        )
        self.db.commit()
        return self.get_note(nid)

    def update_note(self, note_id: str, title: str = None, content: str = None, sources: list = None, tags: list = None) -> dict | None:
        import json
        cur = self.get_note(note_id)
        if not cur:
            return None
        new_title = title if title is not None else cur["title"]
        new_content = content if content is not None else cur["content"]
        new_sources = json.dumps(sources if sources is not None else cur["sources"], ensure_ascii=False)
        new_tags = json.dumps(tags if tags is not None else cur["tags"], ensure_ascii=False)
        now = time.time()
        self.db.execute(
            "UPDATE notes SET title=?, content=?, sources_json=?, tags_json=?, updated_at=? WHERE id=?",
            (new_title, new_content, new_sources, new_tags, now, note_id)
        )
        self.db.commit()
        return self.get_note(note_id)

    def delete_note(self, note_id: str) -> bool:
        self.db.execute("DELETE FROM notes WHERE id=?", (note_id,))
        self.db.commit()
        return True
