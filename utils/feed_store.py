# -*- coding: utf-8 -*-
"""推荐流采集的 SQLite 存储层。

三张表，职责单一：
    aweme    作品元数据 + 命中的主题 + 是否已下载
    comment  评论（一级与二级平铺，用 is_reply 区分）
    round    每轮采集的统计，便于回溯

设计取舍
--------
- 唯一索引放在 aweme_id / (aweme_id, comment_id) 上，插入用
  INSERT OR IGNORE，天然幂等，不用先查后插。
- 主题以 JSON 文本存，不建关联表——主题种类少且不需要跨表 JOIN，
  省一层复杂度。
- 只记已处理过的作品，不记"已跳过"的状态。跨轮去重靠 aweme 主键。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS aweme (
    aweme_id      TEXT PRIMARY KEY,
    desc          TEXT,
    author_name   TEXT,
    author_uid    TEXT,
    digg_count    INTEGER,
    comment_count INTEGER,
    share_count   INTEGER,
    create_time   INTEGER,
    topics        TEXT,
    max_conf      REAL DEFAULT 0,
    collected     INTEGER DEFAULT 0,
    fetch_round   INTEGER,
    fetched_at    INTEGER
);

CREATE TABLE IF NOT EXISTS comment (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    aweme_id   TEXT NOT NULL,
    comment_id TEXT,
    text       TEXT,
    user_name  TEXT,
    digg_count INTEGER,
    is_reply   INTEGER DEFAULT 0,
    parent_id  TEXT,
    created_at INTEGER,
    UNIQUE(aweme_id, comment_id)
);

CREATE INDEX IF NOT EXISTS idx_comment_aweme ON comment(aweme_id);

CREATE TABLE IF NOT EXISTS round (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  INTEGER,
    finished_at INTEGER,
    fetched     INTEGER DEFAULT 0,
    classified  INTEGER DEFAULT 0,
    hit         INTEGER DEFAULT 0,
    downloaded  INTEGER DEFAULT 0,
    comments    INTEGER DEFAULT 0,
    note        TEXT
);
"""


def _topics_to_json(topics):
    return json.dumps(topics or [], ensure_ascii=False)


class FeedStore:
    def __init__(self, db_path):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- aweme

    def is_seen(self, aweme_id):
        cur = self.conn.execute(
            "SELECT 1 FROM aweme WHERE aweme_id = ? LIMIT 1", (aweme_id,))
        return cur.fetchone() is not None

    def filter_new(self, aweme_ids):
        """从一批 id 里挑出库里没有的，保持原顺序。"""
        if not aweme_ids:
            return []
        marks = ",".join("?" for _ in aweme_ids)
        cur = self.conn.execute(
            "SELECT aweme_id FROM aweme WHERE aweme_id IN (%s)" % marks,
            list(aweme_ids),
        )
        seen = {r[0] for r in cur.fetchall()}
        return [a for a in aweme_ids if a not in seen]

    def save_aweme(self, aw, topics=None, threshold=0.8, fetch_round=None):
        """落一条作品元数据。已存在的忽略（幂等）。"""
        topics = topics or []
        max_conf = max((t.get("conf", 0) for t in topics), default=0.0)
        self.conn.execute(
            """INSERT OR IGNORE INTO aweme
               (aweme_id, desc, author_name, author_uid, digg_count,
                comment_count, share_count, create_time, topics,
                max_conf, collected, fetch_round, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)""",
            (
                aw.get("aweme_id"),
                (aw.get("desc") or "")[:1000],
                ((aw.get("author") or {}).get("nickname") or ""),
                ((aw.get("author") or {}).get("sec_uid") or ""),
                (aw.get("statistics") or {}).get("digg_count") or 0,
                (aw.get("statistics") or {}).get("comment_count") or 0,
                (aw.get("statistics") or {}).get("share_count") or 0,
                aw.get("create_time") or 0,
                _topics_to_json(topics),
                round(max_conf, 3),
                fetch_round,
                int(time.time()),
            ),
        )

    def mark_collected(self, aweme_id, round_id=None):
        self.conn.execute(
            "UPDATE aweme SET collected = 1 WHERE aweme_id = ?", (aweme_id,))

    def commit(self):
        self.conn.commit()

    # -------------------------------------------------------------- comment

    def save_comments(self, aweme_id, comments):
        """保存评论树。comments 是 get_work_all_comment 的返回结构。"""
        n = 0
        for c in comments or []:
            n += self._insert_comment(aweme_id, c, is_reply=0, parent=None)
            for r in c.get("reply_comment") or []:
                n += self._insert_comment(
                    aweme_id, r, is_reply=1,
                    parent=c.get("cid") or c.get("comment_id"))
        return n

    def _insert_comment(self, aweme_id, c, is_reply, parent):
        try:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO comment
                   (aweme_id, comment_id, text, user_name, digg_count,
                    is_reply, parent_id, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    aweme_id,
                    c.get("cid") or c.get("comment_id") or "",
                    (c.get("text") or "")[:2000],
                    ((c.get("user") or {}).get("nickname") or ""),
                    c.get("digg_count") or 0,
                    is_reply,
                    parent or "",
                    c.get("create_time") or 0,
                ),
            )
            return cur.rowcount if cur.rowcount > 0 else 0
        except sqlite3.Error:
            return 0

    # ---------------------------------------------------------------- round

    def start_round(self):
        cur = self.conn.execute(
            "INSERT INTO round (started_at) VALUES (?)", (int(time.time()),))
        self.conn.commit()
        return cur.lastrowid

    def finish_round(self, round_id, **counts):
        fields = ["finished_at=?"]
        values = [int(time.time())]
        for k in ("fetched", "classified", "hit", "downloaded",
                  "comments", "note"):
            if k in counts:
                fields.append("%s=?" % k)
                values.append(counts[k])
        values.append(round_id)
        self.conn.execute(
            "UPDATE round SET %s WHERE id=?" % ", ".join(fields), values)
        self.conn.commit()

    # ---------------------------------------------------------------- stats

    def stats(self):
        q = self.conn.execute
        return {
            "aweme_total": q("SELECT COUNT(*) FROM aweme").fetchone()[0],
            "aweme_collected": q(
                "SELECT COUNT(*) FROM aweme WHERE collected=1").fetchone()[0],
            "comment_total": q("SELECT COUNT(*) FROM comment").fetchone()[0],
            "round_total": q("SELECT COUNT(*) FROM round").fetchone()[0],
            "by_topic": self._by_topic(),
            "recent_rounds": self._recent_rounds(),
        }

    def _by_topic(self):
        out = {}
        for row in self.conn.execute(
                "SELECT topics FROM aweme WHERE max_conf > 0"):
            try:
                for t in json.loads(row[0] or "[]"):
                    name = t.get("name")
                    out[name] = out.get(name, 0) + 1
            except (ValueError, TypeError):
                continue
        return out

    def _recent_rounds(self, limit=5):
        rows = self.conn.execute(
            """SELECT id, started_at, fetched, hit, downloaded, comments,
                      note FROM round ORDER BY id DESC LIMIT ?""", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
