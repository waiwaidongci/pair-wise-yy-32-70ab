"""放行复核存储：release_reviews 表，以及放行决定上的复核引用。"""
from __future__ import annotations

import json
import sqlite3


class ReviewStore:
    """只负责复核结论的建表、写入与查询；事务由调用方管理。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        with self.conn:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS release_reviews (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id),
              revision INTEGER NOT NULL,
              conclusion TEXT NOT NULL CHECK(conclusion IN ('release','conditional','hold')),
              opinion TEXT NOT NULL,
              precheck_json TEXT NOT NULL,
              created_by TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """)
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(decisions)")}
            if "review_id" not in columns:
                self.conn.execute("ALTER TABLE decisions ADD COLUMN review_id INTEGER REFERENCES release_reviews(id)")

    def save(self, batch_id: int, revision: int, conclusion: str, opinion: str,
             precheck: dict, actor: str, created_at: str) -> dict:
        cur = self.conn.execute(
            """INSERT INTO release_reviews(batch_id,revision,conclusion,opinion,precheck_json,created_by,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (batch_id, revision, conclusion, opinion,
             json.dumps(precheck, ensure_ascii=False, sort_keys=True), actor, created_at))
        return self.get(cur.lastrowid)

    def get(self, review_id: int) -> dict:
        return self._dict(self.conn.execute("SELECT * FROM release_reviews WHERE id=?", (review_id,)).fetchone())

    def latest(self, batch_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM release_reviews WHERE batch_id=? ORDER BY id DESC LIMIT 1", (batch_id,)).fetchone()
        return self._dict(row) if row else None

    def for_batch(self, batch_id: int) -> list[dict]:
        return [self._dict(row) for row in self.conn.execute(
            "SELECT * FROM release_reviews WHERE batch_id=? ORDER BY id DESC", (batch_id,))]

    def valid_for(self, batch_id: int, revision: int) -> dict | None:
        """只有与批次当前修订号一致的最新复核才有效；资料变更会推进修订号使旧复核失效。"""
        latest = self.latest(batch_id)
        return latest if latest and latest["revision"] == revision else None

    @staticmethod
    def _dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["precheck"] = json.loads(data.pop("precheck_json"))
        return data
